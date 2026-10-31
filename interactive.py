"""Native Qwen front end: read normally, execute through the existing harness."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
import base64

from harness import (ROOT, PROFILES, Refused, State, Harness, Qwen, atomic, clone,
                     files, merge, lock, start_process, stop_process)

READS='read_file,glob,grep_search,list_directory,ask_user_question'
EXECUTION_MODES={'default','auto','auto-edit','yolo'}
# Pinned v0.22.3 native tools. CLI denies remain effective even when project
# settings add core tools. Other MCP servers are excluded by the launcher.
DENIED=('edit,write_file,zoom_image,run_shell_command,todo_write,save_memory,agent,skill,'
        'exit_plan_mode,enter_plan_mode,web_fetch,web_search,image_gen,lsp,cron_create,cron_list,'
        'cron_delete,loop_wakeup,create_sub_session,list_agents,task_stop,task_create,task_update,'
        'task_list,team_create,team_delete,team_plan_approval,request_shutdown,send_message,'
        'structured_output,monitor,notebook_edit,tool_search,read_mcp_resource,enter_worktree,'
        'exit_worktree,workflow,artifact,record_artifact,report_findings,get_goal,update_goal,display_image')
GUIDANCE='''Chat, explain and inspect files normally; discuss hypothetical plans without execution.
For requested executable work (TODO planning, implementation, testing, file changes or shell work),
call mcp__backtrackify__execute_task once for the current user request. It delegates the original request
and conversation to PROMPT, then runs Backtrackify's workers. Direct edits and shell tools are unavailable here.
If execution intent is unclear, ask a brief clarification. After the result, explain the accepted outcome or
obstacle; do not repeat the execution tool for the same request.'''
GUIDANCE+='\nAfter completion, summarize the returned delivery project and promoted file changes. Do not search internal harness sessions to rediscover them. If no project file changes were promoted, say so; accepted reports alone do not prove files were delivered.'


def configure(qwen, home, workspace, settings, cli_args):
    if os.environ.get('BACKTRACKIFY_WORKER'):
        raise Refused('A harness worker cannot launch another managed Qwen session')
    protected={'--allowed-mcp-server-names','--exclude-tools','--core-tools','--system-prompt',
               '--system-prompt-file','--append-system-prompt','--append-system-prompt-file',
               '--model','-m','--extensions','-e','--bare'}
    if any(arg.split('=',1)[0] in protected for arg in cli_args):
        raise Refused('Managed Qwen owns its model/tool boundary; omit model, tool, extension and system-prompt overrides')
    workspace=Path(workspace).resolve()
    for name in ('.qwen/settings.json','.qwen/settings.local.json','.mcp.json'):
        project_settings=workspace/name
        if project_settings.exists():
            project=json.loads(project_settings.read_text(encoding='utf-8'))
            if ('backtrackify' in project.get('mcpServers',{}) or
                    any(p.get('id')=='qwen-backtrackify' for p in project.get('modelProviders',{}).get('openai',[]))):
                raise Refused('Project settings conflict with the reserved backtrackify server/model; existing settings were not changed')
    config=home/'interactive.json'
    atomic(config,json.dumps(dict(cli=qwen.cli,profile=qwen.profile,context=qwen.context,
        base_url=qwen.base_url,agent_seconds=qwen.agent_seconds,project=str(workspace),home=str(home),vision=getattr(qwen,'vision',False))))
    quote=lambda value:"'"+str(value).replace("'","''")+"'"
    command='& '+' '.join(map(quote,[sys.executable,'-B',Path(__file__).resolve(),'capture',config]))
    settings['disableAllHooks']=False
    hook={'type':'command','shell':'powershell','command':command,'timeout':10000}
    settings['hooks']={'UserPromptSubmit':[{'hooks':[hook]}],
        'PreToolUse':[{'matcher':'mcp__backtrackify__execute_task','hooks':[dict(hook)]}]}
    settings['mcpServers']={'backtrackify':{'command':sys.executable,'args':['-B',str(Path(__file__).resolve()),'serve',str(config)],
        'trust':True,'timeout':2147483647,'alwaysLoadTools':True}}
    # Native MCP requires a finite transport timer (at most 2^31-1 ms). This
    # ceiling exceeds the harness's bounded invocation budget; workers keep all
    # their existing limits. Progress notifications retain the native idle guard.
    settings['model'].pop('maxSessionTurns',None)
    settings['model'].pop('maxWallTimeSeconds',None)
    return ['--core-tools',READS,'--exclude-tools',DENIED,'--allowed-mcp-server-names','backtrackify',
            '--append-system-prompt',GUIDANCE]


def capture(config, payload):
    if os.environ.get('BACKTRACKIFY_WORKER'):return {}
    if payload.get('hook_event_name')=='PreToolUse':
        if payload.get('tool_name')!='mcp__backtrackify__execute_task':raise Refused('Unexpected execution tool')
        atomic(Path(config['home'])/'permit.json',json.dumps(payload,ensure_ascii=False))
        allowed=payload.get('permission_mode') in EXECUTION_MODES
        return dict(hookSpecificOutput=dict(hookEventName='PreToolUse',permissionDecision='allow' if allowed else 'deny',
            permissionDecisionReason='Delegate the current request to Backtrackify' if allowed else 'Plan mode is read-only; continue conversation or inspection without execution'))
    if payload.get('hook_event_name')!='UserPromptSubmit':raise Refused('Expected a submitted user prompt')
    if Path(payload['cwd']).resolve()!=Path(config['project']):raise Refused('User prompt project mismatch')
    if not payload.get('prompt') or not payload.get('session_id') or not payload.get('transcript_path'):
        raise Refused('Missing original user prompt/session/transcript')
    transcript=Path(payload['transcript_path'])
    rows=[json.loads(line) for line in transcript.read_text(encoding='utf-8').splitlines() if line.strip()] if transcript.exists() else []
    users=[row for row in rows if row.get('type')=='user' and row.get('provenance')=='real_user']
    # Pinned UserPromptSubmit runs before its real_user transcript row is saved.
    # Bind that boundary too: identical later prompts cannot reuse a stale capture.
    payload=dict(payload,request=uuid.uuid4().hex,previous_user=users[-1]['uuid'] if users else None)
    atomic(Path(config['home'])/'prompt.json',json.dumps(payload,ensure_ascii=False))
    return {}


def conversation(payload):
    """Keep native retained state and visible conversation; never synthesize intent."""
    rows=[json.loads(line) for line in Path(payload['transcript_path']).read_text(encoding='utf-8').splitlines() if line.strip()]
    users=[r for r in rows if r.get('type')=='user' and r.get('provenance')=='real_user']
    text=lambda row:'\n'.join(p['text'] for p in row.get('message',{}).get('parts',[]) if p.get('text') and not p.get('thought'))
    # Native partToString concatenates expanded @file text parts verbatim.
    current=''.join(p['text'] for p in users[-1].get('message',{}).get('parts',[]) if p.get('text') and not p.get('thought')) if users else ''
    if not users or current.strip() not in {payload['prompt'].strip(),payload.get('submitted_prompt','').strip()}:
        raise Refused('No matching current user message in the native transcript; no task was inferred')
    if payload.get('previous_user')!=(users[-2]['uuid'] if len(users)>1 else None):
        raise Refused('Stale user-prompt capture; no previous task was reactivated')
    kept=[]
    for row in rows:
        if row.get('subtype')=='chat_compression':
            kept=['Native retained context:\n'+'\n'.join(p.get('text','') for m in row['systemPayload']['compressedHistory'] for p in m.get('parts',[]))]
        elif row.get('type') in {'user','assistant'} and row is not users[-1]:
            value=text(row)
            if value:kept.append(row['type']+': '+value)
            for part in row.get('message',{}).get('parts',[]):
                if part.get('functionCall',{}).get('name') in READS.split(','):
                    kept.append('Prior tool reference (use current workspace files): '+json.dumps(part['functionCall'],ensure_ascii=False))
                response=part.get('functionResponse',{})
                if response.get('name')=='ask_user_question':kept.append('User clarification: '+json.dumps(response,ensure_ascii=False))
                elif response.get('name') in {'mcp__backtrackify__execute_task','execute_task'}:
                    value='Prior Backtrackify outcome: '+json.dumps(response,ensure_ascii=False)
                    if value not in kept:kept.append(value)
    original=payload.get('submitted_prompt') or payload['prompt']
    if payload['prompt']!=original:kept.append('Expanded current request:\n'+payload['prompt'])
    memory='Conversation evidence; the current user goal takes precedence over earlier requests.\n'+'\n\n'.join(kept)
    if len(original)>6000 or len(original)+len(memory)>20000:
        raise Refused('User goal/conversation exceeds the existing harness context bound; compact the native conversation or restate the relevant constraints')
    return original,memory


def delivery_complete(session):
    manifest=json.dumps(files(session/'workspace'),sort_keys=True).encode()
    return '# DELIVERED | '+hashlib.sha256(manifest).hexdigest() in (session/'source-files.log').read_text(encoding='utf-8').splitlines()


def snapshot_images(payload,session):
    """Only the authorized current real_user row supplies task image bytes."""
    import vision
    conversation(payload)
    user=None
    with Path(payload['transcript_path']).open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():continue
            row=json.loads(line)
            if row.get('type')=='user' and row.get('provenance')=='real_user':user=row
    ids=[]
    for part in user['message'].get('parts',[]):
        media=part.get('inlineData')
        if part.get('fileData'):raise Refused('Task images must be attached bytes, not remote or arbitrary file URIs')
        if not media:continue
        if len(ids)>=vision.MAX_IMAGES:raise Refused('Attach at most two task images')
        try:raw=base64.b64decode(media.get('data',''),validate=True)
        except (ValueError,TypeError):raise Refused('Invalid task image encoding') from None
        id=vision.store(session,raw,media.get('mimeType'))
        if id in ids:raise Refused('Duplicate task image')
        ids.append(id)
    if ids:
        rows=['Backtrackify task images 1','request '+session.name,'native '+payload['session_id'],'user '+user['uuid']]
        rows.extend(ids);atomic(session/'task-images.txt','\n'.join(rows)+'\n')


def deliver(harness, project, baseline, snapshot):
    """Commit project delivery and terminal state together; preserve rollback."""
    if snapshot and files(harness.workspace)!=files(harness.workspace,True):
        raise Refused('Generated installation assets cannot be published as source changes')
    path=harness.session/'source-files.log';before=path.read_bytes()
    def commit():
        manifest=json.dumps(files(harness.workspace),sort_keys=True).encode()
        atomic(path,before.split(b'# DELIVERED | ',1)[0]+b'# DELIVERED | '+hashlib.sha256(manifest).hexdigest().encode()+b'\n')
        try:Harness.save(harness)
        except BaseException:
            atomic(path,before);raise
    merge(project,harness.workspace,baseline,commit=commit,snapshot=snapshot)


class InteractiveHarness(Harness):
    def save(self):
        if self.state.phase=='complete' and not self.delivered:
            try:deliver(self,self.project,self.baseline,self.snapshot)
            except (OSError,ValueError) as error:raise Refused('Project delivery failed; accepted workspace retained: '+str(error)) from error
            self.delivered=True
        else:super().save()


def work(config, session, agent=None):
    project=Path(config['project']);snapshot=project==ROOT
    if not snapshot and project in Path(config['home']).parents:
        raise Refused('Project contains managed session storage; choose its actual source project directory')
    request=json.loads((session/'input.json').read_text(encoding='utf-8'))
    if not (session/'state.log').exists():goal,memory=conversation(request)
    with lock(Path(config['home'])/'project.lock'):
        if not (session/'state.log').exists():
            shared=ROOT/'memory.txt'
            present=True
            try:
                with shared.open(encoding='utf-8') as handle:instructions=handle.read(20001)
            except FileNotFoundError:instructions,present='',False
            except (OSError,UnicodeError) as error:
                raise Refused('ROOT memory.txt must be readable UTF-8 text: '+str(error)) from error
            if any(ord(c)<32 and c not in '\n\r\t' for c in instructions):
                raise Refused('ROOT memory.txt contains unsupported control characters')
            memory=('Shared ROOT instructions'+('' if present else ' (memory.txt absent)')+':\n'+
                    (instructions or '(none)')+'\n\n'+memory)
            if len(goal)+len(memory)+2>20000:
                raise Refused('Goal/shared ROOT instructions/conversation exceed the existing 20000-character bound; nothing was truncated')
            snapshot_images(request,session)
            baseline=clone(project,session/'workspace',snapshot=snapshot)
            atomic(session/'source-files.log',''.join(f'{p} | {h}\n' for p,h in baseline.items()))
            atomic(session/'goal.txt',goal+'\n');atomic(session/'memory.txt',memory+'\n')
            State(max_depth=PROFILES[config['profile']][3]).save(session/'state.log')
        baseline=dict(line.split(' | ') for line in (session/'source-files.log').read_text(encoding='utf-8').splitlines() if not line.startswith('#'))
        saved=State.load(session/'state.log')
        if saved.phase=='complete' and not delivery_complete(session):
            saved.phase='ready';saved.event('Interactive delivery pending; accepted reports retained');saved.save(session/'state.log')
        os.environ['BACKTRACKIFY_VIEW_HOME']=str(Path(config['home'])/'view')
        from visualize import open_view
        try:open_view(session,project,owner=Path(config['home']))
        except (OSError,ValueError) as error:print('VIEW | WARN | '+str(error),flush=True)
        agent=agent or Qwen(config['cli'],config['profile'],config['context'],config['base_url'],session,config['agent_seconds'],vision=config.get('vision',False))
        harness=InteractiveHarness(session,agent)
        harness.project,harness.baseline,harness.snapshot=project,baseline,snapshot
        harness.delivered=harness.state.phase=='complete'
        return harness.run()


def outcome(session):
    state=State.load(session/'state.log')
    active=[t for t in state.tasks.values() if t.status!='DROPPED']
    done=sum(t.status=='DONE' for t in active)
    delivered=state.phase=='complete' and delivery_complete(session)
    phase='COMPLETE' if delivered else 'WORKSPACE COMPLETE' if state.phase=='complete' else state.phase.upper()
    details=''
    if delivered:
        project=json.loads((session/'input.json').read_text(encoding='utf-8'))['cwd']
        baseline=dict(line.split(' | ') for line in (session/'source-files.log').read_text(encoding='utf-8').splitlines() if not line.startswith('#'))
        current=files(session/'workspace');changes=[]
        for path in sorted(set(baseline)|set(current)):
            if baseline.get(path)!=current.get(path):changes.append(('Deleted: ' if path not in current else 'Saved: ')+path)
        details='Delivery project: '+project+'\n'
        details+=('Promoted file changes ('+str(len(changes))+'):\n'+'\n'.join(changes[:20])+('\nAdditional changes: '+str(len(changes)-20) if len(changes)>20 else '') if changes else 'No project file changes were promoted.')
    return (f'Backtrackify {phase} | {done}/{len(active)} DONE | session {session}\n'+
            (details if delivered else
             'No completed delivery; accepted/partial work remains in the session workspace. '+state.reason+'\nDiagnostics: '+str(session/'interactive.log'))+
            '\nAccepted DONE reports are agent results, not independent correctness proofs.')


def execute(config, cancelled, progress, invocation=None, resume=None):
    home=Path(config['home'])
    payload=json.loads((home/'prompt.json').read_text(encoding='utf-8'))
    if invocation is not None and (not isinstance(invocation,dict) or invocation.get('version')!=1 or
            invocation.get('sessionId')!=payload['session_id'] or not invocation.get('promptId')):
        raise Refused('Missing or mismatched native Qwen invocation context; no task was inferred')
    # The pinned headless/stream-json path does not always populate SDK invocation
    # metadata. Its actual real_user journal still binds the request, including
    # a /clear or resumed CLI conversation replacing the previous transcript.
    chats=list((home/'native/projects').glob('*/chats/*.jsonl'))
    current=Path(payload['transcript_path']).resolve()
    if current not in [p.resolve() for p in chats] or any(p.resolve()!=current and p.stat().st_mtime_ns>=current.stat().st_mtime_ns for p in chats):
        raise Refused('User-prompt capture belongs to a previous native conversation; no task was inferred')
    conversation(payload)  # A stale/missing capture must never execute a prior goal.
    session=home/'tasks'/payload['request']
    if resume is not None:
        session=Path(resume).resolve()
        original=(payload.get('submitted_prompt') or payload['prompt']).replace('/','\\').casefold()
        if not session.is_relative_to(ROOT/'sessions') or str(session).casefold() not in original:
            raise Refused('Resume requires an explicit managed session path in the original user request')
        saved=json.loads((session/'input.json').read_text(encoding='utf-8'))
        if Path(saved['cwd']).resolve()!=Path(config['project']) or not (session/'source-files.log').exists():
            raise Refused('Resume session belongs to another project or lacks its delivery baseline')
        state=State.load(session/'state.log')
        from visualize import owner_ended
        if any(t.status=='RUNNING' for t in state.tasks.values()) and not owner_ended(session):
            raise Refused('The selected task is still running; no second worker was started')
    with lock(home/'activation.lock'):
        try:permit=json.loads((home/'permit.json').read_text(encoding='utf-8'))
        except FileNotFoundError as error:raise Refused('No native pre-tool permission record; execution was not activated') from error
        rows=[json.loads(line) for line in Path(payload['transcript_path']).read_text(encoding='utf-8').splitlines() if line.strip()]
        calls=next(([p['functionCall'] for p in row.get('message',{}).get('parts',[]) if p.get('functionCall')]
                    for row in reversed(rows) if row.get('type')=='assistant' and any(p.get('functionCall') for p in row.get('message',{}).get('parts',[]))),[])
        args={'resume_session':resume} if resume is not None else {}
        if (permit.get('session_id')!=payload['session_id'] or permit.get('transcript_path')!=payload['transcript_path'] or
                permit.get('tool_input')!=args or not any(c.get('id')==permit.get('tool_call_id') and
                c.get('name')=='mcp__backtrackify__execute_task' and c.get('args')==args for c in calls)):
            raise Refused('Stale or mismatched native pre-tool permission; no task was inferred')
        if permit.get('permission_mode') not in EXECUTION_MODES:
            raise Refused('Qwen plan mode is read-only; execution was not activated')
        if permit.get('consumed'):
            if (session/'state.log').exists():return outcome(session)
            raise Refused(f'Previous activation was interrupted; no duplicate task started: {session}')
        atomic(home/'permit.json',json.dumps(dict(permit,consumed=True),ensure_ascii=False))
        if session.exists() and (resume is None or state.phase=='blocked' or state.phase=='complete' and delivery_complete(session)):
            if (session/'state.log').exists():return outcome(session)
            raise Refused(f'Previous activation was interrupted; no duplicate task started: {session}')
        if resume is None:
            session.mkdir(parents=True)
            atomic(session/'input.json',json.dumps(payload,ensure_ascii=False))
        command=[sys.executable,'-B',str(Path(__file__).resolve()),'work',str(home/'interactive.json'),str(session)]
        env=dict(os.environ,BACKTRACKIFY_WORKER='1',PYTHONIOENCODING='utf-8')
        with (session/'interactive.log').open('wb') as log:
            process=start_process(command,cwd=Path(config['project']),env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    if cancelled.wait(2):raise Refused('Interactive task cancelled; checkpoint retained at '+str(session))
                    progress('Backtrackify executing; live tree and checkpoints: '+str(session))
            finally:stop_process(process)
        if not (session/'state.log').exists():raise Refused(f'Activation exited {process.returncode} before state creation; inspect {session}/interactive.log')
        if process.returncode not in {0,2}:raise Refused(f'Activation exited {process.returncode}; checkpoint retained: {session}')
        return outcome(session)


def serve(config):
    """Small stdio MCP adapter; all project work stays in an owned subprocess."""
    output_lock=threading.Lock();calls={};threads=[]
    def send(value):
        with output_lock:print(json.dumps(dict(jsonrpc='2.0',**value),ensure_ascii=False),flush=True)
    def run(request,cancelled):
        id=request['id'];params=request.get('params',{});token=params.get('_meta',{}).get('progressToken');count=0
        def progress(message):
            nonlocal count
            count+=1
            if token is not None:send(dict(method='notifications/progress',params=dict(progressToken=token,progress=count,message=message)))
        try:
            args=params.get('arguments',{})
            if (params.get('name')!='execute_task' or not isinstance(args,dict) or set(args)-{'resume_session'} or
                    'resume_session' in args and not isinstance(args['resume_session'],str)):
                raise Refused('Use execute_task without invented goal arguments')
            result=execute(config,cancelled,progress,params.get('_meta',{}).get('qwen-code/invocation'),args.get('resume_session'))
            value=dict(isError=not result.startswith('Backtrackify COMPLETE |'),content=[dict(type='text',text=result)])
        except (OSError,ValueError) as error:value=dict(isError=True,content=[dict(type='text',text='Backtrackify: '+str(error))])
        with contextlib.suppress(BrokenPipeError,OSError):send(dict(id=id,result=value))
        calls.pop(id,None)
    try:
        for line in sys.stdin:
            threads[:]=[thread for thread in threads if thread.is_alive()]
            request=json.loads(line);method=request.get('method');id=request.get('id');params=request.get('params',{})
            if method=='notifications/cancelled':
                if params.get('requestId') in calls:calls[params['requestId']].set()
                continue
            if id is None:continue
            if method=='tools/call':
                cancelled=threading.Event();calls[id]=cancelled
                thread=threading.Thread(target=run,args=(request,cancelled));threads.append(thread);thread.start();continue
            if method=='initialize':result=dict(protocolVersion=params['protocolVersion'],capabilities=dict(tools={}),serverInfo=dict(name='Backtrackify',version='1'))
            elif method=='tools/list':result=dict(tools=[dict(name='execute_task',description='Execute the current original user request through PROMPT, deterministic TODOs and ALGORITHM/TEST workers. Use for planning, edits, tests and shell work; returns accepted results. Ordinary chat and file reads need no activation.',inputSchema=dict(type='object',properties=dict(resume_session=dict(type='string',description='Only when the user explicitly supplied this managed session path to resume its original goal/checkpoint; omit for a new request.')),additionalProperties=False),annotations=dict(readOnlyHint=False,destructiveHint=True,idempotentHint=True))])
            elif method=='ping':result={}
            else:send(dict(id=id,error=dict(code=-32601,message='Unsupported MCP method')));continue
            send(dict(id=id,result=result))
    finally:
        for event in list(calls.values()):event.set()
        for thread in threads:thread.join()


def main():
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('capture','serve','work'));parser.add_argument('config',type=Path);parser.add_argument('session',type=Path,nargs='?')
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    if args.mode=='capture':
        try:output=capture(config,json.load(sys.stdin))
        except (OSError,ValueError) as error:
            atomic(Path(config['home'])/'capture-error.log',str(error)+'\n');raise
        print(json.dumps(output));return 0
    if args.mode=='serve':serve(config);return 0
    return work(config,args.session)


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError) as error:
        print('Backtrackify integration: '+str(error),file=sys.stderr);raise SystemExit(2)
