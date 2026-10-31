"""Optional plain-text checkpoint history and a read-only Windows terminal viewer."""
import argparse
import base64
import copy
from contextlib import ExitStack
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata

from harness import ROOT, State, Refused, atomic, lock, LIMIT_ATTEMPTS


def record(path):
    """Called only after state.log was committed, and only for opted-in sessions."""
    folder=path.parent/'history'
    paths=sorted(folder.glob('*.log'))
    raw=path.read_text(encoding='utf-8')
    if paths and paths[-1].read_text(encoding='utf-8').rsplit('# SAVED | ',1)[0]==raw: return
    number=int(paths[-1].stem)+1 if paths else 1
    atomic(folder/f'{number:06d}.log',raw+f'# SAVED | {path.stat().st_mtime:.6f}\n')


def view_channel(environment,session=None,owner=None):
    home=Path(os.environ.get('BACKTRACKIFY_VIEW_HOME',ROOT/'logs')).resolve()
    if owner is not None:
        # One managed native CLI owns many short-lived task worker processes.
        identity=str(Path(environment).resolve()).casefold()+'|interactive:'+str(Path(owner).resolve()).casefold()
        return home/('live-tree-'+hashlib.sha256(identity.encode()).hexdigest()[:20]+'.log')
    if session:
        try:
            row=next(row for row in (session/'view.log').read_text(encoding='utf-8').splitlines() if row.startswith('ENV | '))
            saved=Path(row[6:]).resolve()
            if saved.parent==home and saved.name.startswith('live-tree-') and viewer_active(saved) and following(saved)==session.resolve():
                return saved  # An explicit --session resume keeps its existing working environment.
        except (OSError,ValueError,StopIteration):pass
    terminal=os.environ.get('WT_SESSION','')
    if not terminal and os.name=='nt':
        ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p
        terminal=str(ctypes.windll.kernel32.GetConsoleWindow() or '')
    # Headless invocations cannot establish a shared interactive environment safely.
    key=terminal or f'process-{os.getpid()}'
    identity=str(Path(environment).resolve()).casefold()+'|'+key
    return home/('live-tree-'+hashlib.sha256(identity.encode()).hexdigest()[:20]+'.log')


def viewer_active(channel):
    if not channel.is_file():return False
    try:
        with lock(channel.with_suffix('.lock')):return False
    except Refused:return True


def following(channel):
    """Only the producer for this environment may publish its next persisted session."""
    target=Path(channel.read_text(encoding='utf-8').strip()).resolve()
    if f'ENV | {channel.resolve()}' not in (target/'view.log').read_text(encoding='utf-8').splitlines():
        raise ValueError('Viewer environment does not match the published session')
    State.load(target/'state.log')
    return target


def open_view(session,environment=None,owner=None):
    if os.name!='nt': raise ValueError('--visualize requires a Windows terminal')
    channel=view_channel(environment or ROOT,session,owner)
    (session/'history').mkdir(exist_ok=True)
    record(session/'state.log')
    atomic(session/'view.log',f'OWNER | {os.getpid()} | {process_stamp(os.getpid())}\nENV | {channel}\n')
    active=viewer_active(channel)
    atomic(channel,str(session.resolve())+'\n')
    if active:return
    # Explorer owns the display process, outside the worker/server kill-on-close Job.
    command=subprocess.list2cmdline(['-B',str(Path(__file__).resolve()),str(session.resolve()),'--follow',str(channel)])
    quote=lambda value:"'"+str(value).replace("'","''")+"'"
    script=("$ErrorActionPreference='Stop'; $shell=New-Object -ComObject Shell.Application; $hwnd=0; "
            "$desktop=$shell.Windows().FindWindowSW(0,0,8,[ref]$hwnd,1); "
            "if (!$desktop) { throw 'Interactive Explorer desktop is unavailable' }; "
            "$desktop.Document.Application.ShellExecute("+quote(sys.executable)+','+quote(command)+','+
            quote(Path(__file__).resolve().parent)+",'open',1)")
    try:
        result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-EncodedCommand',
                               base64.b64encode(script.encode('utf-16le')).decode()],capture_output=True,
                              creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    except subprocess.TimeoutExpired as error:raise OSError('Persistent viewer desktop launch timed out') from error
    if result.returncode:raise OSError('Persistent viewer desktop launch failed: '+result.stderr.decode(errors='replace')[:400])
    if owner is not None:
        # Interactive tasks are serialized. Claim the viewer before this task can
        # finish, so an immediately following task cannot launch a second window.
        deadline=time.monotonic()+5
        while not viewer_active(channel):
            if time.monotonic()>=deadline:raise OSError('Live tree did not claim its viewer lock; execution continues and the next task may retry')
            time.sleep(.05)


