"""Managed native CLI/MCP handoff with CPU responses; no GPU or visible windows."""
import json
from contextlib import ExitStack
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from harness import ROOT, State, Harness, Refused, AgentFailure, Qwen, atomic, clone, merge, apply_plan, parse_plan, lock, start_process, stop_process
from interactive import configure, capture, conversation, work, execute, outcome, READS, DENIED
from tests.test_harness import report
from tests.test_native import CLI, Endpoint
from visualize import view_channel, render


class Setup(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix="interactive test's ")
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.project=self.root/'project';self.project.mkdir();atomic(self.project/'old.txt','preserve\n')
        self.home=self.root/'outer';self.home.mkdir();self.chat=self.home/'native/projects/cpu/chats/current.jsonl';atomic(self.chat,'')
        self.config=dict(home=str(self.home),project=str(self.project),cli=str(CLI),profile='MID',context=32768,base_url='unused',agent_seconds=1800)
        self.environment=patch.dict(os.environ,BACKTRACKIFY_VIEW_HOME=str(self.home/'view'),WT_SESSION='CPU-managed-terminal')
        self.environment.start();self.addCleanup(self.environment.stop)

    def request(self,text='Build the feature.'):
        capture(self.config,dict(hook_event_name='UserPromptSubmit',cwd=str(self.project),session_id='same-qwen-session',transcript_path=str(self.chat),prompt=text))
        with self.chat.open('a',encoding='utf-8') as f:f.write(json.dumps(dict(uuid=uuid.uuid4().hex,type='user',provenance='real_user',message=dict(parts=[dict(text=text)])))+'\n')
        payload=json.loads((self.home/'prompt.json').read_text(encoding='utf-8'))
        session=self.home/'tasks'/payload['request'];session.mkdir(parents=True)
        atomic(session/'input.json',json.dumps(payload));return session

    def permit(self,mode='auto'):
        id=uuid.uuid4().hex
        with self.chat.open('a',encoding='utf-8') as f:f.write(json.dumps(dict(type='assistant',message=dict(parts=[dict(functionCall=dict(id=id,name='mcp__backtrackify__execute_task',args={}))])))+'\n')
        return capture(self.config,dict(hook_event_name='PreToolUse',tool_name='mcp__backtrackify__execute_task',
            tool_call_id=id,tool_input={},permission_mode=mode,session_id='same-qwen-session',transcript_path=str(self.chat)))

    def agent(self,failure=None):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                calls.append((role,task_id))
                if role=='PROMPT':
                    if failure=='plan':return {'operations':parse_plan('ADD | 0 | TEST | | Invalid | 1\nFINISH')} if structured else 'Explanatory prose'
                    if any(tid==3 for _,tid in calls):return {'operations':parse_plan('BLOCKED | The required check could not report\n')}
                    return {'operations':parse_plan('ADD | 0 | ALGORITHM | | Integrate feature\nADD | 1 | ALGORITHM | feature.txt | Implement feature\nADD | 1 | TEST | tests/check.txt | Verify feature | 2\nADD | 0 | TEST | | Release verification | 1\nFINISH')}
                if task_id==2:atomic(workspace/'feature.txt','accepted feature\n')
                if task_id==3:
                    if failure=='report':raise AgentFailure('protocol','Missing native final report')
                    if failure=='concurrent':atomic(self.project/'old.txt','external change\n')
                    atomic(workspace/'tests/check.txt','accepted independent verification\n')
                return report(task_id)
        return Agent(),calls


