"""Native-model DFS/backtracking stress run; workers own every project check."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from harness import ROOT, PROFILES, LIMIT_ATTEMPTS, AgentFailure, Harness, Qwen, State, atomic, execute, files, status
from tests.e2e import check, require_snapshot

LONGFLOW_SECONDS=6*3600  # Acceptance-test watchdog only; production projects have no elapsed-time cutoff.

GOAL = '''Complete MiniLedger, an offline Python standard-library CSV expense summarizer.
Keep ledger.py and miniledger.py as its public interface. Read the existing files before changing code.
ledger.parse_date(text): accept exactly YYYY-MM-DD and real calendar dates, else ValueError.
ledger.read_rows(stream): return a list of (datetime.date, category, integer amount_cents).
Require the exact header date,category,amount_cents in that order; reject missing/extra/duplicate columns,
bad CSV quoting, wrong row widths, invalid dates, and non-integer amounts with ValueError.
Amounts are ASCII base-10 integers: optional + or - followed only by 0..9. Negative refunds and zero are valid; decimals, underscores and non-ASCII digits are invalid.
Trim category whitespace; reject empty categories and reserved TOTAL. Validate all rows before filtering.
ledger.summarize(rows, start=None, end=None): bounds are optional ISO date strings, both inclusive.
Reject invalid bounds and start > end. Return (category, count, total_cents) tuples sorted by category,
with ('TOTAL', overall_count, overall_cents) last, even when there are no matching rows. Do not mutate inputs.
ledger.write_report(summary, stream): exact header category,count,total_cents, CSV quoting as needed,
LF newlines and a trailing newline. Output must be deterministic.
CLI: python miniledger.py INPUT.csv [--start YYYY-MM-DD] [--end YYYY-MM-DD].
Read UTF-8 (also accept a BOM), write only the CSV report to stdout, exit 0 on success.
Invalid input, dates, missing files or usage: exit 2, concise stderr error, no traceback or partial report.
CLI stdout must use LF bytes on Windows as well.
Independent standard-library unittest files under tests/test_*.py must cover valid data and edge cases.
Example input: date,category,amount_cents\n2026-01-01,food,1200\n2026-01-02,travel,2500\n2026-01-02,food,-200
Example report: category,count,total_cents\nfood,2,1000\ntravel,1,2500\nTOTAL,3,3500
This is the full behavioral brief, not an implementation or repair plan. Preserve correct tests. Verify existing behavior before repairs; report concrete defects.
Delegate implementation to ALGORITHM and independent test authoring/execution to TEST.
Cover both public modules and the complete brief in the persisted TODO plan, not just in reasoning.
Choose meaningful composite scopes and their smaller prerequisites yourself; each parent still needs its own report.
Deliver a reviewed release: component implementation and independent checks feed a final integration/release check
of the combined library and CLI. That check depends on the component verification results, not only root order.
'''

CHANGE = '''Follow-up request after the first release:
Provide reusable validation in a separate ledger_rules.py module: parse_date(text) returns datetime.date
and parse_amount(text) returns integer cents, enforcing the original exact date/ASCII-amount rules.
ledger.py must delegate both validations to this module while preserving its public interface and CLI behavior.
The reusable module needs independent verification before integration; existing consumers depend on that result.
First delegate TEST to check this new contract against the current release, before changing implementation.
Missing required public validation behavior is a failed contract check, not a successful inventory report.
Report actual missing behavior, then replan repairs; reopen affected earlier DONE work instead of duplicating it.
Keep correct existing tests and recheck the affected release. PROMPT chooses all TODOs and relationships.
'''

LEDGER = '''import csv
from datetime import date

def parse_date(text):
    return date.fromisoformat(text)

def read_rows(stream):
    reader = csv.reader(stream)
    next(reader, None)
    return [(parse_date(day), category.strip(), int(amount)) for day, category, amount in reader]

def summarize(rows, start=None, end=None):
    totals = {}
    for day, category, amount in rows:
        if amount < 0:
            continue
        if start and day <= parse_date(start):
            continue
        if end and day >= parse_date(end):
            continue
        count, cents = totals.get(category, (0, 0))
        totals[category] = count + 1, cents + amount
    result = [(name, *totals[name]) for name in sorted(totals)]
    return result + [('TOTAL', sum(row[1] for row in result), sum(row[2] for row in result))]

def write_report(summary, stream):
    writer = csv.writer(stream, lineterminator='\\n')
    writer.writerow(['category', 'count', 'total_cents'])
    writer.writerows(summary)
'''

CLI = '''import argparse
import sys
from ledger import read_rows, summarize, write_report

def main():
    parser = argparse.ArgumentParser(description='MiniLedger offline expenses')
    parser.add_argument('input')
    parser.add_argument('--start')
    parser.add_argument('--end')
    args = parser.parse_args()
    try:
        with open(args.input, encoding='utf-8-sig', newline='') as stream:
            rows = read_rows(stream)
        write_report(summarize(rows, args.start, args.end), sys.stdout)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 0
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
'''


def note(session,*parts):
    with (session/'flow.log').open('a',encoding='utf-8') as log:
        log.write(' | '.join(str(p).replace('\n',' ') for p in parts)+'\n')


def depth(state):
    maximum = 0
    for t in state.tasks.values():
        if t.status == 'DROPPED': continue
        level,parent = 1,t.parent
        while parent: level,parent = level+1,state.tasks[parent].parent
        maximum = max(maximum,level)
    return maximum


class Pause(Exception): pass


class RecordingQwen(Qwen):
    owner = '-'
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.plan_attempts = []

    def call(self,role,prompt,workspace,structured=False,**kwargs):
        mode = ('native' if structured else 'text') if role=='PROMPT' else 'report'
        note(self.session,'CALL',role,self.owner,mode)
        returned = False
        try:
            result = super().call(role,prompt,workspace,structured,**kwargs)
            returned = True
            return result
        except AgentFailure as e:
            note(self.session,'AGENT_ERROR',role,self.owner,e.kind)
            raise
        finally:
            if role=='PROMPT': self.plan_attempts.append((mode,returned))
            if returned and self.session_ids: note(self.session,'CONTEXT',self.session_ids[-1])


class FlowHarness(Harness):
    """Observe orchestration and pause only after a committed worker report."""
    def __init__(self,session,agent,stop='complete',run_seconds=None):
        super().__init__(session,agent,run_seconds)
        self.stop = stop
        if self.state.actions==0: self.save_planning_snapshot()

    def save_planning_snapshot(self):
        if self.state.replans and self.state.phase=='ready':
            path=self.session/'planning'/f'{self.state.replans}.log'
            if not path.exists():atomic(path,self.state.render())

    def plan(self):
        self.agent.owner = str(self.state.replans if self.state.phase=='planning' else self.state.replans+1)
        start,accepted = len(self.agent.plan_attempts),False
        failed = [t.id for t in self.state.tasks.values() if t.status in {'FAILED','BLOCKED'}]
        next_id = self.state.next_id
        done = {t.id for t in self.state.tasks.values() if t.status=='DONE'}
        try:
            super().plan()
            accepted = True
            for id in sorted(done):
                task=self.state.tasks[id]
                if task.status=='DONE': continue
                if task.kind=='reopened':note(self.session,'REOPEN',id,task.reason)
                else:
                    cause=re.match(r'Prerequisite (\d+) changed',task.reason)
                    via='needs' if cause and int(cause[1]) in task.needs else 'child'
                    note(self.session,'INVALIDATE',id,via,task.reason)
            for task in self.state.tasks.values():
                if task.id >= next_id and task.parent and task.status != 'DROPPED':
                    note(self.session,'BEFORE',task.id,task.parent)
            for id in failed:
                if self.state.tasks[id].status != 'WAITING': continue
                descendants = []
                for t in self.state.tasks.values():
                    parent = t.parent
                    while parent and parent != id: parent = self.state.tasks[parent].parent
                    if parent == id and t.status != 'DROPPED': descendants.append(t.id)
                note(self.session,'SPLIT',id,','.join(map(str,sorted(descendants))))
        finally:
            attempts = self.agent.plan_attempts[start:]
            for i,(mode,returned) in enumerate(attempts):
                result = 'ACCEPTED' if accepted and i==len(attempts)-1 else 'INVALID' if returned else 'ERROR'
                note(self.session,'PLAN',mode,result)
            if accepted: note(self.session,'TREE',len(self.state.tasks),depth(self.state),self.state.replans)
            self.save_planning_snapshot()
        if self.stop=='invalidation' and any(self.state.tasks[id].kind=='invalidated' for id in done):raise Pause()

    def step(self,task):
        check(task.status!='DONE','Restart replayed accepted work')
        self.agent.owner=str(task.id)
        selected,route=self.state.frontier()
        check(selected is task,'DFS skipped its dependency/child frontier')
        check(all(self.state.tasks[id].status=='DONE' for id in self.state.prerequisites(task.id)),
              'Worker was delegated before children/needs completed')
        deferred=','.join(str(t.id) for t in sorted(self.state.tasks.values(),key=lambda t:t.id)
                          if t.id<task.id and t.status=='PENDING' and t.attempts<LIMIT_ATTEMPTS and
                          all(self.state.tasks[id].status=='DONE' for id in self.state.prerequisites(t.id))) or '-'
        note(self.session,'START',task.id,task.parent,task.role,task.attempts+1,route,deferred)
        report=super().step(task)
        if report:
            note(self.session,'REPORT',task.id,task.role,report['status'],report['summary'],report.get('reason',''))
            if self.stop=='failure' and report['status'] in {'FAILED','BLOCKED'}: raise Pause()
        return report


def checkpoint(session,name):
    dest=session/'checkpoints'/name
    check(not dest.exists(),'Checkpoint already exists: '+str(dest))
    atomic(dest/'state.log',(session/'state.log').read_text(encoding='utf-8'))
    atomic(dest/'files.txt','\n'.join(f'{p} | {h}' for p,h in sorted(files(session/'workspace').items())))
    atomic(dest/'turns.txt','\n'.join(sorted(p.name for p in (session/'turns').glob('*'))))
    note(session,'CHECKPOINT',name)
    return dest


def resume_check(session,saved):
    before=State.load(saved/'state.log')
    check((session/'state.log').read_bytes()==(saved/'state.log').read_bytes(),'Restart state differs from checkpoint')
    manifest='\n'.join(f'{p} | {h}' for p,h in sorted(files(session/'workspace').items()))
    check(manifest==(saved/'files.txt').read_text(encoding='utf-8'),'Restart lost workspace files')
    turns='\n'.join(sorted(p.name for p in (session/'turns').glob('*')))
    check(turns==(saved/'turns.txt').read_text(encoding='utf-8'),'Restart replayed an agent call')
    note(session,'RESTART','PASS',saved.name,os.getpid(),before.phase)
    return [t.id for t in before.tasks.values() if t.status=='DONE']


def change_request(session):
    """A real new user requirement, not a planted defect or a harness-generated repair plan."""
    state=State.load(session/'state.log')
    check(state.complete(),'Initial release has unresolved work')
    check({t.role for t in state.tasks.values() if t.status!='DROPPED'}=={'ALGORITHM','TEST'},
          'Initial release lacks independent implementation/TEST delegation')
    goal=(session/'goal.txt').read_text(encoding='utf-8')
    state.closed=False;state.phase='ready'
    state.event('Follow-up user request: reusable validation module; original TODOs and reports retained')
    try:
        atomic(session/'goal.txt',goal+'\n'+CHANGE)
        state.save(session/'state.log')
    except BaseException:
        atomic(session/'goal.txt',goal)
        raise
    note(session,'REQUEST','Reusable validation module; independent verification before integration')


def worker(args):
    require_snapshot()
    session=args.session.resolve()
    check(session.parent==(ROOT/'sessions').resolve() and session.name.startswith('longflow-'),'Invalid worker session path')
    try:
        if args.resume:resume_check(session,args.resume)
        return FlowHarness(session,RecordingQwen(args.qwen,args.profile,args.context,args.url,session,agent_seconds=args.agent_seconds),args.stop,args.run_seconds).run()
    except Pause:
        status('LONGFLOW','CHECKPOINT','Accepted report and TODO persisted'); return 75
    except (OSError,ValueError,subprocess.TimeoutExpired) as e:
        note(session,'ERROR',type(e).__name__,str(e)); return 2 if isinstance(e,OSError) else 1


def metrics(session):
    rows=[line.split(' | ') for line in (session/'flow.log').read_text(encoding='utf-8').splitlines()] if (session/'flow.log').exists() else []
    result=dict(nodes=0,depth=0,replans=0,restarts=0,schema=0,fallback=0,invalid=0,retries=0,
                failed=0,done=0,blocked=0,cycles=0,planning=0,contexts=0,compactions=0,dfs=0,priority=0,
                crossings=0,returns=0,reopens=0,invalidated=0,forward=0,transitive=0,rechecked=0,recoveries=0,unresolved=0,resumes=0,parents=0,deepened=0)
    calls,repairs,contexts,returns,rechecks,failures=Counter(),{},[],set(),set(),set()
    parents=set();attempted=set();invalidations={}
    state=State.load(session/'state.log')
    for row in rows:
        if row[0]=='TREE':
            result['nodes']=int(row[1]); result['depth']=max(result['depth'],int(row[2])); result['replans']=max(0,int(row[3])-1)
        elif row[0]=='CALL':
            key=tuple(row[1:3]);calls[key]+=1
            result['retries']+=int(calls[key]>1);result['fallback']+=int(row[1]=='PROMPT' and row[3]=='text')
        elif row[0]=='PLAN':
            result['schema']+=int(row[1]=='native' and row[2]=='ACCEPTED');result['invalid']+=int(row[2]=='INVALID')
            result['planning']+=int(row[2]=='ACCEPTED')
            if row[2]=='ACCEPTED':
                result['transitive']+=sum(cause in invalidations for cause in invalidations.values())
                invalidations.clear()
        elif row[0]=='SPLIT' and int(row[1]) in repairs:
            repairs[int(row[1])]=[set(map(int,row[2].split(','))),False]
        elif row[0]=='BEFORE':result['deepened']+=int(int(row[2]) in attempted)
        elif row[0]=='REPORT':
            id,role,status=int(row[1]),row[2],row[3]
            result[{'DONE':'done','FAILED':'failed','BLOCKED':'blocked'}[status]]+=1
            if status in {'FAILED','BLOCKED'}:failures.add(id)
            if role=='TEST' and status in {'FAILED','BLOCKED'}: repairs[id]=[set(),False]
            if status=='DONE':
                result['parents']+=int(id in parents)
                if id in failures:result['recoveries']+=1;failures.remove(id)
                if id in returns:result['returns']+=1;returns.remove(id)
                if id in rechecks:result['rechecked']+=1;rechecks.remove(id)
                for parent,(children,fixed) in list(repairs.items()):
                    if id in children and role=='ALGORITHM':repairs[parent][1]=True
                    if id==parent and role=='TEST' and fixed:
                        result['cycles']+=1;del repairs[parent]
        elif row[0]=='START':
            attempted.add(int(row[1]))
            if int(row[2]):parents.add(int(row[2]))
            result['dfs']+=1;result['priority']+=int(row[6]!='-')
            hops=row[5].split('>');owner=int(hops[0])
            for hop in hops[1:]:
                edge,id=hop.split(':');id=int(id)
                if edge=='needs':returns.add(owner)
                owner=id
            root=state.tasks[int(row[1])]
            while root.parent:root=state.tasks[root.parent]
            result['crossings']+=int('>needs:' in row[5] and root.id!=int(hops[0]))
        elif row[0]=='REOPEN':result['reopens']+=1
        elif row[0]=='INVALIDATE':
            cause=re.match(r'Prerequisite (\d+) changed',row[3])
            if cause:invalidations[int(row[1])]=int(cause[1])
            result['invalidated']+=1;result['forward']+=int(row[2]=='needs');rechecks.add(int(row[1]))
        elif row[0]=='CONTEXT':contexts.append(row[1])
        elif row[0]=='RESTART' and row[1]=='PASS':
            result['restarts']+=1;result['resumes']+=int(row[4]!='complete')
    check(len(contexts)==len(set(contexts)),'A native Qwen context was unexpectedly reused')
    result['contexts']=len(contexts)
    result['unresolved']=len(failures)
    for log in (session/'turns').glob('*/native/projects/*/chats/*.jsonl'):
        for line in log.read_text(encoding='utf-8').splitlines():
            try: row=json.loads(line)
            except ValueError: continue
            result['compactions']+=int(isinstance(row,dict) and row.get('subtype')=='chat_compression')
    return result


def acceptance(state,measured):
    state.validate()
    check({t.role for t in state.tasks.values() if t.status!='DROPPED'}=={'ALGORITHM','TEST'},'Missing implementation or verification work')
    check(measured['restarts']>=2,'Missing checked checkpoint/completion restarts')
    check(measured['schema']>=1,'No accepted native model plan')
    check(measured['contexts']>=measured['done']+measured['failed']+measured['blocked'],'Missing fresh worker contexts')
    check(measured['dfs']==state.actions and state.actions>0,'Missing DFS branch checks for delegated attempts')
    check(measured['unresolved']==0,'A reported failure lacks its own later DONE report')
    check(measured['rechecked']==measured['invalidated'],'Invalidated results lack their own repeated DONE reports')
    check(state.phase=='complete' and state.complete(),'Not every original task has its own final DONE report')
    missing=[name for name,value in coverage(measured).items() if value!='EXERCISED']
    check(not missing,'LongFlow coverage missing: '+', '.join(missing))


def coverage(measured):
    observed={'dynamic replanning':measured['replans']>0,'dynamic deepening':measured['deepened']>0,'parent/child return':measured['parents']>0,
              'backtracking':measured['recoveries']>0,'cross-branch needs':measured['crossings']>0 and measured['returns']>0,
              'reopen':measured['reopens']>0,'forward invalidation':measured['forward']>0 and measured['transitive']>0 and measured['rechecked']==measured['invalidated'],
              'mid-run restart':measured['resumes']>0,'native compaction':measured['compactions']>0}
    return {name:'EXERCISED' if value else 'NOT EXERCISED' for name,value in observed.items()}


def longflow(args,server):
    require_snapshot()
    session=ROOT/'sessions'/('longflow-'+uuid.uuid4().hex[:8]); workspace=session/'workspace'; workspace.mkdir(parents=True)
    atomic(workspace/'ledger.py',LEDGER); atomic(workspace/'miniledger.py',CLI)
    atomic(session/'goal.txt',GOAL); atomic(session/'memory.txt','Constraints: offline, standard library only.\n')
    State(max_depth=PROFILES[args.profile][3]).save(session/'state.log')
    from visualize import open_view, view_channel, viewer_active
    environment=Path(os.environ.get('BACKTRACKIFY_VIEW_HOME',ROOT/'logs')).parent/'LongFlow'
    try:
        if getattr(args,'visualize',False) or viewer_active(view_channel(environment)):open_view(session,environment)
    except (OSError,ValueError) as e:status('VIEW','WARN',f'Cannot open viewer; execution continues: {e}')
    outcome,code='FAIL',1
    deadline=time.monotonic()+LONGFLOW_SECONDS
    def run_worker(label,stop='complete',resume=None,expected=(0,)):
        command=[sys.executable,'-B','-m','tests.longflow','--worker','--session',str(session),'--qwen',str(args.qwen),
                 '--profile',args.profile,'--context',str(args.context),'--url',server.url,'--stop',stop]
        if resume: command+=['--resume',str(resume)]
        if getattr(args,'agent_seconds',None):command+=['--agent-seconds',str(args.agent_seconds)]
        remaining=deadline-time.monotonic()
        check(remaining>0,'GPU LongFlow acceptance deadline reached; checkpoint and unresolved results retained')
        command+=['--run-seconds',str(remaining)]
        status('LONGFLOW','RUN',label+' | '+str(session))
        result=execute(command,ROOT,remaining+2*((getattr(args,'agent_seconds',None) or PROFILES[args.profile][2])+30),session/(label+'.log'))
        if result==2: raise OSError(f'Worker {label} blocked; inspect its log')
        check(result in expected,f'Worker {label} exited {result}; inspect its log')
        return result
    try:
        for stage in ('release','change'):
            if stage=='change':change_request(session)
            run_worker(stage+'-boundary',stop='failure',expected=(0,75))
            first=checkpoint(session,stage+'-boundary')
            if run_worker(stage+'-resume',stop='invalidation',resume=first,expected=(0,75))==75:
                second=checkpoint(session,stage+'-invalidated')
                run_worker(stage+'-repair',resume=second)
            check(State.load(session/'state.log').complete(),stage+' has unfinished TODOs')
        final=checkpoint(session,'complete')
        run_worker('resume-complete',resume=final)
        check((session/'state.log').read_bytes()==(final/'state.log').read_bytes(),'Completed restart changed committed state')
        acceptance(State.load(session/'state.log'),metrics(session))
        outcome,code='PASS',0
    except (OSError,ValueError,subprocess.TimeoutExpired,KeyboardInterrupt) as e:
        outcome='BLOCKED' if isinstance(e,(OSError,subprocess.TimeoutExpired,KeyboardInterrupt)) else 'FAIL'
        code=2 if outcome=='BLOCKED' else 1
        note(session,outcome,type(e).__name__,str(e))
    measured=metrics(session)
    detail=' | '.join(f'{k} {v}' for k,v in measured.items())
    covered=[f'COVERAGE | {name} | {value}' for name,value in coverage(measured).items()]
    atomic(session/'result.txt',outcome+' | '+detail+'\n'+'\n'.join(covered)+'\n')
    for line in covered:note(session,*line.split(' | '))
    status('LONGFLOW',outcome,detail)
    for name,value in coverage(measured).items():status('COVERAGE',value,name)
    return code


if __name__=='__main__':
    p=argparse.ArgumentParser(description='Internal worker; use test.ps1 -Gpu -LongFlow')
    p.add_argument('--worker',action='store_true',required=True); p.add_argument('--session',type=Path,required=True)
    p.add_argument('--qwen',type=Path,required=True); p.add_argument('--profile',choices=PROFILES,required=True)
    p.add_argument('--context',type=int,required=True); p.add_argument('--url',required=True)
    p.add_argument('--stop',choices=('failure','invalidation','complete'),default='complete'); p.add_argument('--resume',type=Path)
    p.add_argument('--run-seconds',type=float)
    p.add_argument('--agent-seconds',type=int)
    raise SystemExit(worker(p.parse_args()))