def process_stamp(pid):
    """Creation time plus liveness, so PID reuse cannot keep a finished run looking active."""
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.restype=ctypes.c_void_p
    api.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=api.OpenProcess(0x1000,False,int(pid))
    if not handle:return ''
    try:
        code=ctypes.c_ulong()
        if not api.GetExitCodeProcess(ctypes.c_void_p(handle),ctypes.byref(code)) or code.value!=259:return ''
        born,ended,kernel,user=(ctypes.c_uint64() for _ in range(4))
        if not api.GetProcessTimes(ctypes.c_void_p(handle),ctypes.byref(born),ctypes.byref(ended),ctypes.byref(kernel),ctypes.byref(user)):return ''
        return str(born.value)
    finally:api.CloseHandle(handle)


def owner_ended(session):
    try:
        row=next(row for row in (session/'view.log').read_text(encoding='utf-8').splitlines() if row.startswith('OWNER | '))
        tag,pid,born=row.split(' | ')
        return tag=='OWNER' and bool(born) and process_stamp(int(pid))!=born
    except (OSError,ValueError,StopIteration):return False


def acceptance_result(session):
    try:
        result=(session/'result.txt').read_text(encoding='utf-8').split(' | ',1)[0].strip()
        return result if result in {'PASS','FAIL','BLOCKED'} else ''
    except OSError:return ''


def read_history(session):
    frames=[]
    paths=sorted((session/'history').glob('*.log'))
    for path in paths:
        raw=path.read_text(encoding='utf-8')
        stamp=float(raw.rsplit('# SAVED | ',1)[1].strip())
        frames.append((stamp,State.load(path)))
    current=State.load(session/'state.log')
    if not frames or frames[-1][1].render()!=current.render():
        frames.append(((session/'state.log').stat().st_mtime,current))
    return frames


def duration(seconds):
    seconds=max(0,int(seconds))
    hours,seconds=divmod(seconds,3600)
    minutes,seconds=divmod(seconds,60)
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes}:{seconds:02d}'


def work_times(frames,index,until):
    elapsed,starts,counts={}, {}, {}
    previous={}
    for number,(stamp,state) in enumerate(frames[:index+1]):
        for id,task in state.tasks.items():
            was=previous.get(id)
            if task.status=='RUNNING' and (was is None or was.status!='RUNNING'):
                if number: counts[id]=counts.get(id,0)+1
                starts[id]=stamp
            elif task.status!='RUNNING' and id in starts:
                elapsed[id]=elapsed.get(id,0)+max(0,stamp-starts.pop(id))
        previous=state.tasks
    for id,start in starts.items():elapsed[id]=elapsed.get(id,0)+max(0,until-start)
    state=frames[index][1]
    return {id:duration(elapsed.get(id,0))+('?' if task.attempts>counts.get(id,0) else '')
            for id,task in state.tasks.items()}


def clean(value):
    return ''.join(c if c.isprintable() or c=='\u200d' else ' ' for c in str(value))


SGR=re.compile(r'(\x1b\[[0-9;]*m)')
BACKGROUND='48;2;16;21;29'
RESET=f'\x1b[0;{BACKGROUND};38;2;203;213;225m'
MUTED='38;2;148;163;184'
CYAN='38;2;56;189;248'
GREEN='38;2;34;197;94'
AMBER='38;2;251;191;36'
RED='38;2;255;84;109'
BORDER='38;2;52;66;82'
COLORS={'DONE':GREEN,'RUNNING':CYAN,'WAITING':AMBER,'FAILED':RED,'BLOCKED':RED,'PENDING':MUTED,'DROPPED':RED}
HEADER_LINES,FOOTER_LINES=7,5


def glyphs(text):
    """Keep combining marks, common emoji sequences and wide characters together."""
    group=''
    for c in text:
        attached=(unicodedata.category(c) in {'Mn','Me'} or c=='\u200d' or
                  '\U0001f3fb'<=c<='\U0001f3ff' or group.endswith('\u200d') or
                  (len(group)==1 and '\U0001f1e6'<=group<='\U0001f1ff' and '\U0001f1e6'<=c<='\U0001f1ff'))
        if group and not attached:
            yield group;group=''
        group+=c
    if group:yield group


