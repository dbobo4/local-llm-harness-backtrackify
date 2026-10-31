"""Real pinned CLI against a controlled HTTP endpoint. No GPU/model claim."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import http.client
import socketserver
import urllib.request

from harness import ROOT, PLAN_SCHEMA, PROFILES, State, Qwen, InferenceGate, AgentFailure, Refused, apply_plan, atomic, execute, generation, parse_plan, qwen_result, request, start_process, REPORT_SCHEMA, COMPRESSION_PREFIX, COMPRESSION_DIRECTIVE, COMPRESSION_GRAMMAR, compression_request, compression_summary
import urllib.error

CLI = Path(os.environ.get('BACKTRACKIFY_NATIVE_CLI', str(ROOT / 'runtime/qwen/qwen-code/lib/cli.js')))

OVERSIZED_PLAN = {'operations':[
    dict(op='ADD',parent_id=0,role='ALGORITHM',path='calc.py',summary='Build'),
    dict(op='ADD',parent_id=1,role='TEST',path='tests/test_calc.py',summary='x'*281),
    dict(op='FINISH')]}

# Original native dependency shape: longflow-adf8ccd4, PLAN 1. Scope labels shortened; IDs unchanged.
INITIAL_SELF_NEEDS = {'operations':[
    dict(op='ADD',parent_id=0,role='ALGORITHM',summary='Implement library',needs=[1,2,3,4]),
    dict(op='ADD',parent_id=0,role='ALGORITHM',summary='Implement CLI',needs=[1]),
    dict(op='ADD',parent_id=0,role='TEST',summary='Independently check library',needs=[1]),
    dict(op='ADD',parent_id=0,role='TEST',summary='Independently check CLI',needs=[2]),
    dict(op='ADD',parent_id=0,role='TEST',summary='Release check',needs=[1,2,3,4])], 'terminal':{'op':'FINISH'}}


class Endpoint:
    def __init__(self, structured):
        self.requests, self.structured = [], structured
        self.image_counts=[]
        self.write_path = ''
        self.write_content = 'native tools work\n'
        self.token_count = 150
        self.token_count_after_tool = None
        self.finish_stream = True
        self.send_sentinel = True
        self.first_plan = None
        self.repeat_plan = False
        self.text_plan = None
        self.first_read = None
        self.first_tools = None
        self.tool_rounds = []
        self.context_by_history = False
        self.template_tokens = 150
        self.reasoning_deltas = []
        self.length_requests = set()
        self.repeat_write_until_file = False
        self.usage_prompt_tokens = 150
        self.compactions = 0
        self.compact_response = False
        self.compact_summaries = []
        self.read_after_compaction = False
        self.compact_input_tokens = None
        self.token_count_after_compaction = None
        self.final_only = False
        self.worker_report = {'task_id':1,'status':'DONE','summary':'Native tool work completed with a structured report'}
        self.first_report = None
        self.plain_final = False
        self.shell_command = None
        self.pause = None
        self.responder = None
        self.entered = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/v1/chat/completions/input_tokens':
                    owner.image_counts.append(body)
                    self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                    self.wfile.write(json.dumps(dict(input_tokens=owner.token_count)).encode());return
                if self.path in {'/apply-template','/tokenize'}:
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    if owner.context_by_history:
                        if self.path=='/apply-template':
                            owner.template_tokens=owner.token_count_after_tool if any(m['role']=='tool' for m in body.get('messages',[])) else owner.token_count
                            if owner.compactions and owner.token_count_after_compaction is not None:
                                owner.template_tokens=owner.token_count_after_compaction
                        self.wfile.write(json.dumps({'prompt':'native test prompt'} if self.path=='/apply-template' else {'tokens':[1]*owner.template_tokens}).encode())
                        return
                    tokens=owner.token_count_after_tool if owner.requests and owner.token_count_after_tool is not None else owner.token_count
                    if owner.compactions and owner.token_count_after_compaction is not None:
                        tokens=owner.token_count_after_compaction
                    self.wfile.write(json.dumps({'prompt':'native test prompt'} if self.path=='/apply-template' else {'tokens':[1]*tokens}).encode())
                    return
                owner.requests.append(body)
                if owner.pause:
                    owner.entered.set()
                    owner.pause.wait(10)
                compact = owner.compact_response and not body.get('tools')
                if owner.responder:
                    response=owner.responder(body)
                elif owner.final_only and not any(t['function']['name']=='structured_output' for t in body.get('tools',[])):
                    response={'role':'assistant','content':'DONE: interactive local connection works.'}
                elif owner.final_only:
                    response={'role':'assistant','content':None,'tool_calls':[{'id':'call_report','type':'function','function':{'name':'structured_output','arguments':json.dumps({'report':owner.worker_report})}}]}
                elif compact:
                    owner.compactions += 1
                    summary=owner.compact_summaries.pop(0) if owner.compact_summaries else '<state_snapshot><goal>Write probe.txt and report DONE.</goal><decisions>Preserve the file.</decisions><work>The file tool completed.</work><errors>None.</errors><next>Give the final report.</next></state_snapshot>'
                    response={'role':'assistant','content':summary}
                elif owner.read_after_compaction and owner.compactions==1:
                    response={'role':'assistant','content':None,'tool_calls':[{'id':'call_retained_read','type':'function',
                        'function':{'name':'read_file','arguments':json.dumps({'file_path':owner.write_path})}}]}
                elif owner.first_read and len(owner.requests)==1:
                    response={'role':'assistant','content':None,'tool_calls':[{'id':'call_read','type':'function',
                        'function':{'name':'read_file','arguments':json.dumps({'file_path':owner.first_read})}}]}
                elif owner.first_tools and len(owner.requests)==1:
                    response={'role':'assistant','content':None,'tool_calls':owner.first_tools}
                elif owner.tool_rounds:
                    response={'role':'assistant','content':None,'tool_calls':owner.tool_rounds.pop(0)}
                elif owner.structured and not any(t['function']['name']=='structured_output' for t in body.get('tools',[])):
                    response={'role':'assistant','content':owner.text_plan or 'ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Verify\nFINISH'}
                elif owner.structured:
                    plan = owner.first_plan if owner.first_plan is not None and (len(owner.requests)==1 or owner.repeat_plan) else {'operations':parse_plan('ADD | 0 | ALGORITHM | calc.py | Build\nADD | 0 | TEST | tests/test_calc.py | Test'), 'terminal':{'op':'FINISH'}}
                    response = {'role':'assistant','content':None,'tool_calls':[{'id':'call_plan','type':'function',
                        'function':{'name':'structured_output','arguments':json.dumps(plan)}}]}
                elif not any(m['role']=='tool' for m in body['messages']) or (owner.repeat_write_until_file and not Path(owner.write_path).exists()):
                    response = {'role':'assistant','content':None,'tool_calls':[{'id':'call_write','type':'function',
                        'function':{'name':'run_shell_command' if owner.shell_command else 'write_file','arguments':json.dumps({'command':owner.shell_command,'description':'Delegated CPU check'} if owner.shell_command else {'file_path':owner.write_path,
                            'content':owner.write_content[:3] if len(owner.requests) in owner.length_requests else owner.write_content})}}]}
                elif owner.plain_final:
                    response={'role':'assistant','content':'DONE: a plain answer cannot replace the report tool.'}
                else:
                    value=owner.first_report if owner.first_report is not None and len(owner.requests)==2 else owner.worker_report
                    response = {'role':'assistant','content':None,'tool_calls':[{'id':'call_report','type':'function',
                        'function':{'name':'structured_output','arguments':json.dumps({'report':value})}}]}
                finish = 'tool_calls' if response.get('tool_calls') else 'stop'
                if len(owner.requests) in owner.length_requests:
                    finish = 'length'
                self.send_response(200)
                if body.get('stream'):
                    self.send_header('Content-Type','text/event-stream'); self.end_headers()
                    if not any(m['role']=='tool' for m in body['messages']):
                        for thought in owner.reasoning_deltas:
                            event={'id':'chatcmpl-test','object':'chat.completion.chunk','created':1,'model':'backtrackify',
                                   'choices':[{'index':0,'delta':{'role':'assistant','reasoning_content':thought},'finish_reason':None}]}
                            try:
                                self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
                                self.wfile.flush()
                            except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):
                                return  # Expected when the real CLI stops a genuine loop.
                    delta = dict(response)
                    if delta.get('tool_calls'):
                        for index,call in enumerate(delta['tool_calls']):call['index']=index
                    chunk = {'id':'chatcmpl-test','object':'chat.completion.chunk','created':1,'model':'backtrackify',
                        'choices':[{'index':0,'delta':delta,'finish_reason':None}]}
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                    chunk['choices']=[{'index':0,'delta':{},'finish_reason':finish}]
                    prompt_tokens=owner.compact_input_tokens if compact and owner.compact_input_tokens is not None else owner.usage_prompt_tokens
                    chunk['usage']={'prompt_tokens':prompt_tokens,'completion_tokens':50,'total_tokens':prompt_tokens+50}
                    if owner.finish_stream:
                        self.wfile.write(('data: '+json.dumps(chunk)+'\n\n'+('data: [DONE]\n\n' if owner.send_sentinel else '')).encode())
                    self.wfile.flush()
                else:
                    self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'id':'chatcmpl-test','object':'chat.completion','created':1,'model':'backtrackify',
                        'choices':[{'index':0,'message':response,'finish_reason':finish}],
                        'usage':{'prompt_tokens':150,'completion_tokens':50,'total_tokens':200}}).encode())
        self.http = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.http.serve_forever,daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self,*args):
        if self.pause: self.pause.set()
        self.http.shutdown();self.http.server_close();self.thread.join()


@unittest.skipUnless(CLI.exists(), 'NOT RUN: pinned Qwen CLI not installed; state-machine tests remain independent')
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.deadline=patch('harness.execute',side_effect=lambda cmd,cwd,timeout,*args:execute(cmd,cwd,min(timeout,120),*args))
        self.deadline.start();self.addCleanup(self.deadline.stop)

    def test_prompt_native_at_image_and_structured_output_use_exact_multimodal_count(self):
        import vision
        from tests.test_vision import png
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='Vision native spaces ') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'review';workspace.mkdir();id=vision.store(root,png(),'image/png')
            atomic(root/'task-images.txt','Backtrackify task images 1\nrequest '+root.name+'\nnative native-1\nuser user-1\n'+id+'\n')
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root,vision=True)
            plan=q.call('PROMPT','Interpret this task image, then plan.',workspace,structured=True)
            self.assertEqual(plan['operations'][-1]['op'],'FINISH');self.assertTrue(endpoint.image_counts)
            self.assertTrue(vision.has_images(endpoint.requests[0]['messages']))
            self.assertEqual(endpoint.image_counts[0],endpoint.requests[0])
            settings=json.loads(next(root.glob('turns/*/settings.json')).read_text())
            self.assertEqual(settings['model']['chatCompression']['maxRecentImagesToRetain'],0)
            self.assertTrue(settings['modelProviders']['openai'][0]['generationConfig']['modalities']['image'])

    def test_prompt_replan_only_reopens_original_image_on_explicit_read(self):
        import vision
        from tests.test_vision import png
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'review';workspace.mkdir();id=vision.store(root,png(),'image/png')
            atomic(root/'task-images.txt','Backtrackify task images 1\nrequest '+root.name+'\nnative native-1\nuser user-1\n'+id+'\n')
            state=State();state.replans=2;state.save(root/'state.log')
            endpoint.first_read=str(vision.image_path(root,id))
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root,vision=True)
            q.call('PROMPT','Re-examine the original task image using read_file: '+endpoint.first_read,workspace,structured=True)
            self.assertFalse(vision.has_images(endpoint.requests[0]['messages']))
            self.assertTrue(vision.has_images(endpoint.requests[-1]['messages']));self.assertTrue(endpoint.image_counts)

    def test_worker_provider_and_gate_remain_text_only_with_vision_backend(self):
        from tests.test_vision import png
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir();image=workspace/'probe.png';image.write_bytes(png())
            endpoint.final_only=True
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root,vision=True)
            result=q.call('TEST','Inspect @"'+str(image)+'" and report.',workspace,task_id=1)
            self.assertEqual(result['status'],'DONE');self.assertFalse(endpoint.image_counts)
            settings=json.loads(next(root.glob('turns/*/settings.json')).read_text())
            self.assertFalse(settings['modelProviders']['openai'][0]['generationConfig']['modalities']['image'])

    def test_native_image_compaction_keeps_xml_contract_and_disables_extra_image_restoration(self):
        import vision
        from tests.test_vision import png
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'review';workspace.mkdir();id=vision.store(root,png(),'image/png')
            atomic(root/'task-images.txt','Backtrackify task images 1\nrequest '+root.name+'\nnative native-1\nuser user-1\n'+id+'\n')
            endpoint.first_read=str(vision.image_path(root,id));endpoint.usage_prompt_tokens=19000
            endpoint.compact_input_tokens=4000;endpoint.compact_response=True
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root,vision=True)
            q.call('PROMPT','Examine the image, read it again to confirm, then plan.',workspace,structured=True)
            self.assertEqual(endpoint.compactions,1);self.assertTrue(vision.has_images(endpoint.requests[0]['messages']))
            last=endpoint.requests[-1];self.assertIn('<state_snapshot>',str(last['messages']))
            settings=json.loads(next(root.glob('turns/*/settings.json')).read_text())
            self.assertEqual(settings['model']['chatCompression']['maxRecentImagesToRetain'],0)
            compact=next(b for b in endpoint.requests if b.get('grammar'))
            self.assertEqual(compact['grammar'],COMPRESSION_GRAMMAR);self.assertEqual(compact['thinking_budget_tokens'],0)

    def test_repeated_native_compaction_replaces_memory_without_duplicate_snapshots(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.write_path=str(workspace/'probe.txt')
            endpoint.usage_prompt_tokens=20000;endpoint.compact_input_tokens=4000;endpoint.compact_response=True;endpoint.read_after_compaction=True
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            self.assertEqual(q.call('ALGORITHM','Write probe.txt and preserve it, then report',workspace,task_id=1)['status'],'DONE')
            self.assertEqual(endpoint.compactions,2)
            last=endpoint.requests[-1]
            self.assertEqual(str(last['messages']).count('<state_snapshot>'),1)
            self.assertIn('Preserve the file',str(last['messages']))
            self.assertEqual((last['thinking_budget_tokens'],last['max_tokens']),(8192,11264))
            self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)

    def test_invalid_native_compaction_preserves_original_history_and_is_not_done(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.write_path=str(workspace/'probe.txt')
            endpoint.usage_prompt_tokens=20000;endpoint.compact_input_tokens=4000;endpoint.compact_response=True
            endpoint.compact_summaries=['<state_snapshot><goal>Unclosed']
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            with self.assertRaises(AgentFailure) as failure:q.call('ALGORITHM','Write probe.txt then report',workspace,task_id=1)
            self.assertEqual(failure.exception.kind,'context');self.assertIn('snapshot',str(failure.exception))
            self.assertTrue((workspace/'probe.txt').exists())  # Partial work remains available to backtracking.
            self.assertTrue(any(c.get('summary_error') for c in q.calls))
            for log in (root/'turns').glob('*/native/projects/*/chats/*.jsonl'):
                rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
                self.assertFalse(any(row.get('subtype')=='chat_compression' for row in rows))
                self.assertIn('Write probe.txt then report',str(rows))

    def test_native_nested_plan_keeps_parent_return_and_cross_scope_prerequisites(self):
        from tests.test_harness import running,report
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.first_plan={'operations':parse_plan('ADD | 0 | ALGORITHM | | Integrate validators\n'
                'ADD | 0 | TEST | | Check combined release | 1\nADD | 1 | ALGORITHM | | Implement validators\n'
                'ADD | 1 | TEST | | Verify validators | 3'),'terminal':{'op':'FINISH'}}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            state=apply_plan(State(),q.call('PROMPT','Plan subordinate validator work and release checks.',workspace,structured=True)['operations'])
            order=[]
            while task:=state.eligible():
                order.append(task.id)
                from harness import apply_report
                state=apply_report(running(state,task.id),task.id,report(task.id))
            self.assertEqual(order,[3,4,1,2]);self.assertTrue(state.complete())

    def test_actual_session_turn_cap_is_limit_after_real_file_tool_not_stream_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.write_path=str(workspace/'partial.txt')
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            def small_cap(command,cwd,timeout,*args):
                command=list(command);command[command.index('--max-session-turns')+1]='1'
                return execute(command,cwd,min(timeout,120),*args)
            with patch('harness.execute',side_effect=small_cap),self.assertRaises(AgentFailure) as failure:
                q.call('ALGORITHM','Write the assigned file, then give your required report.',workspace,task_id=1)
            self.assertEqual(failure.exception.kind,'limit');self.assertIn('session-turn budget',str(failure.exception))
            self.assertTrue((workspace/'partial.txt').exists())
            raw=next((root/'turns').glob('*/qwen.log')).read_text(encoding='utf-8')
            self.assertIn('Reached max session turns',raw)
            self.assertNotIn('"structured_result"',raw)

    def test_pinned_turn_accounting_batches_tools_and_excludes_native_compression(self):
        for turns in (1,2):
            with self.subTest(turns=turns),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                endpoint.first_tools=[{'id':'write_'+name,'type':'function','function':{'name':'write_file',
                    'arguments':json.dumps({'file_path':str(workspace/name),'content':'accepted tool output'})}} for name in ('a.txt','b.txt')]
                endpoint.usage_prompt_tokens=20000;endpoint.compact_input_tokens=4000;endpoint.compact_response=True
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                def bounded(command,cwd,timeout,*args):
                    command=list(command);command[command.index('--max-session-turns')+1]=str(turns)
                    command[command.index('--max-tool-calls')+1]='2'
                    return execute(command,cwd,min(timeout,120),*args)
                with patch('harness.execute',side_effect=bounded):
                    if turns==1:
                        with self.assertRaises(AgentFailure) as failure:q.call('ALGORITHM','Write both files, then report.',workspace,task_id=1)
                        self.assertEqual(failure.exception.kind,'limit');self.assertIn('session-turn',str(failure.exception))
                    else:
                        self.assertEqual(q.call('ALGORITHM','Write both files, then report.',workspace,task_id=1)['status'],'DONE')
                self.assertEqual([p.read_text() for p in sorted(workspace.glob('*.txt'))],['accepted tool output']*2)
                self.assertEqual(len(endpoint.requests),1 if turns==1 else 3)
                self.assertEqual(endpoint.compactions,0 if turns==1 else 1)
                if turns==2:
                    self.assertEqual(sum(bool(c.get('compression')) for c in q.calls),1)
                    self.assertTrue(all(c['complete'] for c in q.calls))

    @unittest.skipUnless(os.name=='nt','Windows native process/job observation')
    def test_pinned_cli_parent_child_overlap_is_one_agent_inside_the_owned_job(self):
        import ctypes
        from ctypes import wintypes as w
        class Entry(ctypes.Structure):
            _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                      ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
                      ('flags',w.DWORD),('name',w.WCHAR*260)]
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];api.CreateToolhelp32Snapshot.restype=w.HANDLE
        api.Process32FirstW.argtypes=api.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        api.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];api.OpenProcess.restype=w.HANDLE
        api.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)]
        api.CloseHandle.argtypes=[w.HANDLE]
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.final_only=True;endpoint.pause=threading.Event()
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            roots,results,errors=[],[],[]
            def spawn(*args,**kwargs):
                p=start_process(*args,**kwargs);roots.append(p);return p
            def run():
                try:results.append(q.call('ALGORITHM','Report DONE without tools.',workspace,task_id=1))
                except BaseException as e:errors.append(e)
            with patch('harness.start_process',side_effect=spawn):
                thread=threading.Thread(target=run);thread.start()
                try:
                    self.assertTrue(endpoint.entered.wait(15),'Pinned CLI did not reach the controlled backend')
                    self.assertEqual(len(roots),1)
                    snapshot=api.CreateToolhelp32Snapshot(2,0);entry=Entry(size=ctypes.sizeof(Entry));children=[]
                    try:
                        ok=api.Process32FirstW(snapshot,ctypes.byref(entry))
                        while ok:
                            if entry.parent==roots[0].pid and entry.name=='node.exe':children.append(entry.pid)
                            ok=api.Process32NextW(snapshot,ctypes.byref(entry))
                    finally:api.CloseHandle(snapshot)
                    self.assertEqual(len(children),1)  # Native heap-policy relaunch, not a second harness call.
                    child=api.OpenProcess(0x400,False,children[0]);self.assertTrue(child)
                    try:
                        member=w.BOOL()
                        self.assertTrue(api.IsProcessInJob(child,roots[0]._backtrackify_job.handle,ctypes.byref(member)))
                        self.assertTrue(member.value)
                    finally:api.CloseHandle(child)
                finally:
                    endpoint.pause.set();thread.join(20)
            self.assertFalse(thread.is_alive());self.assertFalse(errors,errors)
            self.assertEqual(results[0]['status'],'DONE');self.assertEqual(len(roots),1)

    def test_user_qwen_launch_uses_existing_gate_and_default_approval(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.final_only=True
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            def spawn(*args,**kwargs):
                import subprocess
                return start_process(*args,stdin=subprocess.DEVNULL,**kwargs)  # CPU headless stand-in; real TUI inherits its terminal.
            with patch('harness.start_process',side_effect=spawn) as spawned:
                code=q.call('ALGORITHM','',workspace,interactive=True,cli_args=['-p','Reply DONE without using tools.'])
            self.assertEqual(code,0)
            args=spawned.call_args.args[0]
            self.assertNotIn('--approval-mode',args)
            self.assertNotIn('--system-prompt',args)
            self.assertNotIn('CI',spawned.call_args.kwargs['env'])
            self.assertEqual(spawned.call_args.kwargs['env']['QWEN_CODE_LAUNCHER_PATH'],str(CLI.parent.parent/'bin/qwen.cmd'))
            self.assertTrue(endpoint.requests)
            self.assertEqual((endpoint.requests[0]['thinking_budget_tokens'],endpoint.requests[0]['max_tokens']),(8192,11264))

    def test_native_bad_terminal_enum_required_fields_and_embedded_terminal_are_rejected(self):
        valid={'operations':[dict(op='ADD',parent_id=0,role='ALGORITHM',path='calc.py',summary='Build')], 'terminal':{'op':'FINISH'}}
        variants=[]
        bad=json.loads(json.dumps(valid));bad['terminal']={'op':'DONE'};variants.append(bad)
        bad=json.loads(json.dumps(valid));bad['terminal']={'op':'BLOCKED'};variants.append(bad)
        bad=json.loads(json.dumps(valid));bad['operations'][0]['role']='PROMPT';variants.append(bad)
        bad=json.loads(json.dumps(valid));del bad['operations'][0]['summary'];variants.append(bad)
        bad=json.loads(json.dumps(valid));bad['operations'].append({'op':'FINISH'});variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.first_plan=bad
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                response=q.call('PROMPT','Submit valid plan operations and the terminal.',workspace,True)
                self.assertGreaterEqual(len(endpoint.requests),2)
                self.assertTrue(apply_plan(State(),response['operations']).closed)

    def test_native_context_overflow_with_rejected_compaction_uses_fresh_text_fallback(self):
        from harness import Harness
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            atomic(workspace/'brief.txt','Inspect these requirements before planning.')
            atomic(root/'goal.txt','Build and check a local project.');atomic(root/'memory.txt','Preserve requirements.\n')
            State().save(root/'state.log')
            endpoint.first_read=str(workspace/'brief.txt');endpoint.context_by_history=True
            endpoint.token_count_after_tool=21260  # 21260 + 11264 + 256 = 32780, twelve too many.
            endpoint.compact_response=True;endpoint.compact_input_tokens=4000
            endpoint.compact_summaries=['<state_snapshot><goal>Incomplete']
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            h=Harness(root,q);h.plan()
            self.assertEqual(q.counter,2);self.assertEqual(len(endpoint.requests),3)
            self.assertEqual(endpoint.compactions,1)
            self.assertTrue(h.state.closed);self.assertEqual(h.state.next_id,3)
            self.assertFalse(any(m['role']=='tool' for m in endpoint.requests[-1]['messages']))
            self.assertIn('Native compaction',str(h.state.events))
            self.assertIn('prompt 21260 + generation 11264',next((root/'turns').glob('*/requests.log')).read_text())
            self.assertEqual([t.summary for t in h.state.tasks.values()],['Build','Verify'])

    def test_native_optional_path_unicode_and_stop_only_terminal(self):
        summary='\u00e9'*279+'\U0001f40d'  # 280 characters, more UTF-8 bytes and UTF-16 code units.
        plans=[{'operations':[dict(op='ADD',parent_id=0,role='TEST',summary=summary)],'terminal':{'op':'FINISH'}},
               {'operations':[],'terminal':{'op':'BLOCKED','reason':'Required permission is unavailable'}}]
        for plan in plans:
            with self.subTest(terminal=plan['terminal']['op']),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.first_plan=plan
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                value=q.call('PROMPT','Submit the valid planning batch or a stop-only obstacle.',workspace,True)
                self.assertEqual(value['operations'],plan['operations']+[plan['terminal']])
                state=apply_plan(State(),value['operations']);state.save(root/'state.log')
                loaded=State.load(root/'state.log')
                if plan['terminal']['op']=='FINISH':
                    self.assertEqual(loaded.tasks[1].summary,summary);self.assertEqual(loaded.tasks[1].path,'')
                    self.assertEqual(loaded.eligible().id,1);self.assertFalse(loaded.complete())
                else:
                    self.assertEqual(loaded.phase,'blocked');self.assertEqual(loaded.tasks,{})
                    self.assertIn('permission',loaded.reason);self.assertIsNone(loaded.eligible())
                self.assertEqual(len(endpoint.requests),1)

    def test_native_terminal_is_required_before_cli_acceptance(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.first_plan={'operations':parse_plan('ADD | 0 | ALGORITHM | calc.py | Build\nADD | 0 | TEST | tests/test_calc.py | Test')}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            response=q.call('PROMPT','Submit a small plan.',workspace,True)
            self.assertGreaterEqual(len(endpoint.requests),2)  # Native CLI rejects the observed missing terminal.
            self.assertIn('terminal', str(endpoint.requests[1]['messages']))
            self.assertEqual(response['operations'][-1], {'op':'FINISH'})
            self.assertTrue(apply_plan(State(),response['operations']).closed)

    def test_native_needs_and_reopen_use_the_existing_structured_tool(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Check | 1\nFINISH'))
            for task in state.tasks.values():task.status,task.attempts,task.result='DONE',1,'Earlier accepted report'
            state.validate()
            endpoint.first_plan={'operations':[
                dict(op='REOPEN',id=1,reason='Later verification exposed a defect'),
                dict(op='ADD',parent_id=1,role='ALGORITHM',summary='Narrow repair',needs=[]),
                dict(op='NEEDS',id=3,needs=[])], 'terminal':{'op':'FINISH'}}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            response=q.call('PROMPT','Reopen the completed prerequisite and plan its repair.',workspace,True)
            updated=apply_plan(state,response['operations'])
            self.assertEqual(len(endpoint.requests),1)
            self.assertEqual(updated.tasks[1].status,'WAITING');self.assertEqual(updated.tasks[2].kind,'invalidated')
            self.assertEqual(updated.tasks[2].needs,[1]);self.assertEqual(updated.tasks[1].attempts,1)
            tool=next(t for t in endpoint.requests[0]['tools'] if t['function']['name']=='structured_output')
            self.assertIn('REOPEN',json.dumps(tool['function']['parameters']))

    def test_native_needs_item_validation_precedes_harness_semantics(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.first_plan={'operations':[dict(op='ADD',parent_id=0,role='TEST',summary='Check',needs=[0])],
                                 'terminal':{'op':'FINISH'}}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            value=q.call('PROMPT','Submit a valid plan.',workspace,True)
            self.assertGreaterEqual(len(endpoint.requests),2)
            self.assertIn('needs',str(endpoint.requests[1]['messages']))
            self.assertTrue(apply_plan(State(),value['operations']).closed)

    def test_native_cycle_rejection_reaches_fresh_strict_text_correction(self):
        from harness import Harness
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            atomic(root/'goal.txt','Complete MiniLedger and verify the requirements.');atomic(root/'memory.txt','Offline.\n')
            State().save(root/'state.log')
            endpoint.first_plan={'operations':[
                dict(op='ADD',parent_id=0,role='TEST',summary='Audit'),
                dict(op='ADD',parent_id=0,role='TEST',summary='ledger.py acceptance'),
                dict(op='ADD',parent_id=2,role='TEST',summary='Write unit tests',needs=[1,2])],
                'terminal':{'op':'FINISH'}}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            h=Harness(root,q);h.plan()
            self.assertEqual(len(endpoint.requests),2);self.assertEqual(q.counter,2)
            correction=str(endpoint.requests[1]['messages'])
            self.assertIn('2 -> 3 -> 2',correction);self.assertIn('No introduction',correction)
            self.assertIn('ADD | 0 | TEST | | Check existing behavior',correction)
            self.assertEqual(h.state.next_id,3);self.assertEqual([t.summary for t in h.state.tasks.values()],['Build','Verify'])
            self.assertEqual(h.state.actions,0);self.assertTrue(h.state.closed)

    def test_observed_initial_self_reference_reaches_fresh_fallback_without_partial_todos(self):
        from harness import Harness
        corrected=('ADD | 0 | ALGORITHM | | Implement library\nADD | 0 | ALGORITHM | | Implement CLI | 1\n'
            'ADD | 0 | TEST | | Independently check library | 1\nADD | 0 | TEST | | Independently check CLI | 2\n'
            'ADD | 0 | TEST | | Release check | 1,2,3,4\nFINISH')
        preface='State confirmed: only `ledger.py` and `miniledger.py` exist, no tests yet, next ID 1. Issuing the planned batch as designed.'
        oversized='Verify then repair ledger.py: strict YYYY-MM-DD parse_date; read_rows exact header validation, integer-only amounts, no empty/TOTAL category, reject bad CSV quoting; summarize counts negative amounts with inclusive date bounds and rejects start>end; keep write_report exact output; preserve public API'
        malformed=preface+'\nADD | 0 | ALGORITHM | ledger.py | '+oversized+'\nFINISH'
        for valid in (False,True):
            with self.subTest(valid=valid),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                atomic(root/'goal.txt','Implement and independently verify a library and CLI.');atomic(root/'memory.txt','Offline.')
                State().save(root/'state.log');endpoint.first_plan=INITIAL_SELF_NEEDS;endpoint.text_plan=corrected if valid else malformed
                endpoint.repeat_plan=not valid
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root);h=Harness(root,q)
                if valid:h.plan()
                else:self.assertEqual(h.run(),2)
                self.assertEqual(q.counter,2 if valid else 3);self.assertEqual(len(endpoint.requests),q.counter)
                feedback=str(endpoint.requests[1]['messages'])
                self.assertIn('next ID 1',feedback);self.assertIn('Task 1 needs itself',feedback)
                self.assertIn('No introduction, explanation, Markdown or JSON',feedback)
                self.assertIn('needs may reference tasks added later in this batch',feedback)
                self.assertFalse(any(m['role']=='tool' for m in endpoint.requests[1]['messages']))
                self.assertEqual(h.state.actions,0);self.assertFalse(h.state.complete());self.assertFalse(list(workspace.iterdir()))
                if valid:
                    self.assertEqual(h.state.next_id,6);self.assertTrue(h.state.closed)
                    self.assertEqual([t.needs for t in h.state.tasks.values()],[[],[1],[1],[2],[1,2,3,4]])
                    self.assertTrue(all(t.status=='PENDING' and t.attempts==0 for t in h.state.tasks.values()))
                else:
                    self.assertEqual(h.state.next_id,1);self.assertFalse(h.state.tasks)
                    self.assertIn('text: Invalid plan line: State confirmed:',h.state.reason)
                    self.assertFalse(State.load(h.path).tasks)
                    self.assertEqual(len(oversized),301)
                    with self.assertRaisesRegex(Refused,'got 301'):
                        apply_plan(State(),parse_plan(malformed.split('\n',1)[1]))

    def test_native_correction_after_two_rejections_uses_fresh_cli_schema_and_both_errors(self):
        from harness import Harness
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            atomic(root/'goal.txt','Implement and independently verify a library.');atomic(root/'memory.txt','Offline.')
            State().save(root/'state.log');endpoint.first_plan=INITIAL_SELF_NEEDS
            endpoint.text_plan='State confirmed: ready.\nADD | 0 | TEST | | Check\nFINISH'
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root);h=Harness(root,q);h.plan()
            self.assertEqual(q.counter,3);self.assertEqual(len(endpoint.requests),3);self.assertEqual(h.state.replans,1)
            self.assertEqual(h.state.next_id,3);self.assertEqual(h.state.actions,0)
            last=endpoint.requests[-1];feedback=str(last['messages'])
            self.assertIn('Task 1 needs itself',feedback);self.assertIn('Invalid plan line: State confirmed:',feedback)
            self.assertFalse(any(m['role']=='tool' for m in last['messages']))
            schema=next(t['function']['parameters'] for t in last['tools'] if t['function']['name']=='structured_output')
            self.assertEqual(schema['properties']['terminal'],PLAN_SCHEMA['properties']['terminal'])
            self.assertEqual(schema['properties']['operations']['maxItems'],64)
            self.assertEqual(schema['properties']['operations']['items']['oneOf'][0]['properties'],
                             PLAN_SCHEMA['properties']['operations']['items']['oneOf'][0]['properties'])
            self.assertNotIn('structured_output',str(endpoint.requests[1].get('tools',[])))
            self.assertNotIn('grammar',last)  # Native XML tool grammar is derived by llama.cpp, not the HTTP client.
            self.assertEqual([t.summary for t in h.state.tasks.values()],['Build','Test'])
            self.assertTrue(all(t.status=='PENDING' and t.attempts==0 for t in h.state.tasks.values()))

    def test_native_mixed_blocked_plan_requires_fresh_correction_without_partial_todos(self):
        from harness import Harness
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);(root/'workspace').mkdir()
            atomic(root/'goal.txt','Complete MiniLedger and check it.');atomic(root/'memory.txt','Offline.\n')
            State().save(root/'state.log')
            endpoint.first_plan={'operations':[dict(op='ADD',parent_id=0,role='TEST',summary=f'Check scope {i}') for i in range(1,8)],
                'terminal':{'op':'BLOCKED','reason':'Drafted plan contained non-ASCII characters and truncated summaries; resubmitting next round.'}}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            h=Harness(root,q);h.plan()
            self.assertEqual(len(endpoint.requests),2);self.assertEqual(q.counter,2)
            self.assertIn('BLOCKED requires empty operations',str(endpoint.requests[1]['messages']))
            self.assertFalse(any(m['role']=='tool' for m in endpoint.requests[1]['messages']))
            self.assertEqual([t.summary for t in h.state.tasks.values()],['Build','Verify'])
            self.assertEqual(h.state.next_id,3);self.assertEqual(h.state.actions,0)
            self.assertEqual(h.state.eligible().id,1);self.assertEqual(State.load(h.path).phase,'ready')

    def test_native_compaction_runs_before_reserved_output_headroom_is_exhausted(self):
        # Actual pinned CLI with controlled token usage; each variant changes one setting.
        for variant in ('current','old-hard-limit','restore-five'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                endpoint.write_path=str(workspace/'probe.txt');endpoint.write_content='retained file content\n'*200
                endpoint.usage_prompt_tokens=20000;endpoint.compact_input_tokens=4000;endpoint.compact_response=True
                def settings_write(path,text):
                    if path.name=='settings.json':
                        settings=json.loads(text)
                        if variant=='old-hard-limit':settings['context']['autoCompactThreshold']=9984/32768
                        if variant=='restore-five':settings['model']['chatCompression']['maxRecentFilesToRetain']=5
                        text=json.dumps(settings)
                    atomic(path,text)
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                with patch('harness.atomic',side_effect=settings_write):
                    if variant=='old-hard-limit':
                        with self.assertRaises(AgentFailure) as error:q.call('ALGORITHM','Write probe.txt, then report DONE.',workspace,task_id=1)
                        self.assertEqual(error.exception.kind,'context',str(error.exception)+'; captured output: '+repr(error.exception.report))
                        self.assertIn('hard limit: 12984',str(error.exception))
                        self.assertIn('compression status: COMPRESSED',str(error.exception))
                    else:self.assertEqual('DONE',q.call('ALGORITHM','Write probe.txt, then report DONE.',workspace,task_id=1)['status'])
                self.assertGreaterEqual(endpoint.compactions,1)
                self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)
                for body in endpoint.requests:
                    self.assertEqual((body['thinking_budget_tokens'],body['max_tokens']),(8192,11264) if body.get('tools') else (0,3072))
                    self.assertEqual(body['model'],'qwen-backtrackify')
                    self.assertEqual(body.get('chat_template_kwargs',{}).get('enable_thinking') is False,not bool(body.get('tools')))
                if variant!='old-hard-limit':
                    native=[json.loads(line) for log in (root/'turns').glob('*/native/projects/*/chats/*.jsonl') for line in log.read_text(encoding='utf-8').splitlines()]
                    records=[row for row in native if row.get('subtype')=='chat_compression']
                    self.assertTrue(records)
                    self.assertEqual('Recently accessed file (full current content embedded)' in str(records),variant=='restore-five')
                    settings=json.loads(next((root/'turns').glob('*/settings.json')).read_text())
                    self.assertEqual(settings['context']['autoCompactThreshold'],18248/32768)

    def test_native_inflated_compaction_remains_a_context_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.write_path=str(workspace/'probe.txt')
            endpoint.usage_prompt_tokens=23000;endpoint.compact_input_tokens=1000;endpoint.compact_response=True
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            with self.assertRaises(AgentFailure) as error:q.call('ALGORITHM','Write probe.txt, then report DONE.',workspace,task_id=1)
            self.assertEqual(error.exception.kind,'context',str(error.exception)+'; captured output: '+repr(error.exception.report))
            self.assertIn('hard limit: 21248',str(error.exception))
            self.assertIn('COMPRESSION_FAILED_INFLATED_TOKEN_COUNT',str(error.exception))
            self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)

    def test_native_compaction_cannot_bypass_the_gate_on_its_next_request(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.write_path=str(workspace/'probe.txt')
            endpoint.usage_prompt_tokens=20000;endpoint.compact_input_tokens=4000;endpoint.compact_response=True
            endpoint.token_count_after_compaction=21966
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            with self.assertRaises(AgentFailure) as error:q.call('ALGORITHM','Write probe.txt, then report DONE.',workspace,task_id=1)
            self.assertEqual(error.exception.kind,'context',str(error.exception)+'; captured output: '+repr(error.exception.report))
            self.assertIn('21966 + generation 11264 + safety 256 > 32768',str(error.exception))
            self.assertEqual(endpoint.compactions,2)  # Proactive compaction plus one bounded native reactive rescue.
            self.assertEqual(len(endpoint.requests),3)  # Neither oversized continuation reached the backend.

    def test_native_planning_recovers_token_rich_tool_growth_through_native_compaction(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            atomic(workspace/'scope.txt','Keep the original goal, accepted child results and trailing-newline failures.\n')
            endpoint.first_read=str(workspace/'scope.txt');endpoint.context_by_history=True
            endpoint.token_count=7778;endpoint.usage_prompt_tokens=11799;endpoint.token_count_after_tool=22640
            endpoint.compact_response=True;endpoint.compact_input_tokens=4000;endpoint.token_count_after_compaction=7000
            endpoint.compact_summaries=['<state_snapshot><goal>Repair the original scope</goal><decisions>Preserve DONE children</decisions><work>Read scope.txt</work><errors>Trailing newline accepted</errors><next>Submit a valid repair plan</next></state_snapshot>']
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            value=q.call('PROMPT','Replan from the authoritative TODO and failure. Read scope.txt if needed.',workspace,True)
            self.assertTrue(apply_plan(State(),value['operations']).closed)
            self.assertEqual(endpoint.compactions,1);self.assertIsNone(q.calls[-1].get('summary_error'))
            self.assertEqual(len(endpoint.requests),3)  # Read, native summary, then native plan; no oversized inference.
            final=endpoint.requests[-1];self.assertIn('Preserve DONE children',str(final['messages']))
            self.assertIn('Trailing newline accepted',str(final['messages']))
            self.assertEqual((final['thinking_budget_tokens'],final['max_tokens']),(8192,11264))
            log=next((root/'turns').glob('*/requests.log')).read_text()
            self.assertIn('prompt 22640 + generation 11264 + safety 256 > 32768',log)
            self.assertIn('COMPRESSION_ACCEPTED',log)
            records=[json.loads(line) for path in (root/'turns').glob('*/native/projects/*/chats/*.jsonl') for line in path.read_text(encoding='utf-8').splitlines()]
            compact=next(row['systemPayload']['info'] for row in records if row.get('subtype')=='chat_compression')
            self.assertEqual(compact['originalTokenCount'],22640);self.assertFalse(compact['originalTokenCountIsEstimated'])

    def test_worker_calls_have_separate_native_sessions_and_no_resumed_tool_history(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            for id in (1,2):
                endpoint.write_path=str(workspace/f'check{id}.txt')
                endpoint.write_content=f'TOOL_RESULT_FROM_INVOCATION_{id}'
                endpoint.worker_report=dict(task_id=id,status='DONE',summary=f'Completed check {id}')
                self.assertEqual(q.call('TEST',f'INVOCATION_{id}_ONLY: Write check{id}.txt, then give the report.',workspace,task_id=id)['task_id'],id)
            self.assertEqual(len(set(q.session_ids)),2)
            self.assertEqual(len(list((root/'turns').glob('*/settings.json'))),2)
            initial=[body for body in endpoint.requests if not any(m['role']=='tool' for m in body['messages'])]
            self.assertEqual(len(initial),2)
            self.assertNotIn('INVOCATION_1_ONLY',str(initial[1]['messages']))
            self.assertNotIn('TOOL_RESULT_FROM_INVOCATION_1',str(initial[1]['messages']))
            self.assertTrue((workspace/'check1.txt').exists());self.assertTrue((workspace/'check2.txt').exists())

    def test_native_schema_rejects_add_id_then_accepts_harness_assigned_ids(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            operations=parse_plan('ADD | 0 | ALGORITHM | calc.py | Build\nADD | 0 | TEST | tests/test_calc.py | Test\nFINISH')
            operations[0]['id']=1  # Observed native response in 20261024-140849.
            endpoint.first_plan={'operations':operations[:-1], 'terminal':operations[-1]}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            response=q.call('PROMPT','Submit only valid plan operations; ADD has no id.',workspace,True)
            self.assertGreaterEqual(len(endpoint.requests),2)
            self.assertNotIn('id',response['operations'][0])
            self.assertEqual(apply_plan(State(),response['operations']).next_id,3)
            self.assertIn('id',str([m for m in endpoint.requests[1]['messages'] if m['role']=='tool']))

    def test_native_intermediate_length_can_recover_but_final_length_cannot_pass(self):
        for limited in ({1},{2}):
            with self.subTest(limited=limited),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                endpoint.write_path=str(workspace/'probe.txt');endpoint.length_requests=limited
                endpoint.repeat_write_until_file=True  # Qwen rejects the length-truncated call, then retries the tool.
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                if limited=={1}:
                    self.assertEqual('DONE',q.call('TEST','Write probe.txt, then report DONE.',workspace,task_id=1)['status'])
                else:
                    with self.assertRaises(AgentFailure) as error:q.call('TEST','Write probe.txt, then report DONE.',workspace,task_id=1)
                    self.assertEqual(error.exception.kind,'limit')
                self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)
                self.assertTrue(q.calls[next(iter(limited))-1]['limited'])
                self.assertIn('LENGTH | request',next((root/'turns').glob('*/requests.log')).read_text())

    def test_default_framing_allows_ordinary_numbers_and_still_detects_real_thought_loops(self):
        for real_loop in (False,True):
            with self.subTest(real_loop=real_loop),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                endpoint.write_path=str(workspace/'probe.txt')
                endpoint.reasoning_deltas=(['This ','identical ','reasoning ','makes ','no ','progress. ']*100 if real_loop else
                    ['A valid Python integer literal is int("1_','0','0','0','"). I will write the requested file.'])
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                if real_loop:
                    with self.assertRaises(AgentFailure) as error:
                        q.call('ALGORITHM','Write probe.txt, then DONE.',workspace,trace=True,seed=137,task_id=1)
                    self.assertEqual(error.exception.kind,'loop')
                    self.assertFalse((workspace/'probe.txt').exists())
                else:
                    self.assertEqual('DONE',q.call('ALGORITHM','Write probe.txt, then DONE.',workspace,trace=True,seed=137,task_id=1)['status'])
                    self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)
                    turn=next((root/'turns').iterdir())
                    captured=json.loads((turn/'wire-1.request.json').read_text())
                    self.assertEqual(captured['seed'],137)
                    self.assertEqual(captured['thinking_budget_tokens'],8192)
                    self.assertEqual(captured['max_tokens'],11264)
                    self.assertTrue((turn/'wire-1.template.txt').exists())
                    settings=json.loads((turn/'settings.json').read_text())
                    self.assertIs(settings['model']['skipLoopDetection'],False)

    def test_native_repeated_token_deltas_false_positive_depends_on_stream_segmentation(self):
        prefix='A valid Python integer literal is int("1_'
        suffix='"). This is one ordinary example, not repeated reasoning. I will write the requested file now.'
        for split in (True,False):
            with self.subTest(split=split),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                endpoint.write_path=str(workspace/'probe.txt')
                endpoint.reasoning_deltas=[prefix,'0','0','0',suffix] if split else [prefix+'000'+suffix]
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
                if split:
                    with self.assertRaises(AgentFailure) as error:q.call('ALGORITHM','Write probe.txt, then DONE.',workspace,reasoning_chunk_chars=0,task_id=1)
                    self.assertEqual(error.exception.kind,'loop')
                    self.assertIn('repetitive_thoughts',str(error.exception))
                    self.assertFalse((workspace/'probe.txt').exists())
                else:
                    self.assertEqual('DONE',q.call('ALGORITHM','Write probe.txt, then DONE.',workspace,reasoning_chunk_chars=0,task_id=1)['status'])
                    self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)

    def test_default_framing_supports_all_three_native_agent_roles(self):
        for profile,context in [('LOW',8192),('MID',32768),('HIGH',65536),('EXTRA HIGH',32768)]:
            for role in ('PROMPT','ALGORITHM','TEST'):
                with self.subTest(profile=profile,role=role),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(role=='PROMPT') as endpoint:
                    root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                    endpoint.write_path=str(workspace/'probe.txt')
                    endpoint.reasoning_deltas=['One number 1_','0','0','0','; now use the tool.']
                    override=2345 if (profile,role)==('MID','TEST') else None
                    q=Qwen(CLI,profile,context,f'http://127.0.0.1:{endpoint.http.server_port}',root,agent_seconds=override)
                    with patch('harness.execute',side_effect=lambda cmd,cwd,timeout,*args:execute(cmd,cwd,min(timeout,120),*args)) as launched:
                        result=q.call(role,'Complete the assigned work with the supplied tool.',workspace,structured=role=='PROMPT',task_id=1)
                    command,_,timeout,*_=launched.call_args.args
                    wall=override or PROFILES[profile][2]
                    self.assertEqual(command[command.index('--max-wall-time')+1],f'{wall}s')
                    self.assertEqual(timeout,wall+30)
                    settings=json.loads(next((root/'turns').glob('*/settings.json')).read_text())
                    self.assertEqual(settings['model']['maxWallTimeSeconds'],wall)
                    self.assertEqual(settings['modelProviders']['openai'][0]['generationConfig']['timeout'],(wall+30)*1000)
                    if role=='PROMPT': self.assertEqual(result['operations'][-1]['op'],'FINISH')
                    else:
                        self.assertEqual(result['status'],'DONE')
                        self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)
                    for body in endpoint.requests:
                        self.assertEqual((body['thinking_budget_tokens'],body['max_tokens']),generation(profile,'ALGORITHM',context))
                        self.assertNotIn('reasoning_effort',body)
                        self.assertNotIn('reasoning_effort',body.get('chat_template_kwargs',{}))

    def test_context_refused_after_tool_write_cannot_be_native_success(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.write_path=str(workspace/'probe.txt')
            endpoint.token_count_after_tool=21966  # Exact refused prompt size in framing-64 / seed 137.
            endpoint.compact_response=True;endpoint.compact_input_tokens=4000
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            with self.assertRaises(AgentFailure) as error:
                q.call('ALGORITHM','Write probe.txt, then report DONE.',workspace,task_id=1)
            self.assertEqual(error.exception.kind,'context',str(error.exception)+'; captured output: '+repr(error.exception.report))
            self.assertIn('21966 + generation 11264 + safety 256 > 32768',str(error.exception))
            self.assertEqual((workspace/'probe.txt').read_text(),endpoint.write_content)
            self.assertEqual(endpoint.compactions,1)
            self.assertEqual(len(endpoint.requests),2)  # Tool and bounded rescue only; refused continuation never reached the model.

    def test_generic_test_context_allows_native_file_tools_and_final_report(self):
        from harness import Task, Harness
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            atomic(root/'goal.txt','Verify a C++ library.');atomic(root/'memory.txt','Offline only.\n')
            task=Task(1,0,'TEST','checks/spec.cpp','Check boundary behavior')
            State(tasks={1:task},next_id=2).save(root/'state.log')
            prompt=Harness(root,None).context(task,workspace)
            endpoint.write_path=str(workspace/task.path);endpoint.write_content='// independent verification artifact\n'
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            value=q.call('TEST',prompt,workspace,task_id=1)
            self.assertEqual(value['status'],'DONE');self.assertEqual((workspace/task.path).read_text(),endpoint.write_content)
            self.assertIn('Assigned task 1: checks/spec.cpp',prompt)
            self.assertNotIn('unittest',prompt)

    def test_reasoning_override_preserves_total_output_and_tool_continuation(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.write_path=str(workspace/'probe.txt')
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            report=q.call('ALGORITHM','Write probe.txt, then report DONE with one useful sentence.',workspace,thinking_override=32,task_id=1)
            self.assertGreaterEqual(len(endpoint.requests),2)
            for body in endpoint.requests:
                self.assertEqual(body['thinking_budget_tokens'],32)
                self.assertEqual(body['max_tokens'],11264)
            self.assertEqual(report['status'],'DONE')
            self.assertEqual((workspace/'probe.txt').read_text(),'native tools work\n')
            settings=json.loads(next((root/'turns').glob('*/settings.json')).read_text())
            self.assertIs(settings['model']['skipLoopDetection'],False)

    def test_actual_cli_rejects_oversized_summary_before_acceptance(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(True) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.first_plan={'operations':OVERSIZED_PLAN['operations'][:-1], 'terminal':OVERSIZED_PLAN['operations'][-1]}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            result=q.call('PROMPT','Submit a concise plan through structured_output.',workspace,True)
            self.assertGreaterEqual(len(endpoint.requests),2)
            self.assertEqual(result['operations'][1]['summary'],'Test')
            tool=next(t for t in endpoint.requests[0]['tools'] if t['function']['name']=='structured_output')
            limits=tool['function']['parameters']['properties']['operations']['items']['oneOf'][0]['properties']
            self.assertEqual(limits['summary']['maxLength'],280)
            feedback=str([m for m in endpoint.requests[1]['messages'] if m['role']=='tool'])
            self.assertIn('280',feedback)
            self.assertIn('summary',feedback)

    def test_actual_pinned_cli_structured_output_and_wire_budget(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(True) as endpoint:
            root=Path(tmp); workspace=root/'workspace';workspace.mkdir()
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            result=q.call('PROMPT','Submit the small plan through structured_output.',workspace,True)
            self.assertEqual(result['operations'][-1]['op'],'FINISH')
            self.assertTrue(endpoint.requests)
            self.assertEqual(endpoint.requests[0]['thinking_budget_tokens'],8192)
            self.assertEqual(endpoint.requests[0]['max_tokens'],11264)

    def test_actual_pinned_cli_file_tool_and_useful_final(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            endpoint.write_path = str(workspace/'probe.txt')
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            result=q.call('ALGORITHM','Write probe.txt with the supplied file tool, then say DONE.',workspace,task_id=1)
            self.assertEqual(result['status'],'DONE')
            self.assertEqual((workspace/'probe.txt').read_text(),'native tools work\n')
            self.assertGreaterEqual(len(endpoint.requests),2)
            names={tool['function']['name'] for tool in endpoint.requests[0].get('tools',[])}
            self.assertIn('write_file',names)
            self.assertIn('run_shell_command',names)
            self.assertLessEqual(names,{'read_file','glob','grep_search','edit','write_file','run_shell_command','structured_output'})
            self.assertEqual(endpoint.requests[0]['thinking_budget_tokens'],8192)

    def test_worker_uses_native_shell_then_reports_failed_without_harness_recheck(self):
        from harness import Harness, apply_plan
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            atomic(root/'goal.txt','Check current behavior.');atomic(root/'memory.txt','Offline.\n')
            apply_plan(State(),parse_plan('ADD | 0 | TEST | checks | Check behavior\nFINISH')).save(root/'state.log')
            endpoint.shell_command='echo delegated-check'
            endpoint.worker_report={'task_id':1,'status':'FAILED','summary':'Executed the delegated check','reason':'Observed defect in the project'}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            h=Harness(root,q)
            with patch('harness.execute',side_effect=lambda cmd,cwd,timeout,*args:execute(cmd,cwd,min(timeout,120),*args)) as launched:
                value=h.step(h.state.eligible())
            self.assertEqual(value['status'],'FAILED');self.assertEqual(h.state.tasks[1].status,'FAILED')
            self.assertEqual(launched.call_count,1)  # Only the delegated Qwen CLI, never a Python-side project command.
            self.assertIn('structured_output',{t['function']['name'] for t in endpoint.requests[0]['tools']})
            self.assertNotEqual(endpoint.requests[0].get('tool_choice'),'required')
            self.assertTrue(any('delegated-check' in str(m) for body in endpoint.requests[1:] for m in body['messages'] if m['role']=='tool'))

    def test_test_reports_after_real_checks_or_read_loop_without_fabricated_completion(self):
        from harness import Harness
        for outcome in ('DONE','FAILED','loop'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
                root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
                atomic(root/'goal.txt','Verify the delivered project and report the observed result.')
                atomic(root/'memory.txt','Offline; preserve source and tests.')
                atomic(workspace/'tests/test_feature.py','import unittest\nclass Checks(unittest.TestCase):\n    def test_contract(self): self.assertEqual(1, '+('2' if outcome=='FAILED' else '1')+')\n')
                for i in range(8):atomic(workspace/f'source{i}.py',f'VALUE = {i}\n')
                apply_plan(State(),parse_plan('ADD | 0 | TEST | | Verify delivered release\nFINISH')).save(root/'state.log')
                def tool(n,name,args):
                    return [dict(id=f'call_{n}',type='function',function=dict(name=name,arguments=json.dumps(args)))]
                endpoint.tool_rounds=[tool(0,'read_file',dict(file_path=str(workspace/'source0.py'))),
                    tool(1,'run_shell_command',dict(command='python -m unittest discover -s tests -q',description='Run the assigned checks'))]
                if outcome=='loop':
                    for i in range(1,8):
                        if i==4:endpoint.tool_rounds.append(tool(20,'glob',dict(pattern='source*.py')))
                        endpoint.tool_rounds.append(tool(i+1,'read_file',dict(file_path=str(workspace/f'source{i}.py'))))
                endpoint.worker_report=dict(task_id=1,status='FAILED' if outcome=='FAILED' else 'DONE',summary='Assigned checks completed',
                    **({'reason':'The contract assertion failed'} if outcome=='FAILED' else {}))
                q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root);h=Harness(root,q)
                value=h.step(h.state.eligible());loaded=State.load(h.path)
                self.assertEqual(loaded.tasks[1].attempts,1)
                evidence=str([m for body in endpoint.requests for m in body['messages'] if m['role']=='tool'])
                self.assertIn('Ran 1 test',evidence)
                settings=json.loads(next((root/'turns').glob('*/settings.json')).read_text())
                self.assertIs(settings['model']['skipLoopDetection'],False)
                self.assertIn('Verify the assigned workspace as delivered',str(endpoint.requests[0]['messages']))
                if outcome=='loop':
                    self.assertIsNone(value);self.assertEqual(loaded.tasks[1].kind,'loop')
                    self.assertIn('read_file_loop',loaded.tasks[1].reason)
                    self.assertNotEqual(loaded.tasks[1].status,'DONE');self.assertFalse(loaded.tasks[1].result)
                    self.assertIn('OK',evidence);self.assertFalse(endpoint.tool_rounds)
                    self.assertFalse(loaded.complete());self.assertEqual(len(endpoint.requests),10)
                else:
                    self.assertEqual(value['status'],outcome);self.assertEqual(loaded.tasks[1].status,outcome)
                    self.assertEqual(len(endpoint.requests),3)  # Read, check, terminal report; no extra exploration.

    def test_native_report_schema_rejects_bad_status_then_accepts_correction(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.write_path=str(workspace/'probe.txt')
            endpoint.first_report={'task_id':1,'status':'VERIFIED','summary':'Invalid old status'}
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            self.assertEqual(q.call('TEST','Complete task 1, then submit its structured report.',workspace,task_id=1)['status'],'DONE')
            self.assertGreaterEqual(len(endpoint.requests),3)
            schema=next(t for t in endpoint.requests[0]['tools'] if t['function']['name']=='structured_output')['function']['parameters']
            self.assertEqual(schema['required'],REPORT_SCHEMA['required'])
            branches=schema['properties']['report']['oneOf']
            self.assertEqual(branches[0]['properties']['status']['enum'],['DONE'])
            self.assertEqual(branches[1]['properties']['status']['enum'],['FAILED','BLOCKED'])
            self.assertIn('reason',branches[1]['required'])

    def test_native_plain_done_is_not_a_worker_report(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir();endpoint.write_path=str(workspace/'probe.txt')
            endpoint.plain_final=True
            q=Qwen(CLI,'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            with self.assertRaises(AgentFailure) as failure:q.call('TEST','Complete task 1 and report.',workspace,task_id=1)
            self.assertEqual(failure.exception.kind,'protocol')
            self.assertTrue((workspace/'probe.txt').exists())



class GateTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Pinned Windows grammar CPU check')
    def test_pinned_grammar_masks_invalid_compaction_tokens_without_model_inference(self):
        import ctypes as c,math,re
        asset=CLI.parents[4]
        config=(ROOT/'local.ps1').read_text(encoding='utf-8') if (ROOT/'local.ps1').exists() else ''
        match=re.search(r"(?m)^\$AssetRoot\s*=\s*'([^']+)'",config)
        if match:asset=Path(match[1])
        models=sorted((asset/'models').glob('*.gguf'));dll=asset/'runtime/llama/llama.dll'
        if not dll.exists() or not models:self.skipTest('Pinned llama DLL/vocabulary assets unavailable')
        # Pinned b10636 llama_model_params layout; vocab_only skips every weight tensor.
        class Params(c.Structure):
            _fields_=[('devices',c.c_void_p),('overrides',c.c_void_p),('gpu',c.c_int32),('split',c.c_int32),
                ('load',c.c_int32),('main_gpu',c.c_int32),('tensor_split',c.c_void_p),('progress',c.c_void_p),
                ('progress_data',c.c_void_p),('kv',c.c_void_p),('vocab_only',c.c_bool),('check',c.c_bool),
                ('extra',c.c_bool),('no_host',c.c_bool),('no_alloc',c.c_bool),('mtp',c.c_bool)]
        class Token(c.Structure):_fields_=[('id',c.c_int32),('logit',c.c_float),('p',c.c_float)]
        class Candidates(c.Structure):_fields_=[('data',c.POINTER(Token)),('size',c.c_size_t),('selected',c.c_int64),('sorted',c.c_bool)]
        with os.add_dll_directory(str(dll.parent)),os.add_dll_directory(str(asset/'runtime/cuda')):
            lib=c.CDLL(str(dll))
            signatures={'llama_model_default_params':([],Params),'llama_model_load_from_file':([c.c_char_p,Params],c.c_void_p),
                'llama_model_get_vocab':([c.c_void_p],c.c_void_p),'llama_model_free':([c.c_void_p],None),
                'llama_sampler_init_grammar':([c.c_void_p,c.c_char_p,c.c_char_p],c.c_void_p),
                'llama_sampler_apply':([c.c_void_p,c.POINTER(Candidates)],None),'llama_sampler_accept':([c.c_void_p,c.c_int32],None),
                'llama_sampler_free':([c.c_void_p],None),'llama_vocab_eos':([c.c_void_p],c.c_int32),
                'llama_tokenize':([c.c_void_p,c.c_char_p,c.c_int32,c.POINTER(c.c_int32),c.c_int32,c.c_bool,c.c_bool],c.c_int32)}
            for name,(args,result) in signatures.items():getattr(lib,name).argtypes=args;getattr(lib,name).restype=result
            params=lib.llama_model_default_params();params.vocab_only=True;params.gpu=0;params.mtp=False
            devices=(c.c_void_p*1)();params.devices=c.cast(devices,c.c_void_p)
            model=lib.llama_model_load_from_file(str(models[0]).encode(),params);self.assertTrue(model)
            try:
                vocab=lib.llama_model_get_vocab(model)
                def accepted(text):
                    raw=text.encode();tokens=(c.c_int32*(len(raw)+8))()
                    size=lib.llama_tokenize(vocab,raw,len(raw),tokens,len(tokens),False,False);self.assertGreaterEqual(size,0)
                    sampler=lib.llama_sampler_init_grammar(vocab,COMPRESSION_GRAMMAR.encode(),b'root');self.assertTrue(sampler)
                    try:
                        for token in list(tokens[:size])+[lib.llama_vocab_eos(vocab)]:
                            data=(Token*1)(Token(token,0,0));candidates=Candidates(data,1,-1,False)
                            lib.llama_sampler_apply(sampler,c.byref(candidates))
                            if not math.isfinite(data[0].logit):return False
                            lib.llama_sampler_accept(sampler,token)
                        return True
                    finally:lib.llama_sampler_free(sampler)
                valid='<state_snapshot><goal>Keep UTF-8 \u91d1 &amp; constraints</goal><decisions>Use strict CSV</decisions><work>Children complete</work><errors>One known gap</errors><next>Fix and report</next></state_snapshot>'
                self.assertTrue(accepted(valid))
                for invalid in ('<tool_call><function=write_file></function></tool_call>',
                                valid.replace('</work>','</goal>'),valid[:-17],valid.replace('&amp;','&bad;')):
                    with self.subTest(response=invalid[:30]):self.assertFalse(accepted(invalid))
            finally:lib.llama_model_free(model)

    def test_compression_prompt_budget_and_complete_snapshot_validation_are_scoped(self):
        import copy,re
        body=dict(max_tokens=11264,thinking_budget_tokens=8192,messages=[
            dict(role='system',content=COMPRESSION_PREFIX+' Original verbose native prompt'),
            dict(role='assistant',content='Changed parser.py; failing boundary remains.'),
            dict(role='user',content=[dict(type='text',text=COMPRESSION_DIRECTIVE)])])
        before=copy.deepcopy(body)
        self.assertTrue(compression_request(body,11264))
        self.assertEqual(body['grammar'],COMPRESSION_GRAMMAR)
        self.assertEqual(body['max_tokens'],3072);self.assertEqual(body['thinking_budget_tokens'],0)
        self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(body['messages'][1],before['messages'][1])
        self.assertNotIn('<analysis>',str(body));self.assertIn('constraints',body['messages'][0]['content'])
        low=copy.deepcopy(before);self.assertTrue(compression_request(low,4608));self.assertEqual(low['max_tokens'],1024)
        worker=copy.deepcopy(before);worker['messages'][0]['content']='Regular worker; do not disable reasoning'
        original=copy.deepcopy(worker);self.assertFalse(compression_request(worker,11264));self.assertEqual(worker,original)
        # Native stripAnalysisBlock truncates a quoted unmatched tag in an otherwise complete snapshot.
        quoted='<state_snapshot><goal>Preserve files</goal><next>Quoted: '+COMPRESSION_DIRECTIVE+'</next></state_snapshot>'
        stripped=re.sub(r'<analysis>[\s\S]*$','',quoted)
        self.assertNotIn('</state_snapshot>',stripped)
        def response(text,reason='stop'):
            return json.dumps({'choices':[{'message':{'content':text},'finish_reason':reason}]}).encode()
        good='<state_snapshot><goal>Fix parser.py; preserve UTF-8</goal><work>Read input; defect reproduced</work><next>Repair boundary, then report</next></state_snapshot>'
        self.assertEqual(compression_summary(response(good),False),good)
        for text,reason in [(quoted,'stop'),(stripped,'stop'),(good,'length'),('<state_snapshot/>','stop'),('prose','stop')]:
            with self.subTest(text=text[:30],reason=reason),self.assertRaises(Refused):compression_summary(response(text,reason),False)

    def test_rejected_compression_is_not_forwarded_and_next_native_query_can_recover(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            endpoint.compact_response=True
            endpoint.compact_summaries=['<state_snapshot><goal>Unclosed',
                '<state_snapshot><goal>Preserve existing files and constraints</goal><next>Finish and report</next></state_snapshot>']
            with InferenceGate(f'http://127.0.0.1:{endpoint.http.server_port}',32768,8192,11264,Path(tmp)/'requests.log') as gate:
                body=dict(max_tokens=11264,thinking_budget_tokens=8192,stream=True,messages=[
                    dict(role='system',content=COMPRESSION_PREFIX),dict(role='user',content=COMPRESSION_DIRECTIVE)])
                def query():
                    return urllib.request.urlopen(urllib.request.Request(gate.url+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'}),timeout=10)
                with self.assertRaises(urllib.error.HTTPError) as error:query()
                error.exception.close()
                self.assertIn('summary_error',gate.calls[0])
                with query() as response:accepted=response.read()
                self.assertNotIn(b'Unclosed',accepted);self.assertIn(b'Preserve existing files',accepted)
                self.assertIsNone(gate.failure);self.assertTrue(gate.calls[-1]['complete'])
                self.assertEqual([row['max_tokens'] for row in endpoint.requests],[3072,3072])

    def test_upstream_read_error_flushes_thinking_tail_without_fake_completion(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            log=Path(tmp)/'requests.log'
            backend=f'http://127.0.0.1:{endpoint.http.server_port}'
            chunk={'id':'partial','choices':[{'index':0,'delta':{'reasoning_content':'retained short tail'},'finish_reason':None}]}
            class BrokenResponse:
                status=200;headers={'Content-Type':'text/event-stream'};reads=0
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read1(self,size):
                    self.reads+=1
                    if self.reads==1:return ('data: '+json.dumps(chunk)+'\n\n').encode()
                    raise http.client.IncompleteRead(b'')
            original=urllib.request.urlopen
            def broken(req,*args,**kwargs):
                if isinstance(req,urllib.request.Request) and req.full_url==backend+'/v1/chat/completions':return BrokenResponse()
                return original(req,*args,**kwargs)
            with InferenceGate(backend,8192,2048,3584,log,trace=True) as gate,patch('harness.urllib.request.urlopen',side_effect=broken):
                req=urllib.request.Request(gate.url+'/v1/chat/completions',json.dumps(dict(thinking_budget_tokens=2048,max_tokens=3584,messages=[],stream=True)).encode(),{'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response:received=response.read()
            self.assertIn(b'retained short tail',received)
            self.assertNotIn(b'[DONE]',received)
            self.assertFalse(gate.calls[0]['complete'])
            self.assertEqual(gate.failure[0],'backend')
            self.assertEqual((Path(tmp)/'wire-1.client.sse').read_bytes(),received)

    def test_native_finish_reason_is_terminal_without_sse_sentinel(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            endpoint.send_sentinel=False
            with InferenceGate(f'http://127.0.0.1:{endpoint.http.server_port}',8192,2048,3584,Path(tmp)/'requests.log') as gate:
                body={'thinking_budget_tokens':2048,'max_tokens':3584,'messages':[],'stream':True}
                req=urllib.request.Request(gate.url+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response: response.read()
            self.assertTrue(gate.calls[0]['complete'])

    def test_real_error_survives_disconnect_while_sending_error_response(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp:
            log=Path(tmp)/'requests.log'
            with InferenceGate('http://127.0.0.1:1',8192,2048,3584,log) as gate:
                original=socketserver._SocketWriter.write
                def disconnected(writer,data):
                    if writer._sock.getsockname()[1]==gate.http.server_port:
                        raise ConnectionAbortedError('client left before error response')
                    return original(writer,data)
                body={'thinking_budget_tokens':-1,'max_tokens':3584,'messages':[]}
                with patch.object(gate.http,'handle_error') as unexpected, patch('socketserver._SocketWriter.write',new=disconnected):
                    req=urllib.request.Request(gate.url+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
                    with self.assertRaises(http.client.RemoteDisconnected): urllib.request.urlopen(req,timeout=5)
                unexpected.assert_not_called()
            report=log.read_text(encoding='utf-8')
            self.assertIn('REFUSED | Native thinking/output budget differs',report)
            self.assertIn('CLIENT_DISCONNECTED',report)

    def test_mismatched_native_budget_refused(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            backend=f'http://127.0.0.1:{endpoint.http.server_port}'
            with InferenceGate(backend,8192,2048,3584,Path(tmp)/'requests.log') as gate:
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    request(gate.url+'/v1/chat/completions',{'thinking_budget_tokens':-1,'max_tokens':3584,'messages':[]})
                failure.exception.close()
            self.assertFalse(endpoint.requests)

    def test_native_wall_clock_timeout_is_classified(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp:
            import subprocess
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            q=Qwen('unused.js','MID',32768,'http://127.0.0.1:1',root)
            with patch('harness.execute',side_effect=subprocess.TimeoutExpired('qwen',930)), self.assertRaises(AgentFailure) as error:
                q.call('TEST','Write tests.',workspace,task_id=1)
            self.assertEqual(error.exception.kind,'timeout')

    def test_client_disconnect_is_quiet_and_logged(self):
        original=socketserver._SocketWriter.write
        for error in (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
                backend=f'http://127.0.0.1:{endpoint.http.server_port}'
                log=Path(tmp)/'requests.log'
                with InferenceGate(backend,8192,2048,3584,log) as gate:
                    def disconnected(writer,data):
                        if writer._sock.getsockname()[1]==gate.http.server_port and not data.startswith(b'HTTP/'):
                            raise error('client left')
                        return original(writer,data)
                    body={'thinking_budget_tokens':2048,'max_tokens':3584,'messages':[],'stream':True}
                    with patch.object(gate.http,'handle_error') as unexpected, patch('socketserver._SocketWriter.write',new=disconnected):
                        req=urllib.request.Request(gate.url+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
                        with urllib.request.urlopen(req,timeout=5) as response: response.read()
                    unexpected.assert_not_called()
                self.assertIn('CLIENT_DISCONNECTED',log.read_text(encoding='utf-8'))

    def test_interrupted_backend_stream_is_recorded(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            endpoint.finish_stream=False
            log=Path(tmp)/'requests.log'
            with InferenceGate(f'http://127.0.0.1:{endpoint.http.server_port}',8192,2048,3584,log) as gate:
                body={'thinking_budget_tokens':2048,'max_tokens':3584,'messages':[],'stream':True}
                req=urllib.request.Request(gate.url+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response: response.read()
            self.assertFalse(gate.calls[0]['complete'])
            self.assertIn('STREAM_INCOMPLETE',log.read_text(encoding='utf-8'))

    def test_native_success_cannot_hide_an_interrupted_backend_stream(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            root=Path(tmp); workspace=root/'workspace';workspace.mkdir()
            endpoint.finish_stream=False
            def native_stub(command,cwd,timeout,output,env):
                settings=json.loads((Path(env['QWEN_HOME'])/'settings.json').read_text())
                provider=settings['modelProviders']['openai'][0]
                body=dict(provider['generationConfig']['extra_body'],messages=[],stream=True)
                req=urllib.request.Request(provider['baseUrl']+'/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response: response.read()
                output.write_text(json.dumps({'type':'result','is_error':False,'result':'DONE'}),encoding='utf-8')
                return 0
            q=Qwen('unused.js','MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',root)
            with patch('harness.execute',side_effect=native_stub), self.assertRaises(AgentFailure) as error:
                q.call('TEST','Write tests.',workspace,task_id=1)
            self.assertEqual(error.exception.kind,'stream')

    def test_insufficient_prompt_headroom_refused_before_model_send(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp, Endpoint(False) as endpoint:
            endpoint.token_count=7000
            backend=f'http://127.0.0.1:{endpoint.http.server_port}'
            with InferenceGate(backend,8192,2048,3584,Path(tmp)/'requests.log') as gate:
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    request(gate.url+'/v1/chat/completions',{'thinking_budget_tokens':2048,'max_tokens':3584,'messages':[]})
                self.assertIn('Context headroom exhausted',failure.exception.read().decode())
                failure.exception.close()
            self.assertFalse(endpoint.requests)

    def test_exact_headroom_reserves_maximum_output_without_reducing_thinking(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp,Endpoint(False) as endpoint:
            endpoint.final_only=True;endpoint.token_count=21248
            body={'thinking_budget_tokens':8192,'max_tokens':11264,'messages':[{'role':'user','content':'Report'}]}
            with InferenceGate(f'http://127.0.0.1:{endpoint.http.server_port}',32768,8192,11264,Path(tmp)/'requests.log') as gate:
                result=request(gate.url+'/v1/chat/completions',body)
                self.assertEqual(result['usage']['completion_tokens'],50)
                self.assertEqual(endpoint.requests[0]['max_tokens'],11264)
                endpoint.token_count+=1
                with self.assertRaises(urllib.error.HTTPError) as failure:request(gate.url+'/v1/chat/completions',body)
                error=json.load(failure.exception)['error'];failure.exception.close()
                self.assertEqual(error['code'],'context_length_exceeded')
                self.assertIn('21249 + generation 11264',error['message'])
                self.assertEqual(len(endpoint.requests),1)  # Expected short output cannot authorize an oversized request.


class OutputTests(unittest.TestCase):
    def test_native_budget_stderr_without_result_is_not_a_parser_failure(self):
        stream=json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Still working'}]}})+'\n'
        cases=[('Run aborted: wall-clock budget of 1800s exceeded (--max-wall-time).',55,'timeout'),
               ('Run aborted: tool-call budget of 48 exceeded (--max-tool-calls).',55,'limit'),
               ('Reached max session turns for this session. Increase the number of turns by specifying maxSessionTurns in settings.json.',53,'limit'),
               ('',53,'limit')]
        for stderr,code,kind in cases:
            with self.subTest(kind=kind,code=code),self.assertRaises(AgentFailure) as failure:qwen_result(stream+stderr,code)
            self.assertEqual(failure.exception.kind,kind)
            self.assertIn('budget',str(failure.exception))
        with self.assertRaises(AgentFailure) as failure:qwen_result(stream,1)
        self.assertEqual(failure.exception.kind,'stream')

    def test_reasoning_probe_requires_structured_final_report(self):
        from tests.e2e import check_probe_report
        report={'task_id':1,'status':'DONE','summary':'Created probe.txt through the native file tool after reasoning cutoff'}
        value={'type':'result','is_error':False,'structured_result':{'report':report}}
        parsed=qwen_result(json.dumps(value));check_probe_report(parsed['structured_result']['report'])

    def test_reasoning_probe_rejects_missing_or_useless_final_answer(self):
        from tests.e2e import check_probe_report
        for report in ('','DONE','Created a file but did not provide a completion marker.',
                       'BLOCKED: could not write the requested file.',
                       {'task_id':1,'status':'DONE','summary':'Useful-looking report with an unknown reference','related_tasks':[999]}):
            with self.subTest(report=report),self.assertRaises(Refused):check_probe_report(report)

    def test_invalid_structured_result_is_preserved_and_rejected_atomically(self):
        self.assertEqual(len(OVERSIZED_PLAN['operations'][1]['summary']),281)
        messages=[{'type':'assistant','message':{'content':[{'type':'tool_use','name':'structured_output','input':OVERSIZED_PLAN}]}},
                  {'type':'result','is_error':False,'result':json.dumps(OVERSIZED_PLAN),'structured_result':OVERSIZED_PLAN}]
        for raw in (json.dumps(messages),'\n'.join(map(json.dumps,messages))):
            result=qwen_result(raw)
            self.assertEqual(result['structured_result'],OVERSIZED_PLAN)
            self.assertEqual(json.loads(result['result']),OVERSIZED_PLAN)
            state=State();before=state.render()
            with self.assertRaisesRegex(Refused,'summary.*280.*281'):
                apply_plan(state,result['structured_result']['operations'])
            self.assertEqual(state.render(),before)

    def test_native_field_limits_match_harness_boundaries(self):
        branches=PLAN_SCHEMA['properties']['operations']['items']['oneOf']
        fields=branches[0]['properties']
        self.assertNotIn('id',fields)
        self.assertEqual(set(branches[0]['required']),{'op','parent_id','role','summary'})
        self.assertEqual((fields['summary']['minLength'],fields['summary']['maxLength']),(1,280))
        self.assertEqual((fields['path']['minLength'],fields['path']['maxLength']),(1,240))
        ops=parse_plan('ADD | 0 | ALGORITHM | calc.py | Build\nADD | 0 | TEST | tests/test_calc.py | Test\nFINISH')
        ops[0]['summary']='x'*280
        ops[0]['path']='x'*237+'.py'
        self.assertEqual(apply_plan(State(),ops).next_id,3)
        for key,value in [('summary','x'*281),('summary',''),('path','x'*241),('path','')]:
            invalid=json.loads(json.dumps(ops));invalid[0][key]=value
            state=State();before=state.render()
            with self.subTest(key=key,length=len(value)),self.assertRaises(Refused):apply_plan(state,invalid)
            self.assertEqual(state.render(),before)
        for summary in ('\u00e9'*280,'x'*279+'\u574f'):
            ops[0]['summary']=summary
            accepted=apply_plan(State(),ops)
            with tempfile.TemporaryDirectory(dir=ROOT/'sessions') as tmp:
                path=Path(tmp)/'state.log';accepted.save(path)
                self.assertEqual(State.load(path).tasks[1].summary,summary)
            ops[0]['summary']=summary+'x'
            before=accepted.render()
            with self.assertRaisesRegex(Refused,'summary.*280'):apply_plan(accepted,ops)
            self.assertEqual(accepted.render(),before)

    def test_array_and_stream_json_preserve_text_without_defining_worker_completion(self):
        messages=[{'type':'assistant','message':{'content':[{'type':'thinking','thinking':'DONE is only a thought'},
                  {'type':'text','text':'DONE: tests written'}]}},
                  {'type':'result','is_error':False,'result':'DONE: tests written'}]
        for raw in ('diagnostic\n'+json.dumps(messages),'diagnostic\n[]\n'+'\n'.join(map(json.dumps,messages))):
            self.assertTrue(qwen_result(raw)['result'].startswith('DONE'))


    def test_tool_results_and_thoughts_are_not_completion(self):
        raw='\n'.join(map(json.dumps,[{'type':'assistant','message':{'content':[{'type':'thinking','thinking':'DONE'}]}},
             {'type':'user','message':{'content':[{'type':'tool_result','content':'DONE'}]}},
             {'type':'result','is_error':False}]))
        self.assertEqual(qwen_result(raw)['result'],'')

    def test_structured_contract_error_cannot_hide_a_context_api_error(self):
        native={'type':'result','is_error':True,'result':'[API Error: Context is too large after compression.]',
                'error':{'message':'Model produced plain text instead of calling the structured_output tool as required by --json-schema'}}
        with self.assertRaises(AgentFailure) as failure:qwen_result(json.dumps(native),1)
        self.assertEqual(failure.exception.kind,'context')

    def test_native_loop_is_distinct_from_timeout_exit_and_broken_stream(self):
        native={'type':'result','is_error':True,'error':{'message':'Loop detection halted the run (repetitive_thoughts).'}}
        with self.assertRaises(AgentFailure) as error:
            qwen_result(json.dumps(native),1)
        self.assertEqual(error.exception.kind,'loop')
        native['error']['message']='Execution failed'
        with self.assertRaises(AgentFailure) as error:
            qwen_result(json.dumps(native),1)
        self.assertEqual(error.exception.kind,'qwen')
        with self.assertRaises(AgentFailure) as error:
            qwen_result(json.dumps({'type':'result','is_error':False,'result':'[API Error: Context is too large to send safely after automatic compression.]'}))
        self.assertEqual(error.exception.kind,'context',str(error.exception)+'; captured output: '+repr(error.exception.report))
        with self.assertRaises(AgentFailure) as error:
            qwen_result(json.dumps(native),55,timed_out=True)
        self.assertEqual(error.exception.kind,'timeout')
        with self.assertRaises(AgentFailure) as error:
            qwen_result('{"type":"assistant","message":{"content":[]}}\n{"type":"result"',0)
        self.assertEqual(error.exception.kind,'stream')
        with self.assertRaises(AgentFailure) as error:
            qwen_result('not native JSON',0)
        self.assertEqual(error.exception.kind,'output')

if __name__=='__main__': unittest.main()
