import contextlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import backend
from harness import Refused, request


class FakeModel:
    def __init__(self,**config):self.config=config
    def __enter__(self):
        if self.config.get('slow'):time.sleep(2)
        model=self;model.failed=False;model.process=SimpleNamespace(poll=lambda:1 if model.failed else None)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                data=self.rfile.read(int(self.headers['Content-Length']))
                value=json.loads(data)
                if value.get('crash'):model.failed=True
                if value.get('entered'):Path(value['entered']).write_text('entered')
                if self.path not in {'/tokenize','/apply-template','/detokenize'}:time.sleep(value.get('delay',.1))
                self.send_response(200);self.end_headers();self.wfile.write(data)
        self.http=ThreadingHTTPServer(('127.0.0.1',self.config['port']),Handler)
        self.thread=threading.Thread(target=self.http.serve_forever);self.thread.start();return self
    def __exit__(self,*_):self.http.shutdown();self.http.server_close();self.thread.join()


def fixture_owner(config,root):
    backend.backend_lock=contextlib.nullcontext
    backend.owner(config,Path(root),FakeModel,idle_seconds=1)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='backend clients ');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        self.config=dict(exe='fake',model='fake',context=32768,batch=512,ubatch=256,port=port,thinking=8192,threads=16,spec_type='draft-mtp,ngram-mod',gpu='0')
        self.processes=[]
        self.addCleanup(self.cleanup)
    def cleanup(self):
        for p in self.processes:
            if p.poll() is None:p.kill()
            p.wait(timeout=5)
    def launch(self,command):
        code='from tests.test_backend import fixture_owner; import json,sys; fixture_owner(json.loads(sys.argv[1]),sys.argv[2])'
        with (self.root/'owner.log').open('ab') as log:
            p=subprocess.Popen([sys.executable,'-B','-c',code,json.dumps(self.config),str(self.root)],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        self.processes.append(p)
    def client(self,config=None):return backend.SharedBackend(config or self.config,self.root,self.launch)

    def test_cli_first_chat_second_and_reverse_share_one_independent_backend(self):
        for first,second in [('CLI','Chat'),('Chat','CLI')]:
            with self.subTest(first=first):
                a=self.client();a.__enter__();b=self.client();b.__enter__()
                self.assertEqual(a.url,b.url);self.assertEqual(request(a.url+'/echo',{'client':first}),{'client':first})
                a.__exit__();self.assertEqual(request(b.url+'/echo',{'client':second}),{'client':second})
                with self.client() as restarted:self.assertEqual(restarted.url,b.url)
                b.__exit__();self.processes[-1].wait(timeout=8)
    def test_concurrent_requests_are_serial_and_preserve_independent_payloads(self):
        with self.client() as a,self.client() as b:
            results=[];started=time.monotonic()
            threads=[threading.Thread(target=lambda c=c:results.append(request(c.url+'/echo',{'identity':id(c)}))) for c in (a,b)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=5)
            self.assertEqual(len(results),2);self.assertNotEqual(results[0],results[1])
            self.assertGreater(time.monotonic()-started,.19)
    def test_abrupt_client_exit_does_not_kill_other_clients_model(self):
        with self.client() as survivor:
            code='import urllib.request,time,sys; r=urllib.request.urlopen(sys.argv[1]+"/lease"); print("LEASE",flush=True); time.sleep(60)'
            child=subprocess.Popen([sys.executable,'-B','-c',code,survivor.url],stdout=subprocess.PIPE,text=True)
            self.processes.append(child);self.assertEqual(child.stdout.readline().strip(),'LEASE')
            child.kill();child.wait(timeout=5);child.stdout.close()
            self.assertEqual(request(survivor.url+'/echo',{'still':'alive'}),{'still':'alive'})
            self.assertIsNone(self.processes[0].poll())
    def test_mismatched_settings_are_refused_without_loading_another_model(self):
        with self.client():
            changed=dict(self.config,context=65536)
            with self.assertRaisesRegex(Refused,'different model/profile'):self.client(changed).__enter__()
            self.assertEqual(len(self.processes),1)
    def test_metadata_does_not_wait_for_another_clients_generation(self):
        with self.client() as first,self.client() as second:
            marker=self.root/'entered';result=[]
            worker=threading.Thread(target=lambda:result.append(request(first.url+'/echo',{'delay':1,'entered':str(marker)})))
            worker.start();deadline=time.monotonic()+5
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(marker.exists())
            self.assertEqual(request(second.url+'/tokenize',{'tokens':'independent'},timeout=.5),{'tokens':'independent'})
            self.assertTrue(worker.is_alive());worker.join(timeout=5);self.assertEqual(len(result),1)
    def test_owner_stops_after_last_lease_and_stale_descriptor_is_not_liveness(self):
        with self.client() as c:url=c.url
        self.processes[0].wait(timeout=8)
        with self.client() as c:self.assertNotEqual(c.url,url)
        self.assertEqual(len(self.processes),2)
    def test_model_exit_does_not_leave_a_false_ready_owner(self):
        with self.client() as client:
            request(client.url+'/echo',{'crash':True})
            self.processes[0].wait(timeout=8)
            self.assertIsNotNone(self.processes[0].poll())
    def test_slow_model_loading_does_not_exhaust_the_client_idle_lifetime(self):
        self.config['slow']=True
        with self.client() as client:
            time.sleep(1.2)
            self.assertEqual(request(client.url+'/echo',{'loaded':'once'}),{'loaded':'once'})
            self.assertEqual(len(self.processes),1)
    def test_clients_arriving_during_model_loading_wait_and_share_one_start(self):
        self.config['slow']=True;release=threading.Event();results=[];errors=[]
        def attach():
            try:
                with self.client() as c:results.append(c.url);release.wait(8)
            except Exception as error:errors.append(str(error))
        first=threading.Thread(target=attach);second=threading.Thread(target=attach)
        try:
            first.start();deadline=time.monotonic()+5
            while not self.processes and time.monotonic()<deadline:time.sleep(.01)
            second.start();deadline=time.monotonic()+7
            while len(results)<2 and not errors and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(errors,[]);self.assertEqual(len(results),2)
            self.assertEqual(results[0],results[1]);self.assertEqual(len(self.processes),1)
        finally:release.set();first.join(timeout=5);second.join(timeout=5)
    def test_gpu_tests_and_production_run_retain_exclusive_ownership(self):
        from types import SimpleNamespace
        args=SimpleNamespace(**self.config,server=Path('server'),profile='MID',command='longflow')
        args.model=Path('model');args.spec_type='draft-mtp,ngram-mod'
        with patch('backend.SharedBackend',side_effect=AssertionError('GPU test must be exclusive')):
            with backend.connection(args,FakeModel,contextlib.nullcontext):pass

    def test_independent_windows_launcher_preserves_quoting_and_does_not_use_client_job(self):
        import base64
        with patch('backend.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as run:
            backend.launch([sys.executable,'-B',"D:\\source app's\\backend.py",'--owner','{"model":"a b"}'])
        command=run.call_args.args[0]
        self.assertIn('-NoProfile',command)
        script=base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('ShellExecute',script);self.assertIn("app''s",script)
        self.assertNotIn('start_process',script)

    def test_pinned_cli_structured_report_and_chat_http_share_the_actual_proxy(self):
        from tests.test_native import CLI,Endpoint
        from harness import Qwen
        if not CLI.exists():self.skipTest('Pinned native CLI unavailable')
        endpoint=Endpoint(False);endpoint.final_only=True
        class NativeModel:
            def __init__(self,**config):self.config=config
            def __enter__(self):
                handler=endpoint.http.RequestHandlerClass;endpoint.http.server_close()
                endpoint.http=ThreadingHTTPServer(('127.0.0.1',self.config['port']),handler)
                endpoint.thread=threading.Thread(target=endpoint.http.serve_forever,daemon=True)
                return endpoint.__enter__()
            def __exit__(self,*args):endpoint.__exit__(*args)
        owner=threading.Thread(target=backend.owner,args=(self.config,self.root,NativeModel,.5))
        results=[]
        with patch('backend.backend_lock',contextlib.nullcontext):
            try:
                with backend.SharedBackend(self.config,self.root,lambda _:owner.start()) as cli,self.client() as chat:
                    worker=threading.Thread(target=lambda:results.append(Qwen(CLI,'MID',32768,cli.url,self.root/'session').call(
                        'ALGORITHM','Return the assigned report.',self.root,task_id=1)))
                    worker.start()
                    answer=request(chat.url+'/v1/chat/completions',{'model':'qwen-backtrackify','messages':[{'role':'user','content':'Independent chat message'}],
                        'max_tokens':50,'stream':False})
                    worker.join(timeout=30)
                    self.assertFalse(worker.is_alive());self.assertEqual(results[0]['status'],'DONE')
                    self.assertIn('choices',answer);self.assertEqual(len(self.processes),0)
            finally:owner.join(timeout=8)
        self.assertFalse(owner.is_alive());self.assertGreaterEqual(len(endpoint.requests),2)


if __name__=='__main__':unittest.main()
