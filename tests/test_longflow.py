"""CPU orchestration observations; simulated worker reports are not GPU evidence."""
from pathlib import Path
import os
import json
import io
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from harness import ROOT, Harness, State, Refused, AgentFailure, apply_plan, atomic, parse_plan, apply_report
from tests.longflow import (FlowHarness, Pause, acceptance, checkpoint, depth, metrics,
                            coverage, note, resume_check, change_request, GOAL, CHANGE)
from tests.test_harness import report, running


class Agent:
    def __init__(self,plans=(),responses=None):
        self.plans=iter(plans);self.responses=responses or {};self.calls=[];self.plan_attempts=[];self.owner='-'

    def call(self,role,prompt,workspace,structured=False,task_id=None):
        self.calls.append((role,prompt,task_id))
        if role=='PROMPT':
            self.plan_attempts.append(('native' if structured else 'text',True))
            value=next(self.plans)
            return {'operations':parse_plan(value)} if structured else value
        result=self.responses.get(task_id,report(task_id))
        return result.pop(0) if isinstance(result,list) else result


class LongFlowTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'sessions');self.session=Path(self.temp.name)
        (self.session/'workspace').mkdir()
        atomic(self.session/'goal.txt','Verify existing code, then repair observed defects.')
        atomic(self.session/'memory.txt','Offline.\n')
        State().save(self.session/'state.log')

    def tearDown(self):self.temp.cleanup()

    def planning(self):
        return ['ADD | 0 | TEST | checks | Existing behavior\nADD | 0 | ALGORITHM | source | Complete remaining work\nFINISH',
                'ADD | 1 | TEST | checks/a | Input cases\nADD | 1 | TEST | checks/b | Output cases\nADD | 2 | ALGORITHM | source | Input behavior\nADD | 2 | ALGORITHM | source | Output behavior\nFINISH',
                'ADD | 3 | TEST | checks/a | Valid inputs\nADD | 3 | TEST | checks/a | Invalid inputs\nADD | 4 | TEST | checks/b | Edge cases\nFINISH']

    def test_incremental_model_operations_create_depth_without_padding(self):
        agent=Agent(self.planning());h=FlowHarness(self.session,agent)
        for number in (1,2,3):
            h.plan();self.assertEqual(h.state.replans,number)
            self.assertTrue(h.state.closed)  # Valid plans are never reopened to fill a shape target.
            if number<3:
                h=FlowHarness(self.session,agent)  # Interrupted planning resumes its committed IDs.
        self.assertEqual(len(h.state.tasks),9);self.assertEqual(depth(h.state),3)
        self.assertIn('Allowed existing ADD parents: 0',agent.calls[0][1])
        self.assertIn('0, 1, 2',agent.calls[1][1]);self.assertIn('next ID 7',agent.calls[2][1])
        self.assertEqual(metrics(self.session)['schema'],3)
        self.assertEqual(metrics(self.session)['planning'],3)
        self.assertEqual(len(list((self.session/'planning').glob('*.log'))),3)
        edges=[tuple(map(int,row.split(' | ')[1:])) for row in (self.session/'flow.log').read_text(encoding='utf-8').splitlines() if row.startswith('BEFORE | ')]
        self.assertEqual(set(edges),{(t.id,t.parent) for t in h.state.tasks.values() if t.parent})
        self.assertEqual(len(edges),7)
        self.assertIn('parent 0 -> child depth 1',agent.calls[0][1])
        self.assertIn('DFS postorder',agent.calls[0][1])

    def test_dfs_branch_guard_and_delegation_trace_survive_restart(self):
        agent=Agent(self.planning());h=FlowHarness(self.session,agent)
        for _ in range(3):h.plan()
        before=h.path.read_bytes()
        with self.assertRaisesRegex(Refused,'DFS skipped'):h.step(h.state.tasks[5])
        self.assertEqual(h.path.read_bytes(),before)
        with patch('harness.execute',side_effect=AssertionError('Project command')):
            h.step(h.state.eligible())
            saved=checkpoint(self.session,'deep-branch')
            resume_check(self.session,saved)
            h=FlowHarness(self.session,agent)
            self.assertEqual(h.run(),0)
        starts=[row.split(' | ') for row in (self.session/'flow.log').read_text(encoding='utf-8').splitlines() if row.startswith('START | ')]
        self.assertEqual([int(row[1]) for row in starts],[7,8,3,9,4,1,5,6,2])
        self.assertEqual([row[5] for row in starts],['1>child:3>child:7','1>child:3>child:8','1>child:3','1>child:4>child:9','1>child:4','1','2>child:5','2>child:6','2'])
        self.assertEqual(metrics(self.session)['dfs'],h.state.actions)
        self.assertEqual(starts[0][6],'5,6');self.assertGreater(metrics(self.session)['priority'],0)

    def test_pause_restart_accepts_reports_without_running_project_commands(self):
        state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | a | Implement\nADD | 0 | TEST | spec.js | Verify\nFINISH'))
        state.actions=1;state.save(self.session/'state.log')
        agent=Agent(['ADD | 2 | ALGORITHM | source | Repair\nADD | 2 | TEST | spec.js | Check repair\nFINISH'],
                    {2:[report(2,'FAILED',reason='Observed defect'),report(2)]})
        h=FlowHarness(self.session,agent,stop='failure')
        with patch('harness.execute',side_effect=AssertionError('Project execution')):
            with self.assertRaises(Pause):h.run()
            saved=checkpoint(self.session,'accepted')
            completed=resume_check(self.session,saved)
            self.assertEqual(completed,[1])
            self.assertEqual(FlowHarness(self.session,agent).run(),0)
            final=checkpoint(self.session,'complete');resume_check(self.session,final)
            before=(self.session/'state.log').read_bytes()
            self.assertEqual(FlowHarness(self.session,agent).run(),0)
            self.assertEqual((self.session/'state.log').read_bytes(),before)
        self.assertEqual([c[2] for c in agent.calls if c[0]!='PROMPT'],[1,2,3,4,2]);self.assertEqual(metrics(self.session)['restarts'],2)
        self.assertEqual(metrics(self.session)['dfs'],5)

    def test_checkpoint_refuses_stale_state_files_and_duplicate_name(self):
        saved=checkpoint(self.session,'saved')
        with self.assertRaises(Refused):checkpoint(self.session,'saved')
        atomic(self.session/'workspace/a','changed')
        with self.assertRaisesRegex(Refused,'workspace'):resume_check(self.session,saved)
        State(closed=False,replans=1).save(self.session/'state.log')
        with self.assertRaisesRegex(Refused,'state'):resume_check(self.session,saved)

    def test_virtual_branch_and_invalidated_results_restart_then_get_own_reports(self):
        state=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Check | 4\nADD | 0 | ALGORITHM | | Independent\n'
                 'ADD | 0 | TEST | | Final check | 1\nADD | 0 | ALGORITHM | | Build\nADD | 4 | ALGORITHM | | Prepare\nFINISH'))
        state.save(self.session/'state.log')
        agent=Agent(['REOPEN | 4 | Later check exposed incomplete behavior\nADD | 4 | ALGORITHM | | Narrow repair\nFINISH'])
        h=FlowHarness(self.session,agent)
        with patch('harness.execute',side_effect=AssertionError('Project command')):
            self.assertEqual(h.run(),0)
            h=FlowHarness(self.session,agent,stop='invalidation')
            with self.assertRaises(Pause):h.plan()
            saved=checkpoint(self.session,'invalidated');resume_check(self.session,saved)
            h=FlowHarness(self.session,agent)
            self.assertEqual(h.state.tasks[1].kind,'invalidated');self.assertEqual(h.state.tasks[3].status,'PENDING')
            self.assertEqual(h.run(),0)
        self.assertEqual([c[2] for c in agent.calls if c[0]!='PROMPT'],[5,4,1,2,3,6,4,1,3])
        measured=metrics(self.session)
        self.assertGreater(measured['crossings'],0);self.assertGreater(measured['returns'],0)
        self.assertEqual(measured['reopens'],1);self.assertEqual(measured['forward'],2)
        self.assertEqual(measured['invalidated'],measured['rechecked']);self.assertEqual(measured['rechecked'],2)
        self.assertEqual(measured['dfs'],h.state.actions)

    def test_metrics_require_linked_repairs_and_original_parent_report(self):
        note(self.session,'REPORT',1,'TEST','FAILED','Checks exposed defect','Observed failure')
        note(self.session,'PLAN','native','ACCEPTED');note(self.session,'SPLIT',1,'3,4')
        note(self.session,'REPORT',99,'ALGORITHM','DONE','Unrelated work','')
        note(self.session,'REPORT',98,'TEST','DONE','Unrelated check','')
        self.assertEqual(metrics(self.session)['cycles'],0)
        note(self.session,'REPORT',3,'ALGORITHM','DONE','Repair complete','')
        note(self.session,'REPORT',4,'TEST','DONE','Child check complete','')
        self.assertEqual(metrics(self.session)['cycles'],0)  # Original parent is still unresolved.
        note(self.session,'REPORT',1,'TEST','DONE','Original scope verified by worker','')
        note(self.session,'CONTEXT','native-1');note(self.session,'CONTEXT','native-2')
        measured=metrics(self.session)
        self.assertEqual(measured['cycles'],1);self.assertEqual(measured['contexts'],2)
        note(self.session,'CONTEXT','native-1')
        with self.assertRaisesRegex(Refused,'context'):metrics(self.session)

    def test_acceptance_rejects_unfinished_parent_and_missing_targets(self):
        atomic(self.session/'goal.txt',GOAL)
        agent=Agent(['ADD | 0 | ALGORITHM | ledger.py | Complete library\nADD | 0 | TEST | tests | Check full MiniLedger brief\nFINISH'])
        h=FlowHarness(self.session,agent)
        context=h.plan_context()
        self.assertIn('Complete MiniLedger',context)
        for forced in ('8..12','at least 3 levels','step 1/4','later branch to exercise'):
            self.assertNotIn(forced,context)
        with patch('harness.execute',side_effect=AssertionError('Project command')):self.assertEqual(h.run(),0)
        for name in ('boundary','complete'):resume_check(self.session,checkpoint(self.session,name))
        for i in range(len(agent.calls)):note(self.session,'CONTEXT',f'controlled-{i}')
        measured=metrics(self.session)
        with self.assertRaisesRegex(Refused,'coverage missing'):acceptance(h.state,measured)
        self.assertEqual(measured['depth'],1);self.assertEqual(measured['nodes'],2)
        self.assertEqual(coverage(measured)['parent/child return'],'NOT EXERCISED')
        self.assertEqual(coverage(measured)['backtracking'],'NOT EXERCISED')
        for key in ('restarts','schema','contexts','dfs'):
            bad=dict(measured);bad[key]=0
            with self.subTest(key=key),self.assertRaises(Refused):acceptance(h.state,bad)
        for changes in ({'unresolved':1},{'invalidated':1,'rechecked':0}):
            with self.assertRaises(Refused):acceptance(h.state,dict(measured,**changes))
        h.state.phase='ready';h.state.tasks[1].status='PENDING'
        with self.assertRaises(Refused):acceptance(h.state,measured)

    def test_single_worker_completion_cannot_pass_longflow_or_gain_harness_tasks(self):
        from types import SimpleNamespace
        from tests.longflow import longflow
        for window_error in (False,True):
            with self.subTest(window_error=window_error):
                agent=Agent(['ADD | 0 | ALGORITHM | ledger.py | Complete MiniLedger\nFINISH'])
                workers=[]
                def execute_worker(command,cwd,timeout,log):
                    workers.append(command)
                    session=Path(command[command.index('--session')+1])
                    if '--resume' in command:resume_check(session,Path(command[command.index('--resume')+1]))
                    before=len(agent.calls)
                    code=FlowHarness(session,agent,stop=command[command.index('--stop')+1]).run()
                    for i in range(before,len(agent.calls)):note(session,'CONTEXT',f'controlled-{i}')
                    return code
                args=SimpleNamespace(profile='MID',context=32768,qwen=Path('unused-pinned-cli'),visualize=True)
                window=(patch('visualize.open_view',side_effect=OSError('Cannot create terminal')) if window_error
                        else patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')))
                with patch('tests.longflow.ROOT',self.session),patch('tests.longflow.require_snapshot'),window,\
                     patch('tests.longflow.execute',side_effect=execute_worker),patch('tests.longflow.status') as messages:
                    self.assertEqual(longflow(args,SimpleNamespace(url='http://unused-controlled-backend')),1)
                session=Path(workers[0][workers[0].index('--session')+1])
                self.assertEqual(len(workers),2);self.assertTrue(State.load(session/'state.log').complete())
                self.assertEqual(len(State.load(session/'state.log').tasks),1)
                result=(session/'result.txt').read_text(encoding='utf-8')
                self.assertTrue(result.startswith('FAIL | '))
                self.assertIn('COVERAGE | forward invalidation | NOT EXERCISED',result)
                self.assertIn('COVERAGE | cross-branch needs | NOT EXERCISED',result)
                if window_error:
                    self.assertTrue(any(call.args[:2]==('VIEW','WARN') for call in messages.call_args_list))
                elif os.name=='nt':
                    self.assertTrue(list((session/'history').glob('*.log')))
                    self.assertTrue(State.load(sorted((session/'history').glob('*.log'))[-1]).complete())

    def test_staged_driver_requires_observed_recovery_and_resumes_after_one_hour(self):
        from types import SimpleNamespace
        from tests.longflow import longflow
        plans=['ADD | 0 | ALGORITHM | ledger.py | Build library\nADD | 0 | TEST | tests | Verify | 1\n'
               'ADD | 0 | TEST | | Check release | 2\nFINISH',
               'ADD | 0 | TEST | tests | Check reusable validation API | 1\nFINISH',
               'REOPEN | 1 | New validation API must be integrated\n'
               'ADD | 0 | ALGORITHM | ledger_rules.py | Build reusable validation\n'
               'ADD | 1 | ALGORITHM | ledger.py | Integrate validation | 5\n'
               'NEEDS | 1 | 5\nEDIT | 4 | Recheck reusable validation API\nFINISH']
        agent=Agent(plans,{4:[report(4,'FAILED',reason='Reusable validation module is missing',related_tasks=[1]),report(4)]})
        clock=[0];workers=[]
        def execute_worker(command,cwd,timeout,log):
            workers.append(command);session=Path(command[command.index('--session')+1])
            if '--resume' in command:resume_check(session,Path(command[command.index('--resume')+1]))
            h=FlowHarness(session,agent,stop=command[command.index('--stop')+1],
                          run_seconds=float(command[command.index('--run-seconds')+1]))
            before=len(agent.calls)
            try:code=h.run()
            except Pause:code=75
            for i in range(before,len(agent.calls)):note(session,'CONTEXT',f'controlled-{i}')
            # A controlled native trace fixture tests accounting, not GPU/model compaction.
            atomic(session/'turns/controlled/native/projects/p/chats/c.jsonl',json.dumps({'subtype':'chat_compression'})+'\n')
            clock[0]+=3900 if len(workers)==1 else 10
            return code
        args=SimpleNamespace(profile='MID',context=32768,qwen=Path('unused'),visualize=False)
        with patch('tests.longflow.ROOT',self.session),patch('tests.longflow.require_snapshot'),\
             patch('tests.longflow.time.monotonic',side_effect=lambda:clock[0]),\
             patch('tests.longflow.execute',side_effect=execute_worker):
            self.assertEqual(longflow(args,SimpleNamespace(url='http://unused-controlled-backend')),0)
        self.assertEqual([call[2] for call in agent.calls if call[0]!='PROMPT'],[1,2,3,4,5,6,1,2,3,4])
        prompt=next(call[1] for call in agent.calls if call[0]=='PROMPT' and 'Reusable validation module is missing' in call[1])
        self.assertIn('related tasks: 1',prompt);self.assertIn('REOPEN candidates',prompt)
        self.assertGreater(float(workers[1][workers[1].index('--run-seconds')+1]),0)
        session=Path(workers[0][workers[0].index('--session')+1])
        measured=metrics(session)
        self.assertTrue(all(value=='EXERCISED' for value in coverage(measured).values()))
        self.assertGreater(measured['transitive'],0);self.assertGreater(measured['parents'],0)
        self.assertEqual(measured['invalidated'],measured['rechecked'])
        acceptance(State.load(session/'state.log'),measured)
        for key in ('compactions','reopens','forward','transitive','parents','resumes','recoveries','crossings','returns','replans','deepened'):
            with self.subTest(missing=key),self.assertRaisesRegex(Refused,'coverage missing'):
                acceptance(State.load(session/'state.log'),dict(measured,**{key:0}))

    def test_follow_up_changes_only_goal_and_planning_phase_not_worker_results(self):
        h=FlowHarness(self.session,Agent(['ADD | 0 | ALGORITHM | ledger.py | Build\nADD | 0 | TEST | | Verify | 1\nFINISH']))
        self.assertEqual(h.run(),0)
        before=State.load(h.path);workspace=(self.session/'workspace')
        atomic(workspace/'ledger.py','unchanged release\n')
        original=(self.session/'goal.txt').read_text(encoding='utf-8')
        change_request(self.session)
        after=State.load(h.path)
        self.assertFalse(after.closed);self.assertEqual(after.phase,'ready')
        self.assertEqual(before.tasks,after.tasks);self.assertEqual(before.actions,after.actions)
        self.assertEqual((workspace/'ledger.py').read_text(encoding='utf-8'),'unchanged release\n')
        self.assertEqual((self.session/'goal.txt').read_text(encoding='utf-8'),original+'\n'+CHANGE)
        self.assertIn('ledger_rules.py',Harness(self.session,None).plan_context())
        with self.assertRaises(Refused):change_request(self.session)

    def test_complete_restart_in_new_python_process_without_model_calls(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | TEST | check | Verification\nFINISH'))
        s.tasks[1].status='DONE';s.tasks[1].result='Accepted report';s.tasks[1].attempts=1;s.phase='complete';s.actions=1
        s.save(self.session/'state.log')
        before=(self.session/'state.log').read_bytes()
        script=('from pathlib import Path; from tests.longflow import FlowHarness; '
                f'raise SystemExit(FlowHarness(Path({str(self.session)!r}),None).run())')
        child=subprocess.run([sys.executable,'-B','-c',script],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stdout+child.stderr)
        self.assertEqual((self.session/'state.log').read_bytes(),before)

    def test_committed_planning_observation_is_recovered_without_replaying_adds(self):
        h=FlowHarness(self.session,Agent(self.planning()))
        h.plan();ids=list(h.state.tasks)
        (self.session/'planning/1.log').unlink()  # Observation lost; atomic state still includes the step.
        h=FlowHarness(self.session,Agent(self.planning()[1:]))
        self.assertTrue((self.session/'planning/1.log').exists())
        h.plan();self.assertEqual(list(h.state.tasks)[:2],ids)
        self.assertEqual(h.state.replans,2)

    def test_planning_keeps_the_core_bound_without_padding_tasks(self):
        h=FlowHarness(self.session,Agent(['ADD | 0 | TEST | check | Verify\nFINISH']+['FINISH']*7))
        for _ in range(8):h.plan()
        with self.assertRaisesRegex(Refused,'Replanning limit'):h.plan()
        self.assertEqual(len(h.state.tasks),1);self.assertTrue(h.state.closed)

    def test_forward_reference_is_atomic_and_restart_guard_rejects_done_replay(self):
        state=State();before=state.render()
        with self.assertRaises(Refused):apply_plan(state,parse_plan('ADD | 2 | TEST | check | Invalid forward reference\nFINISH'))
        self.assertEqual(state.render(),before)
        state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | source | Work\nFINISH'))
        state.actions=1;state.tasks[1].status='DONE';state.tasks[1].attempts=1;state.tasks[1].result='Accepted report';state.save(self.session/'state.log')
        h=FlowHarness(self.session,Agent())
        with self.assertRaisesRegex(Refused,'replayed'):h.step(h.state.tasks[1])

    def test_invalid_initial_plan_is_never_filled_with_harness_tasks(self):
        agent=Agent(['ADD | 0 | TEST | | Audit\nADD | 0 | TEST | | Acceptance\nADD | 2 | TEST | | Write tests | 1,2\nFINISH',
                     'No tests exist yet. Plan ...\nADD | 0 | TEST | | Audit\nFINISH','ADD | 0 | TEST | | Invalid | 1\nFINISH'])
        h=FlowHarness(self.session,agent)
        with self.assertRaises(Refused):h.plan()
        self.assertEqual(len(h.state.tasks),0);self.assertEqual(h.state.next_id,1)
        self.assertEqual(metrics(self.session)['invalid'],3)
        self.assertIn('2 -> 3 -> 2',agent.calls[1][1])

    def test_runtime_has_no_project_execution_or_old_acceptance_paths(self):
        import ast,inspect,harness
        tree=ast.parse(inspect.getsource(Harness))
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)]
        forbidden={'execute','start_process','verify','test_merge','fingerprint','run','Popen','check_output'}
        for call in calls:
            name=call.func.id if isinstance(call.func,ast.Name) else call.func.attr if isinstance(call.func,ast.Attribute) else ''
            self.assertNotIn(name,forbidden)
        for name in ('verify','CheckFailure','fingerprint','test_merge','check_summary','has_marker'):
            self.assertFalse(hasattr(harness,name),name)
        self.assertNotIn('VERIFIED',harness.STATUSES)

    def test_nested_context_split_turn_limit_checkpoint_and_explicit_parent_continuation(self):
        scope='Integrate child outcomes and fix only remaining ledger gaps'
        initial=('ADD | 0 | ALGORITHM | ledger.py | Integrate library\n'
            'ADD | 1 | ALGORITHM | ledger.py | Dates\nADD | 1 | ALGORITHM | ledger.py | CSV\n'
            'ADD | 1 | ALGORITHM | ledger.py | Summary\nADD | 0 | ALGORITHM | cli.py | CLI | 1\n'
            'ADD | 0 | TEST | | Check library | 1\nADD | 0 | TEST | | Check CLI | 5\n'
            'ADD | 0 | TEST | | Release | 1,5,6,7\nFINISH')
        plans=[initial,'ADD | 1 | ALGORITHM | ledger.py | Replay example\nADD | 1 | ALGORITHM | ledger.py | '+scope+'\nFINISH',
            'EDIT | 10 | '+scope+'\nADD | 10 | ALGORITHM | ledger.py | Bounds\n'
            'ADD | 10 | ALGORITHM | ledger.py | Mutation\nADD | 10 | ALGORITHM | ledger.py | Strictness\nFINISH',
            'EDIT | 10 | '+scope+'\nFINISH']
        calls=[];contexts=[];planned=[];plan_iter=iter(plans)
        class Worker:
            owner='-';plan_attempts=[]
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    planned.append(prompt);_.plan_attempts.append(('native',True))
                    return {'operations':parse_plan(next(plan_iter))}
                calls.append(task_id);contexts.append((task_id,prompt))
                if task_id==1 and calls.count(1)==1:raise AgentFailure('context','Native compaction returned malformed snapshot XML')
                if task_id==10:
                    number=calls.count(10)
                    if number==1:raise AgentFailure('context','Native compaction returned malformed snapshot XML')
                    if number==2:
                        atomic(workspace/'ledger.py',(workspace/'ledger.py').read_text()+'\nretained strict fix')
                        raise AgentFailure('limit','Qwen session-turn budget exhausted before final report')
                    self_test.assertIn('details: Confirmed strict CSV gap; repair remains',prompt)
                    self_test.assertIn('10-2-',prompt)  # The unaccepted repair is retained, not silently promoted.
                    self_test.assertNotIn('retained strict fix',(workspace/'ledger.py').read_text())
                if role=='ALGORITHM':atomic(workspace/'ledger.py',(workspace/'ledger.py').read_text()+f'\naccepted task {task_id}')
                return report(task_id,reason='Confirmed strict CSV gap; repair remains' if task_id==13 else 'No open findings')
        self_test=self;agent=Worker();atomic(self.session/'workspace/ledger.py','initial')
        h=FlowHarness(self.session,agent);h.plan()
        with patch('harness.execute',side_effect=AssertionError('No independent project command')):
            while not (10 in h.state.tasks and h.state.tasks[10].status=='FAILED' and h.state.tasks[10].attempts==2):
                if not h.state.closed or any(t.status in {'FAILED','BLOCKED'} for t in h.state.tasks.values()):h.plan()
                else:h.step(h.state.eligible())
            before={id:h.state.tasks[id] for id in (2,3,4,9,11,12,13)}
            saved=checkpoint(self.session,'nested-limit');resume_check(self.session,saved)
            h=FlowHarness(self.session,agent);self.assertEqual(h.run(),0)
        self.assertEqual(calls,[2,3,4,1,9,10,11,12,13,10,10,1,5,6,7,8])
        self.assertEqual(h.state.tasks[10].summary,scope);self.assertEqual(h.state.tasks[10].attempts,3)
        self.assertEqual(h.state.tasks[1].attempts,2);self.assertTrue(h.state.complete())
        self.assertEqual(before,{id:h.state.tasks[id] for id in before})
        self.assertIn('Parent continuation EDIT candidates (exact unchanged summary only): 10',planned[-1])
        self.assertNotIn('Task 10: split into smaller independent children',planned[-1])
        self.assertEqual(metrics(self.session)['dfs'],16)

    def test_unicode_console_cannot_reclassify_committed_done_or_lose_flow_reports(self):
        class Worker:
            owner='-'
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                return report(task_id,summary='Strict validation \u91d1\u989d verified',reason='UTF-8 \u6f22 preserved')
        state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | ledger.py | Implement\nADD | 0 | TEST | | Check | 1\nFINISH'))
        state.save(self.session/'state.log')
        output=io.BytesIO()
        console=io.TextIOWrapper(output,encoding='cp1252',errors='strict',write_through=True)
        try:
            with patch('sys.stdout',console):self.assertEqual(FlowHarness(self.session,Worker()).run(),0)
            self.assertIn(b'\\u91d1',output.getvalue())
        finally:console.detach()
        state=State.load(self.session/'state.log');self.assertTrue(state.complete())
        self.assertIn('\u91d1\u989d',state.tasks[1].result);self.assertIn('\u6f22',state.tasks[1].reason)
        self.assertFalse(any('[workspace]' in e for e in state.events))
        self.assertEqual(metrics(self.session)['done'],2)

    def test_failed_check_after_parent_turn_limit_restart_and_bounded_replanning(self):
        scope='Integrate library, reconcile independent checks and report remaining fixes'
        initial='ADD | 0 | ALGORITHM | ledger.py | '+scope+'\nADD | 1 | ALGORITHM | ledger.py | Implement library\nFINISH'
        extend=('EDIT | 1 | '+scope+'\nNEEDS | 1 | 4\nADD | 0 | ALGORITHM | cli.py | Implement CLI\n'
            'ADD | 0 | TEST | tests/check.py | Verify library\nADD | 0 | TEST | | Check CLI | 3\n'
            'ADD | 0 | TEST | | Release | 1,3,4,5\nFINISH')
        repair='ADD | 4 | ALGORITHM | ledger.py | Repair reported validation defect\nFINISH'
        bad='ADD | 4 | ALGORITHM | ledger.py | Repair validation\nEDIT | 4 | Recheck library\nFINISH'
        for native_context,fallback_valid in ((True,False),(True,True),(False,True)):
            with self.subTest(context=native_context,valid_fallback=fallback_valid):
                session=self.session/f'{native_context}-{fallback_valid}';(session/'workspace').mkdir(parents=True)
                atomic(session/'goal.txt','Complete and independently verify library and CLI.');atomic(session/'memory.txt','Offline.')
                State().save(session/'state.log');calls=[];prompts=[];rounds=iter((initial,extend));plan_calls=[]
                class Worker:
                    owner='-';plan_attempts=[]
                    def call(_,role,prompt,workspace,structured=False,task_id=None):
                        if role=='PROMPT':
                            plan_calls.append(structured);prompts.append(prompt)
                            if len(plan_calls)<=2:
                                _.plan_attempts.append(('native',True));return {'operations':parse_plan(next(rounds))}
                            _.plan_attempts.append(('native' if structured else 'text',not (structured and native_context)))
                            if structured:
                                if native_context:raise AgentFailure('context','Context headroom exhausted: prompt 22640 + generation 11264 + safety 256 > 32768')
                                return {'operations':parse_plan(bad)}
                            return repair if fallback_valid else bad
                        calls.append(task_id)
                        if task_id==1 and calls.count(1)==1:
                            atomic(workspace/'unaccepted','partial parent work');raise AgentFailure('limit','Qwen session-turn budget exhausted')
                        if task_id in (2,7):atomic(workspace/'ledger.py','fixed' if task_id==7 else 'original implementation')
                        if task_id==4 and calls.count(4)==1:
                            atomic(workspace/'tests/check.py','Regression for trailing newline')
                            return report(4,'FAILED',reason='Three trailing-newline validation failures',related_tasks=[1])
                        return report(task_id)
                agent=Worker();h=FlowHarness(session,agent,stop='failure')
                with self.assertRaises(Pause):h.run()
                self.assertEqual(calls,[2,1,4]);before=h.state.tasks.copy()
                saved=checkpoint(session,'release-boundary');resume_check(session,saved);h=FlowHarness(session,agent)
                self.assertEqual(h.run(),0 if fallback_valid else 2)
                self.assertEqual(h.state.tasks[2],before[2]);self.assertFalse((h.workspace/'unaccepted').exists())
                self.assertIn('Three trailing-newline validation failures',prompts[-1]);self.assertIn('1 | 0 | ALGORITHM | PENDING | attempts 1',prompts[-1])
                if fallback_valid:
                    self.assertEqual(calls,[2,1,4,7,4,1,3,5,6]);self.assertTrue(h.state.complete())
                    self.assertEqual(h.state.tasks[1].summary,scope);self.assertEqual(h.state.tasks[1].attempts,2)
                    self.assertEqual((h.workspace/'ledger.py').read_text(),'fixed')
                else:
                    self.assertEqual(h.state.tasks,before);self.assertEqual(calls,[2,1,4])
                    self.assertIn('native: Context headroom exhausted',h.state.reason)
                    self.assertIn('text: EDIT task 4: status WAITING; live children [7]',h.state.reason)
                    self.assertEqual(State.load(h.path).tasks,before);self.assertFalse(h.state.complete())

    def test_failed_diagnostic_dependency_can_be_reoriented_without_discarding_its_result(self):
        initial=('ADD | 0 | ALGORITHM | consumer.py | Integrate consumer\nADD | 1 | ALGORITHM | consumer.py | Implement consumer\n'
            'ADD | 0 | TEST | | Check consumer | 1\nADD | 0 | TEST | | Release | 3\nADD | 0 | TEST | | Unrelated check\nFINISH')
        base=apply_plan(State(),parse_plan(initial))
        while task:=base.eligible():base=apply_report(running(base,task.id),task.id,report(task.id))
        followup=('REOPEN | 2 | Delegate validation to reusable module\n'
            'ADD | 0 | TEST | tests/check_api.py | Check new public contract\n'
            'ADD | 0 | ALGORITHM | api.py | Create public API | 6\n'
            'ADD | 0 | TEST | tests/check_api.py | Independently check new API | 7\nNEEDS | 2 | 8\nFINISH')
        waiting=apply_plan(base,parse_plan(followup))  # Valid acyclic plan; missing-module semantics belong to agents.
        self.assertEqual(waiting.eligible().id,6)
        self.assertEqual([waiting.tasks[i].status for i in (1,2,3,4,5)],['WAITING','PENDING','PENDING','PENDING','DONE'])
        recover=('NEEDS | 7 | \nNEEDS | 6 | 7\nNEEDS | 8 | 6\n'
            'EDIT | 6 | Check new public contract\nFINISH')
        for corrected in (False,True,'native'):
            with self.subTest(corrected=corrected):
                session=self.session/str(corrected);(session/'workspace').mkdir(parents=True)
                atomic(session/'goal.txt','Add and independently verify a reusable API, then integrate it.')
                atomic(session/'memory.txt','Preserve consumer behavior.');waiting.save(session/'state.log')
                calls=[];prompts=[];plan_calls=[]
                class Worker:
                    owner='-';plan_attempts=[]
                    def call(_,role,prompt,workspace,structured=False,task_id=None):
                        if role=='PROMPT':
                            prompts.append(prompt);plan_calls.append(structured);_.plan_attempts.append(('native' if structured else 'text',True))
                            if structured:
                                return {'operations':parse_plan(recover if corrected=='native' and len(plan_calls)==3 else 'NEEDS | 7 | \nFINISH')}
                            return recover if corrected is True else 'Confirmed: the contract test exists.\n'+recover
                        calls.append(task_id)
                        if task_id==6:
                            atomic(workspace/'tests/check_api.py','from api import parse_value\n')
                            if not (workspace/'api.py').exists():return report(6,'FAILED',reason="ModuleNotFoundError: No module named 'api'")
                        if task_id==7:atomic(workspace/'api.py','def parse_value(text): return int(text)\n')
                        return report(task_id)
                agent=Worker();h=FlowHarness(session,agent,stop='failure')
                with self.assertRaises(Pause):h.run()
                failed=h.state.tasks.copy();before=h.state.render()
                with self.assertRaisesRegex(Refused,'Circular children/needs'):
                    apply_plan(h.state,parse_plan('NEEDS | 6 | 7\nEDIT | 6 | Check new public contract\nFINISH'))
                self.assertEqual(h.state.render(),before)
                saved=checkpoint(session,'diagnostic-failure');resume_check(session,saved)
                h=FlowHarness(session,agent)
                with patch('harness.execute',side_effect=AssertionError('No harness-owned project command')):
                    self.assertEqual(h.run(),0 if corrected else 2)
                self.assertEqual(plan_calls,[True,False] if corrected is True else [True,False,True]);self.assertEqual(h.state.tasks[5],base.tasks[5])
                self.assertIn('FAILED/BLOCKED tasks that must be resolved before FINISH (never skipped as history): 6',prompts[0])
                self.assertIn('needs requires an accepted DONE result, not an attempted diagnostic',prompts[0])
                self.assertIn('Unresolved FAILED/BLOCKED tasks: 6',prompts[1])
                self.assertEqual(State.load(saved/'state.log').tasks[6].status,'FAILED')
                if corrected:
                    self.assertEqual(calls,[6,7,6,8,2,1,3,4]);self.assertTrue(h.state.complete())
                    self.assertEqual(h.state.tasks[6].attempts,2);self.assertEqual(h.state.tasks[2].attempts,2)
                    self.assertEqual(h.state.tasks[6].summary,waiting.tasks[6].summary)
                    self.assertEqual(h.state.tasks[8].needs,[6]);self.assertEqual(h.state.tasks[6].needs,[7])
                else:
                    self.assertEqual(calls,[6]);self.assertEqual(h.state.tasks,failed)
                    self.assertEqual(h.state.tasks[7].needs,[6])  # Invalid native edge removal was never committed.
                    self.assertIn('text: Invalid plan line: Confirmed:',h.state.reason)

    def test_diagnostic_failure_can_gain_a_repair_child_then_give_its_own_report(self):
        calls=[]
        class Worker:
            owner='-';plan_attempts=[]
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    _.plan_attempts.append(('native',True))
                    return {'operations':parse_plan('ADD | 1 | ALGORITHM | api.py | Implement missing public API\nFINISH')}
                calls.append(task_id)
                if task_id==1 and not (workspace/'api.py').exists():
                    atomic(workspace/'tests/check_api.py','from api import parse_value\n')
                    return report(1,'FAILED',reason='Required public API is missing')
                if task_id==3:atomic(workspace/'api.py','def parse_value(text): return int(text)\n')
                return report(task_id)
        apply_plan(State(),parse_plan('ADD | 0 | TEST | tests/check_api.py | Verify public contract\n'
            'ADD | 0 | TEST | | Release | 1\nFINISH')).save(self.session/'state.log')
        h=FlowHarness(self.session,Worker());self.assertEqual(h.run(),0)
        self.assertEqual(calls,[1,3,1,2]);self.assertTrue(h.state.complete());self.assertEqual(h.state.tasks[1].attempts,2)
        self.assertEqual(h.state.tasks[1].summary,'Verify public contract')


if __name__=='__main__':unittest.main()