def columns(text):
    total=0
    for group in glyphs(SGR.sub('',text)):
        widths=[0 if unicodedata.category(c) in {'Mn','Me','Cf'} else
                2 if unicodedata.east_asian_width(c) in {'W','F'} else 1 for c in group]
        total+=(max(widths,default=0) if '\u200d' in group or any('\U0001f3fb'<=c<='\U0001f3ff' for c in group)
                else max(2,sum(widths)) if '\ufe0f' in group or '\u20e3' in group else sum(widths))
    return total


def shorten(text,width,marker='…'):
    width=max(0,width)
    if columns(text)<=width:return text
    if width==0:return ''
    result=[];used=0
    for part in SGR.split(text):
        if SGR.fullmatch(part):result.append(part);continue
        for group in glyphs(part):
            size=columns(group)
            if used+size>width-columns(marker):
                return ''.join(result)+marker+(RESET if '\x1b' in text else '')
            result.append(group);used+=size
    return ''.join(result)


def wrapped(text,width):
    text=clean(text)
    while columns(text)>width:
        prefix=shorten(text,width,'')
        if not prefix:
            yield '…';text=text[len(next(glyphs(text))):];continue
        split=prefix.rfind(' ')
        count=split if split>len(prefix)//2 else len(prefix)
        yield text[:count].rstrip();text=text[count:].lstrip()
    if text:yield text


def task_ids(ids):
    return ','.join(f'#{id}' for id in sorted(ids)) or 'none'


def plan_changes(before,after):
    marks,changes={},[]
    split={t.parent for id,t in after.tasks.items() if id not in before.tasks and
           t.status!='DROPPED' and t.parent in before.tasks}
    for id,t in sorted(after.tasks.items()):
        old=before.tasks.get(id)
        tags=[]
        if old is None:
            if t.status=='DROPPED':
                tags.append('DROP');changes.append(f'− DROP #{id} (added in this batch): {t.reason}')
            else:
                tags.append('NEW');changes.append(f'+ ADD #{id}'+(f' under #{t.parent}' if t.parent else ''))
        else:
            if old.status!='DROPPED' and t.status=='DROPPED':
                tags.append('DROP');changes.append(f'− DROP #{id}: {t.reason}')
            if sorted(old.needs)!=sorted(t.needs):
                tags.append('NEEDS');changes.append(f'NEEDS #{id}: {task_ids(old.needs)} → {task_ids(t.needs)}')
            if old.summary!=t.summary:
                tags.append('EDIT');changes.append(f'EDIT #{id}: {old.summary} → {t.summary}')
            if old.status=='DONE' and t.status!='DONE' and t.kind in {'reopened','invalidated'}:
                label='REOPEN' if t.kind=='reopened' else 'INVALIDATED'
                tags.append(label);changes.append(f'{label} #{id}: {t.reason}')
            if id in split:
                tags.append('SPLIT');changes.append(f'SPLIT #{id}: {old.status}→{t.status}')
            elif old.status in {'FAILED','BLOCKED'} and t.status=='PENDING':
                tags.append('RETRY');changes.append(f'RETRY #{id}: {old.status}→PENDING')
        if tags:marks[id]=tags
    changes.sort(key=lambda text:0 if text.startswith('+') else 1 if text.startswith('−') else 2)
    return marks,changes


def last_plan(frames,index):
    """Only a new persisted commit counts; phase recovery and rejected attempts do not."""
    for number in range(index,0,-1):
        before,after=frames[number-1][1],frames[number][1]
        if (before.phase=='planning' and after.phase in {'ready','blocked'} and
                before.replans==after.replans and before.events!=after.events and after.events and
                after.events[-1].split(' | ',1)[-1].startswith('Atomic plan batch applied: ')):
            return number
    return None


def task_name(task):
    text=clean(task.summary).strip()
    if task.path:
        verb=text.split()[0].rstrip(':')
        if verb.lower() not in {'repair','fix','create','build','check','verify','run','implement','test'}:
            verb='Check' if task.role=='TEST' else 'Work on'
        text=verb+' '+Path(task.path).name
    return shorten(re.split(r'; |\.\s',text,maxsplit=1)[0],32)