class IntegrationTests(Setup):
    def test_completion_identifies_actual_delivery_and_does_not_invent_file_changes(self):
        session=self.request();agent,_=self.agent()
        with patch('visualize.open_view'):self.assertEqual(work(self.config,session,agent),0)
        result=outcome(session);self.assertIn('Delivery project: '+str(self.project),result)
        self.assertIn('Saved: feature.txt',result);self.assertIn('Saved: tests/check.txt',result)
        self.assertNotIn('Saved: old.txt',result)
        other=self.request('Inspect only.');base,_=self.agent()
        class ReadOnly:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':return dict(operations=parse_plan('ADD | 0 | TEST | | Inspect existing files\nFINISH'))
                return report(task_id)
        with patch('visualize.open_view'):self.assertEqual(work(self.config,other,ReadOnly()),0)
        self.assertIn('No project file changes were promoted.',outcome(other))

    def test_nested_execution_delivers_only_accepted_files_before_terminal_completion(self):
        session=self.request('Készítsd el a feature-t.');agent,calls=self.agent()
        observed=[]
        real_save=State.save
        def save(state,path):
            if state.phase=='complete':observed.append((self.project/'feature.txt').read_text())
            return real_save(state,path)
        with patch('visualize.open_view') as view,patch.object(State,'save',save):
            self.assertEqual(work(self.config,session,agent),0)
        self.assertEqual([id for _,id in calls if id],[2,3,1,4]);self.assertEqual(view.call_count,1)
        self.assertEqual(observed,['accepted feature\n']);self.assertEqual((self.project/'old.txt').read_text(),'preserve\n')
        self.assertTrue(State.load(session/'state.log').complete());self.assertTrue((self.project/'tests/check.txt').exists())
        # A normal checkpoint resume neither re-runs completed workers nor re-delivers.
        with patch('visualize.open_view'),patch('interactive.deliver',side_effect=AssertionError('No duplicate delivery')):
            self.assertEqual(work(self.config,session,agent),0)
        self.assertEqual(len(calls),5)

    def test_invalid_plans_missing_reports_and_concurrent_edits_stay_unsuccessful(self):
        for failure in ('plan','report','concurrent'):
            with self.subTest(failure=failure):
                atomic(self.project/'old.txt','preserve\n');session=self.request('Build '+failure);agent,calls=self.agent(failure)
                with patch('visualize.open_view'):self.assertEqual(work(self.config,session,agent),2)
                state=State.load(session/'state.log');self.assertEqual(state.phase,'blocked');self.assertFalse(state.complete())
                self.assertFalse((self.project/'feature.txt').exists())
                if failure=='plan':self.assertEqual(len(state.tasks),0);self.assertEqual(len(calls),3)
                if failure=='report':
                    self.assertEqual(state.tasks[3].attempts,3);self.assertEqual(state.tasks[2].status,'DONE')
                if failure=='concurrent':self.assertEqual((self.project/'old.txt').read_text(),'external change\n')

    def test_project_and_state_commit_failure_roll_back_delivery_without_false_100(self):
        session=self.request();agent,_=self.agent();real_save=State.save
        def fail(state,path):
            if state.phase=='complete':raise OSError('CPU state commit failure')
            return real_save(state,path)
        with patch('visualize.open_view'),patch.object(State,'save',fail):self.assertEqual(work(self.config,session,agent),2)
        state=State.load(session/'state.log');self.assertEqual(state.phase,'blocked')
        self.assertTrue(all(t.status=='DONE' for t in state.tasks.values()))
        self.assertFalse((self.project/'feature.txt').exists());self.assertTrue((session/'workspace/feature.txt').exists())
        self.assertIn('4/4 DONE · 99%',render([(state.started,state)],color=False))

    def test_automatic_viewer_follows_and_reopens_only_on_new_activation(self):
        lease=ExitStack();self.addCleanup(lease.close)
        channel=view_channel(self.project,owner=self.home)
        def opened(*args,**kwargs):
            lease.enter_context(lock(channel.with_suffix('.lock')))
            return subprocess.CompletedProcess([],0,b'',b'')
        with patch.dict(os.environ,WT_SESSION=''),patch('visualize.ctypes.windll.kernel32.GetConsoleWindow',return_value=0),\
             patch('visualize.subprocess.run',side_effect=opened) as launch:
            first=self.request('Task A');agent,_=self.agent()
            with patch('visualize.os.getpid',return_value=1001):self.assertEqual(work(self.config,first,agent),0)
            self.assertEqual(launch.call_count,1)
            second=self.request('Task B');agent,_=self.agent()
            with patch('visualize.os.getpid',return_value=2002):self.assertEqual(work(self.config,second,agent),0)
            self.assertEqual(launch.call_count,1);self.assertEqual(channel.read_text().strip(),str(second.resolve()))
            lease.close()  # Closing X releases only the viewer's OS lock.
            third=self.request('Task C after closing X');agent,_=self.agent();self.assertEqual(work(self.config,third,agent),0)
            self.assertEqual(launch.call_count,2);self.assertEqual(channel.read_text().strip(),str(third.resolve()))
        self.assertEqual(State.load(first/'state.log').tasks[1].attempts,1)
        self.assertNotEqual(first,second);self.assertNotEqual(second,third)

    def test_viewer_launch_failure_is_nonfatal_and_chat_capture_does_not_retry_it(self):
        with patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'CPU desktop unavailable')) as launch:
            first=self.request('Task A');agent,_=self.agent();self.assertEqual(work(self.config,first,agent),0)
            self.assertTrue(State.load(first/'state.log').complete());self.assertEqual(launch.call_count,1)
            capture(self.config,dict(hook_event_name='UserPromptSubmit',cwd=str(self.project),session_id='same-qwen-session',
                transcript_path=str(self.chat),prompt='Explain the result.'))
            self.assertEqual(launch.call_count,1);self.assertEqual(list((self.home/'tasks').iterdir()),[first])
            second=self.request('Task B');agent,_=self.agent();self.assertEqual(work(self.config,second,agent),0)
            self.assertTrue(State.load(second/'state.log').complete());self.assertEqual(launch.call_count,2)

    def test_missing_stale_and_oversized_context_never_invents_or_repeats_a_goal(self):
        invocation=dict(version=1,sessionId='same-qwen-session',promptId='native-prompt')
        with self.assertRaises(OSError):execute(self.config,threading.Event(),lambda _:None,invocation)
        session=self.request('Original task');payload=json.loads((session/'input.json').read_text())
        goal,memory=conversation(payload);self.assertEqual(goal,'Original task')
        self.request('New genuine task')
        with self.assertRaisesRegex(Refused,'matching current user'):conversation(payload)
        self.request('Original task')
        with self.assertRaisesRegex(Refused,'Stale user-prompt'):conversation(payload)
        session=self.request('x'*6001)
        with self.assertRaisesRegex(Refused,'context bound'):conversation(json.loads((session/'input.json').read_text()))
        with self.assertRaisesRegex(Refused,'invocation context'):execute(self.config,threading.Event(),lambda _:None,dict(version=1,sessionId='unrelated',promptId='wrong'))
        self.assertFalse((self.project/'feature.txt').exists())

    def test_ambiguous_native_journal_after_clear_is_refused_instead_of_selecting_old_context(self):
        self.request('Original task');self.permit()
        other=self.chat.with_name('after-clear.jsonl');atomic(other,'{}\n')
        stamp=self.chat.stat();os.utime(other,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        with patch('interactive.start_process',side_effect=AssertionError('No stale task may start')):
            with self.assertRaisesRegex(Refused,'previous native conversation'):
                execute(self.config,threading.Event(),lambda _:None)

    def test_configuration_is_local_fail_closed_and_workers_cannot_reenter(self):
        settings=dict(model={'maxSessionTurns':16,'maxWallTimeSeconds':1800});q=SimpleNamespace(**self.config)
        flags=configure(q,self.home,self.project,settings,[])
        self.assertFalse(settings['disableAllHooks']);self.assertEqual(set(settings['hooks']),{'UserPromptSubmit','PreToolUse'})
        self.assertEqual(set(settings['mcpServers']),{'backtrackify'})
        self.assertIn('write_file',flags[flags.index('--exclude-tools')+1]);self.assertIn('run_shell_command',DENIED)
        self.assertNotIn('maxSessionTurns',settings['model']);self.assertFalse((self.project/'.qwen/settings.json').exists())
        with patch.dict(os.environ,BACKTRACKIFY_WORKER='1'):
            with self.assertRaisesRegex(Refused,'worker cannot'):configure(q,self.home,self.project,settings,[])
            before=(self.home/'prompt.json').read_bytes() if (self.home/'prompt.json').exists() else None
            capture(self.config,{})
            self.assertEqual(before,(self.home/'prompt.json').read_bytes() if (self.home/'prompt.json').exists() else None)
        for mode in ('default','auto','auto-edit','yolo','plan'):
            self.assertEqual(self.permit(mode)['hookSpecificOutput']['permissionDecision'],'deny' if mode=='plan' else 'allow')
        with self.assertRaisesRegex(Refused,'owns its model/tool'):configure(q,self.home,self.project,settings,['--allowed-mcp-server-names','other'])
        atomic(self.project/'.mcp.json',json.dumps(dict(mcpServers=dict(backtrackify=dict(command='unrelated')))))
        before=(self.project/'.mcp.json').read_bytes()
        with self.assertRaisesRegex(Refused,'reserved backtrackify'):configure(q,self.home,self.project,settings,[])
        self.assertEqual((self.project/'.mcp.json').read_bytes(),before)
        if CLI.exists():
            source=next(p for p in (CLI.parent/'chunks').glob('*.js') if 'var ToolNames = {' in p.read_text(encoding='utf-8'))
            block=source.read_text(encoding='utf-8').split('var ToolNames = {',1)[1].split('\n};',1)[0]
            native=set(re.findall(r': "([a-z_]+)"',block))
            self.assertEqual(native-set(READS.split(',')),set(DENIED.split(',')))

    def test_cancelled_activation_consumes_intent_and_reaps_only_its_owned_worker(self):
        session=self.request();(session/'input.json').unlink();session.rmdir()
        self.permit()
        cancelled=threading.Event();cancelled.set();owned=[]
        invocation=dict(version=1,sessionId='same-qwen-session',promptId='native-prompt')
        def spawn(cmd,**kw):
            self.assertEqual(kw['stdin'],subprocess.DEVNULL)
            process=start_process([sys.executable,'-c','import time; time.sleep(20)'],**kw);owned.append(process);return process
        with patch('interactive.start_process',side_effect=spawn):
            with self.assertRaisesRegex(Refused,'cancelled'):execute(self.config,cancelled,lambda _:None,invocation)
        self.assertIsNotNone(owned[0].poll())
        self.permit()
        with patch('interactive.start_process',side_effect=AssertionError('No duplicate activation')):
            with self.assertRaisesRegex(Refused,'interrupted'):execute(self.config,threading.Event(),lambda _:None,invocation)

    def test_source_snapshot_delivery_preserves_installation_assets(self):
        atomic(self.project/'runtime/asset.bin','immutable runtime');baseline=clone(self.project,self.root/'review',snapshot=True)
        atomic(self.root/'review/old.txt','accepted edit\n')
        merge(self.project,self.root/'review',baseline,snapshot=True)
        self.assertEqual((self.project/'runtime/asset.bin').read_text(),'immutable runtime')
        self.assertEqual((self.project/'old.txt').read_text(),'accepted edit\n')

    def test_context_keeps_file_references_user_answers_and_native_retained_state(self):
        self.request('Inspect the project')
        rows=[dict(type='assistant',message=dict(parts=[dict(functionCall=dict(name='read_file',args=dict(file_path='ledger.py')))])),
              dict(type='user',provenance='tool',message=dict(parts=[dict(functionResponse=dict(name='read_file',response=dict(output='SOURCE_DUMP_NOT_NEEDED'))),
                   dict(functionResponse=dict(name='ask_user_question',response=dict(answer='Preserve the public API')))]))]
        with self.chat.open('a',encoding='utf-8') as f:
            for row in rows:f.write(json.dumps(row)+'\n')
        session=self.request('Fix it');payload=json.loads((session/'input.json').read_text())
        goal,memory=conversation(payload)
        self.assertEqual(goal,'Fix it');self.assertIn('ledger.py',memory);self.assertIn('Preserve the public API',memory)
        self.assertNotIn('SOURCE_DUMP_NOT_NEEDED',memory)
        with self.chat.open('a',encoding='utf-8') as f:
            f.write(json.dumps(dict(type='system',subtype='chat_compression',systemPayload=dict(compressedHistory=[dict(parts=[dict(text='<state_snapshot>Preserve API; inspect ledger.py; work remains.</state_snapshot>')])])) )+'\n')
        goal,memory=conversation(payload)
        self.assertIn('Native retained context',memory);self.assertIn('Preserve API',memory)
        self.assertNotIn('SOURCE_DUMP_NOT_NEEDED',memory);self.assertEqual(goal,'Fix it')

    def test_native_transport_replay_returns_the_committed_outcome_without_side_effects(self):
        session=self.request();agent,calls=self.agent()
        with patch('visualize.open_view'):self.assertEqual(work(self.config,session,agent),0)
        state=apply_plan(State.load(session/'state.log'),parse_plan('ADD | 0 | TEST | | Unneeded duplicate\nDROP | 5 | Existing independent checks already cover this scope\nFINISH'))
        state.phase='complete';state.save(session/'state.log')
        self.permit()
        before={p:p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        with patch('interactive.start_process',side_effect=AssertionError('No duplicate worker')):
            first=execute(self.config,threading.Event(),lambda _:None)
            replay=execute(self.config,threading.Event(),lambda _:None)
        self.assertEqual(first,replay);self.assertIn('COMPLETE | 4/4 DONE',replay)
        self.assertEqual(before,{p:p.read_bytes() for p in self.project.rglob('*') if p.is_file()})
        self.assertEqual(len(calls),5);self.assertTrue(json.loads((self.home/'permit.json').read_text())['consumed'])

    def test_workspace_only_checkpoint_completion_can_be_delivered_without_repeating_workers(self):
        session=self.request();agent,calls=self.agent();baseline=clone(self.project,session/'workspace')
        atomic(session/'source-files.log',''.join(f'{p} | {h}\n' for p,h in baseline.items()))
        atomic(session/'goal.txt','Build the feature.');atomic(session/'memory.txt','Keep the original public contract.')
        State().save(session/'state.log')
        self.assertEqual(Harness(session,agent).run(),0)
        before=State.load(session/'state.log');self.assertFalse((self.project/'feature.txt').exists())
        self.assertIn('WORKSPACE COMPLETE',outcome(session))
        self.request('Resume '+str(session))
        with patch('visualize.open_view'),patch.object(agent,'call',side_effect=AssertionError('Accepted work must not repeat')):
            self.assertEqual(work(self.config,session,agent),0)
        self.assertEqual(before.tasks,State.load(session/'state.log').tasks)
        self.assertEqual(len(calls),5);self.assertTrue((self.project/'feature.txt').exists())
        self.assertTrue(outcome(session).startswith('Backtrackify COMPLETE |'))

    def test_interrupted_parent_resumes_saved_goal_after_new_user_prompt_without_replaying_children(self):
        session=self.request();base,calls=self.agent()
        class Interrupted:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if task_id==1:raise KeyboardInterrupt
                return base.call(role,prompt,workspace,structured,task_id)
        with patch('visualize.open_view'),self.assertRaises(KeyboardInterrupt):work(self.config,session,Interrupted())
        before=State.load(session/'state.log');self.assertEqual(before.tasks[1].status,'RUNNING')
        self.assertEqual(before.tasks[2].status,'DONE');self.assertEqual(before.tasks[3].status,'DONE')
        self.assertFalse((self.project/'feature.txt').exists())
        self.request('Resume '+str(session));resumed=[]
        class Resume:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                resumed.append((role,task_id))
                if role=='PROMPT':return {'operations':parse_plan('EDIT | 1 | Integrate feature\nFINISH')}
                return report(task_id)
        with patch('visualize.open_view'):self.assertEqual(work(self.config,session,Resume()),0)
        after=State.load(session/'state.log')
        self.assertEqual([id for _,id in resumed if id],[1,4]);self.assertEqual(after.tasks[1].attempts,2)
        for id in (2,3):self.assertEqual(after.tasks[id],before.tasks[id])
        self.assertEqual((session/'goal.txt').read_text().strip(),'Build the feature.')
        self.assertTrue((self.project/'feature.txt').exists())
        self.request('A different new task')
        with self.assertRaisesRegex(Refused,'explicit managed session path'):
            execute(self.config,threading.Event(),lambda _:None,resume=str(session))


@unittest.skipUnless(CLI.exists(),'Pinned Qwen CLI not installed')
class NativeIntegrationTests(Setup):
    # CPU fixture retains real native MCP, hooks, tools and harness subprocesses;
    # only Explorer window creation and model replies are controlled.
    def native(self,prompt,responder,stream=False,vision=False):
        (ROOT/'logs').mkdir(exist_ok=True)
        q=Qwen(CLI,'MID',32768,'unused',self.home,vision=vision)
        worker=self.root/'cpu_bridge.py'
        atomic(worker,"import sys\nfrom pathlib import Path\nif sys.argv[1]=='work':\n Path(sys.argv[-1],'startup.log').write_text('started')\nsys.path.insert(0,"+repr(str(ROOT))+")\nimport interactive\nfrom unittest.mock import patch\nfrom harness import start_process\nreal=start_process\ndef spawn(cmd,**kw):\n cmd=list(cmd);cmd[2]="+repr(str(worker))+"\n Path(cmd[-1],'spawn.log').write_text(repr(cmd))\n p=real(cmd,**kw)\n Path(cmd[-1],'spawned.log').write_text(str(p.pid))\n return p\nif sys.argv[1]=='serve':\n with patch('interactive.start_process',side_effect=spawn):interactive.main()\nelse:\n with patch('visualize.open_view',side_effect=OSError('CPU has no desktop')):raise SystemExit(interactive.main())\n")
        def setup(qwen,home,workspace,settings,args):
            flags=configure(qwen,home,workspace,settings,args)
            settings['mcpServers']['backtrackify']['args'][1]=str(worker)
            self.native_home=home
            return flags
        with Endpoint(False) as endpoint:
            endpoint.responder=responder;q.base_url=f'http://127.0.0.1:{endpoint.http.server_port}'
            output=self.root/'native.log'
            def spawn(cmd,**kw):
                with (ROOT/'logs/interactive-command.log').open('a',encoding='utf-8') as debug:debug.write(json.dumps(cmd,ensure_ascii=False)+'\n')
                kw['stdout']=log;kw['stderr']=subprocess.STDOUT
                if stream:
                    kw['stdin']=subprocess.PIPE
                    process=start_process(cmd,**kw)
                    for text in prompt:process.stdin.write((json.dumps(dict(type='user',message=dict(role='user',content=text)))+'\n').encode())
                    process.stdin.close()
                    wait=process.wait;process.wait=lambda timeout=None:wait(90 if timeout is None else timeout)
                    return process
                process=start_process(cmd,stdin=subprocess.DEVNULL,**kw)
                wait=process.wait
                process.wait=lambda timeout=None:wait(45 if timeout is None else timeout)
                return process
            with output.open('wb') as log,patch('interactive.configure',side_effect=setup),patch('harness.start_process',side_effect=spawn):
                try:code=q.call('ALGORITHM','',self.project,interactive=True,cli_args=(['--input-format','stream-json','--output-format','stream-json'] if stream else ['--output-format','stream-json','-p',prompt]))
                except subprocess.TimeoutExpired:code=124
            text=output.read_text(encoding='utf-8',errors='replace')
            with (ROOT/'logs/interactive-native-evidence.log').open('a',encoding='utf-8') as evidence:
                evidence.write('\nCASE '+str(prompt)+'\n'+text+'\n')
                for body in endpoint.requests:evidence.write(json.dumps(body,ensure_ascii=False)+'\n')
                evidence.write('CAPTURED '+str((self.native_home/'prompt.json').exists())+'\n')
                if (self.native_home/'prompt.json').exists():evidence.write('CAPTURE PAYLOAD '+(self.native_home/'prompt.json').read_text(encoding='utf-8')+'\n')
                if (self.native_home/'capture-error.log').exists():evidence.write('CAPTURE ERROR '+(self.native_home/'capture-error.log').read_text()+'\n')
                for path in self.native_home.glob('requests.log'):evidence.write('GATE '+path.read_text(encoding='utf-8')+'\n')
                for path in self.native_home.glob('native/projects/*/chats/*.jsonl'):evidence.write(path.read_text(encoding='utf-8')+'\n')
                for path in self.native_home.glob('native/debug/*.txt'):evidence.write(path.read_text(encoding='utf-8')+'\n')
                for path in self.native_home.glob('tasks/*/interactive.log'):evidence.write(path.read_text(encoding='utf-8',errors='replace')+'\n')
                for path in self.native_home.glob('tasks/*/*start*.log'):evidence.write('STARTUP '+path.name+' '+path.read_text()+'\n')
                for path in self.native_home.glob('tasks/*/spawn*.log'):evidence.write('SPAWN '+path.name+' '+path.read_text()+'\n')
            self.assertEqual(code,0,text[-5000:]);return endpoint.requests,text

    def test_real_cli_chat_and_read_do_not_activate_and_native_writes_fail_closed(self):
        for action in ('chat','read','write','shell','todo','plan','guard_failure'):
            def responder(body):
                tools=[m for m in body['messages'] if m.get('role')=='tool']
                if tools or action=='chat':return dict(role='assistant',content='Normal conversation continues.')
                name={'read':'read_file','write':'write_file','shell':'run_shell_command','todo':'todo_write',
                      'plan':'mcp__backtrackify__execute_task','guard_failure':'mcp__backtrackify__execute_task'}[action]
                args=dict(file_path=str(self.project/'old.txt'),**({} if action=='read' else dict(content='BYPASS')))
                if action=='shell':args=dict(command='echo BYPASS > old.txt')
                if action=='todo':args=dict(todos=[dict(content='Build feature',status='in_progress',activeForm='Building feature')])
                if action in {'plan','guard_failure'}:args={}
                return dict(role='assistant',content=None,tool_calls=[dict(id='call_direct',type='function',function=dict(name=name,arguments=json.dumps(args)))])
            original=configure
            def configured(*args):
                flags=original(*args)
                if action=='plan':flags+=['--approval-mode','plan']
                if action=='guard_failure':args[3]['hooks']['PreToolUse'][0]['hooks'][0]['command']='exit 1'
                return flags
            with patch(__name__+'.configure',side_effect=configured):
                requests,text=self.native('Explain the file; do not change it.',responder)
            self.assertEqual((self.project/'old.txt').read_text(),'preserve\n')
            self.assertFalse((self.native_home/'tasks').exists(),action+' '+text[-3000:])
            self.assertTrue(all('write_file' not in [t['function']['name'] for t in b.get('tools',[])] for b in requests))
            if action=='read':self.assertTrue(any('preserve' in str(m) for b in requests for m in b['messages'] if m['role']=='tool'))
            if action=='plan':
                init=next(json.loads(line) for line in text.splitlines() if line.startswith('{') and json.loads(line).get('subtype')=='init')
                self.assertEqual(init['permission_mode'],'plan');self.assertIn('read-only',text)
            if action=='guard_failure':self.assertIn('permission record',text)

    def responses(self,start_todo=False):
        def tool(name,args,id='call_cpu'):return dict(role='assistant',content=None,tool_calls=[dict(id=id,type='function',function=dict(name=name,arguments=json.dumps(args)))])
        def responder(body):
            def content(message):
                value=message.get('content','')
                return value if isinstance(value,str) else '\n'.join(p.get('text','') for p in value or [] if isinstance(p,dict))
            schemas={t['function']['name']:t['function'] for t in body.get('tools',[])}
            native=schemas.get('structured_output');previous=[m for m in body['messages'] if m.get('role')=='tool']
            if native:
                self.assertNotIn('mcp__backtrackify__execute_task',schemas)
                context='\n'.join(content(m) for m in body['messages'] if m['role']=='user')
                goal=re.search(r'Goal:\n(.*?)\nConstraints:',context,re.S)
                name='feature-b.txt' if goal and 'feature B' in goal[1] else 'feature-a.txt' if goal and 'feature A' in goal[1] else 'feature.txt'
                if 'operations' in native['parameters'].get('properties',{}):
                    return tool('structured_output',dict(operations=parse_plan(f'ADD | 0 | ALGORITHM | {name} | Implement feature\nADD | 0 | TEST | | Independently verify | 1'),terminal=dict(op='FINISH')))
                id=int(re.search(r'Assigned task (\d+):',context)[1])
                if not previous:
                    if id==1:
                        review=max(self.native_home.glob('tasks/*/reviews/1-*'),key=lambda p:p.stat().st_mtime_ns)
                        name=re.search(r'Assigned task 1: ([^|]+)',context)[1].strip()
                        return tool('write_file',dict(file_path=str(review/name),content='native accepted feature\n'))
                    return tool('run_shell_command',dict(command=f'python -c "from pathlib import Path; assert Path(\'{name}\').read_text()==\'native accepted feature\\n\'; print(\'CHECK PASSED\')"',description='Independent CPU verification'))
                return tool('structured_output',dict(report=report(id)))
            user=max(i for i,m in enumerate(body['messages']) if m['role']=='user')
            current=content(body['messages'][user])
            previous=[m for m in body['messages'][user+1:] if m['role']=='tool']
            if 'Explain' in current:return dict(role='assistant',content='Normal explanation; no execution needed.')
            if 'Read old.txt' in current:
                return dict(role='assistant',content='Read complete.') if previous else tool('read_file',dict(file_path=str(self.project/'old.txt')))
            if not previous:
                if start_todo:return tool('todo_write',dict(todos=[dict(content='Implement feature',status='in_progress',activeForm='Implementing feature')]))
                return tool('mcp__backtrackify__execute_task',{})
            if start_todo and len(previous)==1 and 'todo_write' in str(previous[0]):return tool('mcp__backtrackify__execute_task',{})
            if not any('Backtrackify COMPLETE' in str(m) for m in previous):return dict(role='assistant',content='Handoff error: '+str(previous))
            if sum('Backtrackify COMPLETE' in str(m) for m in previous)==1:return tool('mcp__backtrackify__execute_task',{},id='call_duplicate')
            return dict(role='assistant',content='Returned to the original Qwen conversation with accepted Backtrackify results.')
        return responder

    def test_real_cli_handoff_native_workers_and_return_without_duplicate_execution(self):
        requests,text=self.native('Implement and independently verify the feature.',self.responses(start_todo=True))
        self.assertEqual((self.project/'feature.txt').read_text(),'native accepted feature\n')
        tasks=list((self.native_home/'tasks').iterdir());self.assertEqual(len(tasks),1)
        state=State.load(tasks[0]/'state.log');self.assertEqual(state.actions,2);self.assertTrue(state.complete())
        self.assertIn('Returned to the original Qwen conversation',text)
        self.assertTrue(any('Backtrackify COMPLETE' in str(m) for b in requests for m in b['messages'] if m['role']=='tool'))
        worker_logs=list((tasks[0]/'turns').glob('*/settings.json'))
        self.assertEqual(len(worker_logs),3)
        self.assertTrue(all(json.loads(p.read_text())['disableAllHooks'] for p in worker_logs))

    def test_real_native_image_is_bound_to_mcp_task_and_only_prompt_sees_pixels(self):
        import vision
        from tests.test_vision import png
        (self.project/'image.png').write_bytes(png())
        requests,text=self.native('@image.png Implement and independently verify the feature.',self.responses(),vision=True)
        tasks=list((self.native_home/'tasks').iterdir());self.assertEqual(len(tasks),1,text[-3000:])
        self.assertTrue(State.load(tasks[0]/'state.log').complete());self.assertEqual(len(vision.task_images(tasks[0])),1)
        self.assertIn('@image.png',(tasks[0]/'goal.txt').read_text())
        seen=set()
        for body in requests:
            context=str(body['messages'][0])
            if 'You are PROMPT' in context:
                self.assertTrue(vision.has_images(body['messages']));seen.add('PROMPT')
            elif 'Assigned task ' in str(body['messages']):
                self.assertFalse(vision.has_images(body['messages']));seen.add('WORKER')
        self.assertEqual(seen,{'PROMPT','WORKER'})

    def test_same_native_cli_handles_chat_read_and_two_new_user_tasks(self):
        requests,text=self.native(['Explain what you can do.','Read old.txt and summarize it.',
            'Implement and verify feature A.','Explain what changed.','Implement and verify feature B.'],self.responses(),stream=True)
        tasks=list((self.native_home/'tasks').iterdir());self.assertEqual(len(tasks),2,text[-5000:])
        self.assertTrue(all(State.load(p/'state.log').complete() for p in tasks))
        self.assertTrue((self.project/'feature-a.txt').exists());self.assertTrue((self.project/'feature-b.txt').exists())
        self.assertEqual((self.project/'old.txt').read_text(),'preserve\n')
        goals={(p/'goal.txt').read_text().strip() for p in tasks}
        self.assertEqual(goals,{'Implement and verify feature A.','Implement and verify feature B.'})


@unittest.skipUnless(CLI.exists() and os.name=='nt','Pinned Windows CLI required')
class ExternalProjectRoutingTests(unittest.TestCase):
    def test_absolute_launcher_routes_native_mcp_workers_and_delivery_to_external_project(self):
        for spaced in (False,True):
            with self.subTest(spaces=spaced),tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='r-') as tmp,Endpoint(False) as endpoint:
                root=Path(tmp);app=root/('A with spaces' if spaced else 'A');app.mkdir()
                project=root/('B with spaces' if spaced else 'B');project.mkdir();atomic(project/'existing.txt','preserve')
                for name in ('qwen.cmd','launch-qwen.ps1','setup.ps1','interactive.py','vision.py'):(app/name).write_bytes((ROOT/name).read_bytes())
                for role in ('prompt','algorithm','test'):atomic(app/'agents'/(role+'.md'),(ROOT/'agents'/(role+'.md')).read_text(encoding='utf-8'))
                source=(ROOT/'harness.py').read_text(encoding='utf-8')
                source=source.replace("if __name__ == '__main__':","if __name__ == '__main__':\n    gguf_metadata=lambda _: {'qwen35.context_length':32768,'qwen35.nextn_predict_layers':1}")
                atomic(app/'harness.py',source)
                url='http://127.0.0.1:'+str(endpoint.http.server_port)
                atomic(app/'backend.py','from contextlib import contextmanager\nfrom types import SimpleNamespace\n@contextmanager\ndef connection(*args):\n yield SimpleNamespace(url='+repr(url)+',vision=True)\n')
                atomic(app/'visualize.py','def open_view(*args,**kwargs):pass\ndef owner_ended(*args):return True\n')
                atomic(app/'install.py',"import sys\nassert sys.argv[1]=='check', 'No installation is allowed in this fixture'\n")
                assets=CLI.parents[4]
                atomic(app/'local.ps1',"$Profile='MID'\n$Context=32768\n$Batch=512\n$Microbatch=256\n$Threads=16\n$Gpu=0\n$Port=8080\n$ChatPort=0\n$SpecType='draft-mtp,ngram-mod'\n$AgentSeconds=45\n$AssetRoot='"+str(assets).replace("'","''")+"'\n")
                seen=[]
                def tool(name,args):return dict(role='assistant',content=None,tool_calls=[dict(id='call_cpu',type='function',function=dict(name=name,arguments=json.dumps(args)))])
                def response(body):
                    with (root/'model-calls.log').open('a',encoding='utf-8') as log:log.write(str(len(body['messages']))+' '+str([t['function']['name'] for t in body.get('tools',[])])+'\n')
                    schema=None
                    for entry in body.get('tools',[]):
                        if entry['function']['name']=='structured_output':schema=entry['function']
                    previous=[]
                    for m in body['messages']:
                        if m['role']=='tool':previous.append(m)
                    if schema:
                        system=body['messages'][0]['content'];match=re.search(r'^Execution workspace: (.+)$',system,re.M)
                        self.assertIsNotNone(match,'Worker must receive its real workspace explicitly');workspace=Path(match[1].strip())
                        self.assertTrue(workspace.is_relative_to(app/'sessions'));self.assertIn('Task notes cannot redefine it',system)
                        self.assertFalse((app/'feature.txt').exists());self.assertFalse((project/'feature.txt').exists())
                        if 'operations' in schema['parameters'].get('properties',{}):
                            seen.append('PROMPT')
                            return tool('structured_output',dict(operations=parse_plan('ADD | 0 | ALGORITHM | feature.txt | Create accepted feature\nADD | 0 | TEST | | Verify feature | 1'),terminal=dict(op='FINISH'),task_notes='Misleading historical claim: project root is '+str(app)))
                        context=str(body['messages']);self.assertIn('Misleading historical claim',context)
                        task=int(re.search(r'Assigned task (\d+):',context)[1]);seen.append('WORKER '+str(task))
                        if not previous:return tool('write_file',dict(file_path=str(workspace/'feature.txt'),content='accepted in isolated copy\n')) if task==1 else tool('read_file',dict(file_path=str(workspace/'feature.txt')))
                        if task==2:self.assertIn('accepted in isolated copy',str(previous))
                        return tool('structured_output',dict(report=report(task)))
                    if not previous:return tool('mcp__backtrackify__execute_task',{})
                    result=str(previous);self.assertIn('Delivery project: '+str(project).replace('\\','\\\\'),result)
                    self.assertIn('Saved: feature.txt',result);seen.append('RETURN')
                    return dict(role='assistant',content='Completed in '+str(project)+': feature.txt. No internal search needed.')
                endpoint.responder=response
                env=dict(os.environ);env.pop('BACKTRACKIFY_WORKER',None);env.pop('BACKTRACKIFY_TEST_ROOT',None)
                script="& '"+str(app/'qwen.cmd').replace("'","''")+"' --output-format stream-json -p 'Create and verify a feature in this project.'"
                command=['powershell.exe','-NoLogo','-NoProfile','-Command',script]
                process=start_process(command,cwd=project,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
                try:output,_=process.communicate(timeout=65)
                finally:stop_process(process)
                self.assertEqual(process.returncode,0,output[-7000:])
                self.assertEqual((project/'feature.txt').read_text(),'accepted in isolated copy\n');self.assertEqual((project/'existing.txt').read_text(),'preserve')
                self.assertFalse((app/'feature.txt').exists());self.assertIn('RETURN',seen)
                task,=list(app.glob('sessions/interactive-*/turns/*/tasks/*'));self.assertTrue(State.load(task/'state.log').complete())
                config=json.loads(task.parent.parent.joinpath('interactive.json').read_text(encoding='utf-8'));self.assertEqual(config['project'],str(project))
                self.assertIn('Delivery project: '+str(project),outcome(task))


if __name__=='__main__':unittest.main()