def warning(task):
    if not task.reason or task.status in {'DONE','DROPPED'}:return ''
    if 'assigned artifact path' in task.reason:return 'File scope rejected'
    return {'context':'Context limit','loop':'Repeated output','timeout':'Time limit',
            'limit':'Turn limit' if 'session-turn' in task.reason or 'session turn' in task.reason else 'Output/action limit',
            'protocol':'Invalid report','stream':'Stream interrupted','output':'Missing result',
            'workspace':'Workspace error',
            'invalidated':'Prerequisite changed','reopened':'Reopened','interrupted':'Interrupted'}.get(
                task.kind,shorten(clean(task.reason).split(';',1)[0],54))


def planned_order(state):
    """A conditional preview using the real scheduler, never a second traversal or a saved result."""
    preview=copy.deepcopy(state)
    order=[]
    while task_route:=preview.frontier():
        task,_=task_route
        if task is None:break
        if task.status not in {'PENDING','RUNNING'}:
            order.append(f'#{task.id} [replan]');break
        order.append(f'#{task.id}')
        task.status='DONE'  # Hypothetical success only in this disposable preview.
    return order


def throughput(session,state):
    """Last completed native API request, matched to this worker attempt; never task-time estimates."""
    task=next((t for t in state.tasks.values() if t.status=='RUNNING'),None)
    if task is None:return None
    try:
        logs=sorted((session/'turns').glob('*/qwen.log'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        for log in logs:
            with log.open('r',encoding='utf-8',errors='replace') as source:head=source.read(8192)
            init=None
            for line in head.splitlines():
                try:row=json.loads(line)
                except ValueError:continue
                if isinstance(row,dict) and row.get('type')=='system' and row.get('subtype')=='init':init=row;break
            if not init:continue
            review=Path(init.get('cwd',''))
            if review.parent!=session.resolve()/'reviews' or not review.name.startswith(f'{task.id}-{task.attempts}-'):continue
            for usage in sorted((log.parent/'native/usage').glob('token-usage-*.jsonl'),reverse=True):
                with usage.open('rb') as source:
                    source.seek(max(0,usage.stat().st_size-65536));tail=source.read().decode('utf-8',errors='replace')
                for line in reversed(tail.splitlines()):
                    try:row=json.loads(line)
                    except ValueError:continue
                    if not isinstance(row,dict) or row.get('sessionId')!=init.get('session_id') or row.get('source')!='main':continue
                    tokens,ms=row.get('outputTokens'),row.get('apiDurationMs')
                    if all(type(v) in {int,float} and math.isfinite(v) and v>0 for v in (tokens,ms)):
                        return tokens*1000/ms
                    return None
            return None
    except (OSError,ValueError,TypeError):pass
    return None


def navigation(width,errors,details,activity,color,scroll=False):
    keys='← → History  L Live  ' if width>=64 else '←→ Hist  L Live  ' if width>=30 else 'L Live  '
    if errors:keys+=('D/Enter Less  ' if details else 'D/Enter Errors  ') if width>=64 else 'D Err  '
    if scroll:keys+='↑↓ Scroll  ' if width>=46 else '↑↓  '
    keys+='Q Quit' if width>=46 else 'Q'
    if columns(keys)>width:keys='Q  L  '+('D  ' if errors else '')+('↑↓' if scroll else '')
    if color:keys=f'\x1b[{MUTED}m{keys}{RESET}'
    if columns(keys)+len(activity)+2<=width:
        keys+=' '*(width-columns(keys)-len(activity))+(f'\x1b[{MUTED}m{activity}{RESET}' if color else activity)
    return keys


def render(frames,index=None,now=None,width=120,color=True,details=False,rate=None,ended=False,session_label='',acceptance=''):
    width=max(1,width)
    live=index is None
    index=len(frames)-1 if live else index
    stamp,state=frames[index]
    ended=ended and live or state.phase in {'complete','blocked'}
    until=(time.time() if now is None else now) if live and not ended else stamp
    times=work_times(frames,index,until)
    active=[t for t in state.tasks.values() if t.status!='DROPPED']
    done=sum(t.status=='DONE' for t in active)
    # LongFlow's final acceptance follows TODO completion; an older completed
    # release in HISTORY must not inherit the later release's acceptance.
    longflow=bool(acceptance) or session_label.rsplit(' / ',1)[-1].startswith('longflow-')
    success=(state.phase=='complete' and state.complete() and
             (not longflow or acceptance=='PASS' and index==len(frames)-1))
    percent=100 if success else min(99,round(100*done/len(active))) if active else 0
    planned=last_plan(frames,index)
    marks={}
    diff=False
    if planned is not None:
        before,accepted=frames[planned-1][1],frames[planned][1]
        marks,_=plan_changes(before,accepted)
        # Show accepted changes through the first worker start, then clear on its result or a new plan.
        diff=(state.phase=='ready' and state.replans==accepted.replans and
              (state.actions==accepted.actions or state.actions==accepted.actions+1 and
               any(t.status=='RUNNING' for t in state.tasks.values())))
    if not diff:marks={}
    issues=[t for t in state.tasks.values() if warning(t)]
    interrupted=ended and any(t.status=='RUNNING' for t in state.tasks.values())
    has_errors=bool(issues or state.reason or interrupted)
    details=details and has_errors
    selected,_=copy.deepcopy(state).frontier()
    selected_id=selected.id if selected else None
    terminal_width=max(1,width)
    padding=2 if width>=40 else 1 if width>=8 else 0
    width=min(88,max(1,width-2*padding))
    margin=max(padding,(terminal_width-width)//2)
    def ink(text,code=MUTED):
        return f'\x1b[{code}m{text}{RESET}' if color and text else text
    def add(text=''):lines.append(shorten(text,width))
    def pair(left,right):
        if not right:return shorten(left,width)
        room=width-columns(right)-2
        if room<8:return shorten(left,width)
        left=shorten(left,room)
        return left+' '*max(2,width-columns(left)-columns(right))+right
    def clock(value):
        if value=='?':return '?'
        uncertain=value.endswith('?')
        parts=[int(p) for p in value.rstrip('?').split(':')]
        seconds=sum(p*60**i for i,p in enumerate(reversed(parts)))
        h,seconds=divmod(seconds,3600);m,seconds=divmod(seconds,60)
        return (f'{h}h {m:02d}m' if h else f'{m}m {seconds:02d}s' if m else f'{seconds}s')+('?' if uncertain else '')
    mode='LIVE' if live else f'HISTORY {index+1}/{len(frames)}'
    if width>=42:mode+=f' · PLAN {state.replans}'
    lines=[]
    brand='BACKTRACKIFY · LIVE TREE' if width>=58 else 'BACKTRACKIFY' if width>=22 else 'TODO'
    add(pair(ink(brand,'1;38;2;226;232;240'),ink(mode,CYAN if live else AMBER)))
    add(ink('─'*width,BORDER))
    add(ink(shorten('SESSION · '+session_label,width))) if session_label else add()
    add(pair(ink('Elapsed '+clock(duration(until-state.started))),ink(f'{done}/{len(active)} DONE · {percent}%',GREEN if success else '38;2;226;232;240')))
    filled=int(width*percent/100)
    add(ink('━'*filled,GREEN)+ink('━'*(width-filled),BORDER))
    add(ink('─'*width,BORDER))
    add()
    def detail(prefix,label,text,code=MUTED):
        indent=prefix+'  '
        if columns(indent)>width//2:indent='  '
        for line in wrapped(label+clean(text),max(1,width-columns(indent))):add(ink(indent+line,code))
    def branch(parent,prefix=''):
        children=sorted((t for t in state.tasks.values() if t.parent==parent and
                         (t.status!='DROPPED' or 'DROP' in marks.get(t.id,[]))),key=lambda t:t.id)
        for number,t in enumerate(children):
            last=number==len(children)-1
            lead=prefix+('└─ ' if last else '├─ ') if parent else ''
            more=prefix+('   ' if last else '│  ') if parent else '  '
            # Fold distant ancestors without changing the real parent-child relationship.
            indent_limit=max(3,width-29)
            if columns(lead)>indent_limit:lead='⋮ '+lead[-max(1,indent_limit-2):]
            detail_prefix=lead[:-3] if parent else ''
            available=width-columns(lead)
            role='ALG' if t.role=='ALGORITHM' and available<40 else t.role
            status=t.status if available>=38 else {'RUNNING':'RUN','WAITING':'WAIT','PENDING':'PEND','FAILED':'FAIL','BLOCKED':'BLOCK','DROPPED':'DROP'}.get(t.status,t.status)
            tags=marks.get(t.id,[])
            dropped=t.status=='DROPPED'
            symbol='▶' if t.id==selected_id else {'DONE':'✓','RUNNING':'▶','WAITING':'◷','FAILED':'!','BLOCKED':'!','DROPPED':'×'}.get(t.status,'○')
            code=COLORS[t.status]
            identity=f'{symbol} #{t.id} {role}'
            row=ink(lead,BORDER)+ink(identity,'9;'+MUTED if dropped else CYAN if t.id==selected_id else code)+ink(' · '+status,code)
            tail=''
            if t.attempts:tail+=' · '+clock(times[t.id])+f' · {t.attempts}/{LIMIT_ATTEMPTS}'
            if t.needs:
                shown=sorted(t.needs)
                room=max(9,available-columns(identity)-len(status)-columns(tail)-20)
                while shown:
                    omitted=len(t.needs)-len(shown)
                    needs='needs '+task_ids(shown)+(f' +{omitted}' if omitted else '')
                    if columns(needs)<=room:break
                    shown.pop()
                else:needs=f'needs +{len(t.needs)}'
                tail+=' · '+needs
            tags=[tag for tag in tags if tag not in {'NEW','DROP'}]
            suffix=(' · '+' · '.join(tags)) if tags else ''
            room=width-columns(row)-columns(tail)-columns(suffix)-3
            if room>=8:row+=' · '+ink(shorten(task_name(t),min(32,room)),'9;'+MUTED if dropped else code)
            row+=ink(tail,code)+ink(suffix,CYAN if 'NEEDS' in tags else AMBER)
            add(row)
            message=warning(t)
            if message:
                recovering=t.status not in {'FAILED','BLOCKED'}
                warning_prefix='Waiting for children · previous ' if t.status=='WAITING' else 'Retry · ' if recovering else ''
                add(ink(detail_prefix+'  ↳ '+warning_prefix+message,AMBER if recovering else RED))
            if ended and t.status=='RUNNING':add(ink(detail_prefix+'  ↳ Run ended before report acceptance; resume to recover',AMBER))
            if 'EDIT' in tags:
                add(ink(detail_prefix+'  ↳ was '+shorten(clean(before.tasks[t.id].summary),48),AMBER))
            if 'NEEDS' in tags:
                add(ink(detail_prefix+'  ↳ needs '+task_ids(before.tasks[t.id].needs)+' → '+task_ids(t.needs),CYAN))
            branch(t.id,more)
    branch(0)
    if not state.tasks:
        add()
        title='Planning the first TODOs' if state.phase=='planning' else 'Planning stopped' if state.phase=='blocked' else 'Waiting for planning'
        add(ink('● PROMPT',RED if state.phase=='blocked' else CYAN)+' · '+ink(title,'38;2;226;232;240'))
        add()
        add(ink('Your goal → an accepted plan → one task at a time'))
        add(ink('Tasks appear after an accepted atomic plan.'))
        add(ink('No workers delegated yet.'))
        add()
    if state.reason:add(ink('STOP · '+shorten(clean(state.reason).split(';',1)[0],64),RED))
    if state.tasks:
        add()
        add(ink('PLANNED EXECUTION ORDER · if reports succeed',CYAN))
        order=planned_order(state) if state.phase!='planning' else []
        for line in wrapped(' → '.join(order) or ('Awaiting accepted plan' if state.phase=='planning' else 'Stopped' if state.phase=='blocked' else 'All active tasks complete'),max(1,width-2)):
            add(ink('  '+line))
    if details:
        add()
        add(ink('ERROR DETAILS',RED if state.reason or any(t.status in {'FAILED','BLOCKED'} for t in issues) else AMBER))
        if interrupted:detail('','Interrupted: ','Run ended; the stored RUNNING task has no accepted report. Resume the session to recover.',AMBER)
        if state.reason:detail('','Stop: ',state.reason,RED)
        for task in issues:
            recovering=task.status not in {'FAILED','BLOCKED'}
            detail('',f'#{task.id} {task.status} · ',task_name(task),AMBER if recovering else RED)
            detail('','Previous attempt: ' if recovering else 'Cause: ',task.reason)
            if task.path:detail('','Artifact: ',task.path)
            detail('','Task: ',task.summary)
            if task.result:detail('','Worker reported: ',task.result)
            if task.related:detail('','Related: ',task_ids(task.related))
        for event in state.events[-3:]:
            if any(mark in event for mark in ('failed:','Stopped:','rejected:','unavailable:')):detail('','Diagnostic: ',event)
    current=next((t for t in state.tasks.values() if t.status=='RUNNING'),None) or selected
    if ended:
        current_line=ink('ENDED · '+('OK · ' if success else '')+('TODOs DONE · accepted agent reports' if state.phase=='complete' else 'Blocked' if state.phase=='blocked' else 'Unfinished work retained; session can be resumed'),GREEN if success else AMBER)
    elif current and state.phase=='ready':
        current_line=pair(ink(f'CURRENT · #{current.id} · '+task_name(current),CYAN),
                          ink(f'attempt {current.attempts}/{LIMIT_ATTEMPTS}') if current.attempts else '')
    else:
        current_line=ink('COMPLETE · All active tasks reported DONE',GREEN) if state.phase=='complete' else ink(
            'BLOCKED · See Errors for the obstacle',RED) if state.phase=='blocked' else ink(
            'PLANNING · PROMPT is preparing the next tasks',CYAN) if state.phase=='planning' else ink('Waiting for the next eligible task')
    add(ink('─'*width,BORDER))
    add(current_line)
    if live and acceptance:
        add(ink('GPU LONGFLOW · '+acceptance,GREEN if acceptance=='PASS' else RED))
    elif live and state.phase=='complete' and session_label.rsplit(' / ',1)[-1].startswith('longflow-'):
        add(ink('GPU LONGFLOW · Acceptance pending',AMBER))
    elif diff:
        groups=[]
        for tag,title,code in [('NEW','ADD',GREEN),('DROP','DROP',RED),('SPLIT','SPLIT',AMBER),('EDIT','EDIT',AMBER),
                               ('NEEDS','NEEDS',CYAN),('REOPEN','REOPEN',AMBER),('INVALIDATED','INVALIDATED',AMBER),('RETRY','RETRY',AMBER)]:
            ids=[id for id,tags in marks.items() if tag in tags]
            if ids:groups.append(ink(title+' '+task_ids(ids),code))
        add(ink('LAST PLAN · ',CYAN)+' · '.join(groups))
    else:
        reports=[t for t in state.tasks.values() if t.result and t.status=='DONE']
        last=reports[-1] if reports else None
        add(ink(f'OK · #{last.id} · '+task_name(last),GREEN) if last else '')
    add(ink('─'*width,BORDER))
    activity='History' if not live else 'Ended' if ended else ('Working' if state.phase=='planning' or any(t.status=='RUNNING' for t in state.tasks.values()) else
                                       'Waiting' if state.phase=='blocked' or selected and selected.status in {'FAILED','BLOCKED','WAITING'} else 'Idle')+(
                                           f' · last API {rate:.1f} t/s' if type(rate) in {int,float} and math.isfinite(rate) and rate>0 else ' · -- t/s')
    add(navigation(width,has_errors,details,activity,color))
    # Center a restrained panel; the rest of the window keeps the same background.
    return '\n'.join((RESET if color else '')+' '*margin+line+' '*max(0,terminal_width-margin-columns(line)) for line in lines)


def viewport(text,width,height,scroll=None):
    """Keep the panel and navigation visible; scroll only the tree and Details."""
    rows=text.splitlines()
    header,body,footer=rows[:HEADER_LINES],rows[HEADER_LINES:-FOOTER_LINES],rows[-FOOTER_LINES:]
    top=[''] if height>=18 else []
    bottom=[''] if height>=18 else []
    if height<15:header,footer=header[:2],footer[-1:]
    if height<6:header=[]
    capacity=max(0,height-len(top)-len(bottom)-len(header)-len(footer))
    if scroll is None:
        selected=next((i for i,row in enumerate(body) if SGR.sub('',row).strip()=='ERROR DETAILS'),None)
        if selected is None:selected=next((i for i,row in enumerate(body) if re.match(r'^[│ ⋮├└─]*▶ #',SGR.sub('',row))),0)
        scroll=max(0,selected-capacity//2)
    scroll=min(max(0,scroll),max(0,len(body)-capacity))
    visible=body[scroll:scroll+capacity]
    colored='\x1b' in text
    plain=SGR.sub('',footer[-1]).strip()
    activity=re.search(r'(Working|Waiting|Idle|History|Ended)(?: · (?:--|last API \d+\.\d+) t/s)?$',plain)
    panel_width=min(88,max(1,width-(4 if width>=40 else 2 if width>=8 else 0)))
    margin=max(0,(width-panel_width)//2)
    footer[-1]=' '*margin+navigation(panel_width,bool(re.search(r'\bD\b',plain)),'Less' in plain,
                                     activity[0] if activity else '',colored,capacity<len(body))
    screen=top+header+visible+footer+bottom+['']*max(0,capacity-len(visible))
    return '\n'.join((RESET if colored else '')+shorten(line,width)+' '*max(0,width-columns(line))
                     for line in screen[:max(1,height)]),scroll

def main(argv=None):
    parser=argparse.ArgumentParser(description='Read-only Backtrackify TODO history; no model or project commands')
    parser.add_argument('session',type=Path)
    parser.add_argument('--once',action='store_true',help='Print the current tree without opening an interactive terminal')
    parser.add_argument('--details',action='store_true',help='Expand current warnings and error diagnostics')
    parser.add_argument('--follow',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    if args.once:
        print(render(read_history(args.session),width=shutil.get_terminal_size((120,40)).columns,color=False,details=args.details,
                     ended=owner_ended(args.session),session_label=args.session.parent.parent.name+' / '+args.session.name,
                     acceptance=acceptance_result(args.session)));return 0
    if os.name!='nt':parser.error('Interactive view requires Windows; use --once')
    import msvcrt
    kernel=ctypes.windll.kernel32
    kernel.SetConsoleTitleW('Backtrackify | LIVE TODO TREE | '+clean(args.session.name))
    kernel.GetStdHandle.restype=ctypes.c_void_p
    handle=kernel.GetStdHandle(-11)
    mode=ctypes.c_ulong()
    ansi=bool(kernel.GetConsoleMode(ctypes.c_void_p(handle),ctypes.byref(mode)) and
              kernel.SetConsoleMode(ctypes.c_void_p(handle),mode.value|4))
    frames,index,scroll,changed,last=[],None,0,True,0
    details=args.details
    follow=True
    signature=None
    guard=ExitStack()
    try:
        if args.follow:guard.enter_context(lock(args.follow.with_suffix('.lock')))
        if ansi:print('\x1b[?1049h\x1b[?25l'+RESET,end='',flush=True)
        while True:
            if msvcrt.kbhit():
                key=msvcrt.getwch()
                if key in {'\x00','\xe0'}:key='@'+msvcrt.getwch()
                if key.lower()=='q':return 0
                if key.lower()=='l':index=None;scroll=0;follow=True
                elif key.lower()=='d' or key in {'\r','\n'}:
                    state=frames[len(frames)-1 if index is None else index][1] if frames else None
                    if state and (state.reason or any(warning(t) for t in state.tasks.values()) or
                                  owner_ended(args.session) and any(t.status=='RUNNING' for t in state.tasks.values())):
                        details=not details;scroll=0;follow=True
                elif key=='@K' and frames:index=max(0,(len(frames)-1 if index is None else index)-1);scroll=0;follow=True
                elif key=='@M' and index is not None:
                    index=index+1 if index<len(frames)-1 else None;scroll=0;follow=True
                elif key in {'@H','@P','@I','@Q'}:
                    scroll=max(0,scroll+{'@H':-1,'@P':1,'@I':-10,'@Q':10}[key]);follow=False
                changed=True
            if changed or time.monotonic()-last>=1:
                try:
                    if args.follow:
                        try:target=following(args.follow)
                        except (OSError,ValueError):target=args.session
                        if target!=args.session.resolve():
                            args.session=target;frames=[];index=None;scroll=0;signature=None;details=False;follow=True
                            kernel.SetConsoleTitleW('Backtrackify | LIVE TODO TREE | '+clean(target.name))
                    stat=(args.session/'state.log').stat()
                    current=(stat.st_mtime_ns,stat.st_size,len(list((args.session/'history').glob('*.log'))))
                    if current!=signature:frames=read_history(args.session);signature=current
                    width,height=shutil.get_terminal_size((120,40))
                    width,height=max(1,width-1),max(1,height)
                    ended=owner_ended(args.session) if index is None else False
                    rate=throughput(args.session,frames[-1][1]) if index is None and not ended else None
                    text=render(frames,index,width=width,color=ansi,details=details,rate=rate,ended=ended,
                                session_label=args.session.parent.parent.name+' / '+args.session.name,
                                acceptance=acceptance_result(args.session))
                    screen,scroll=viewport(text,width,height,None if follow else scroll)
                    if ansi:
                        print(RESET+'\x1b[H'+'\n'.join('\x1b[2K'+line for line in screen.splitlines())+'\x1b[J',end='',flush=True)
                    else:print('\n'*height+screen,flush=True)
                except (OSError,ValueError) as e:
                    print('VIEW | waiting for readable state | '+clean(e),flush=True)
                last=time.monotonic();changed=False
            time.sleep(0.1)
    except KeyboardInterrupt:return 0
    finally:
        if ansi:
            print('\x1b[0m\x1b[?25h\x1b[?1049l',end='',flush=True)
            kernel.SetConsoleMode(ctypes.c_void_p(handle),mode.value)
        guard.close()


if __name__=='__main__':
    raise SystemExit(main())
