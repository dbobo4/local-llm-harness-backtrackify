"""Owned Chat reopening, idle cleanup and bulk deletion; no GPU inference."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import backend
import chat
from harness import ROOT,Refused,atomic,request,lock


class BulkDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=chat.Conversations(self.root/'chat','unused',8192)
        self.ids=[self.store.create()['id'] for _ in range(2)]
        for id in self.ids:
            c=self.store.get(id);c.goal='Preserve goal';c.turns=[dict(role='user',content=id),dict(role='assistant',content='Saved')];c.save()
        self.before={id:self.store.path(id).read_bytes() for id in self.ids}
    def targets(self):return [dict(id=id,revision=self.store.revision(self.store.path(id))) for id in self.ids]
    def unchanged(self):
        for id,raw in self.before.items():self.assertEqual(self.store.path(id).read_bytes(),raw)

    def test_confirmation_and_complete_current_target_list_are_mandatory(self):
        for confirm,targets in [(False,self.targets()),(1,self.targets()),(None,self.targets()),(True,None),(True,[]),(True,self.targets()[:1]),(True,self.targets()+self.targets()[:1]),(True,[dict(id='../outside',revision='x')])]:
            with self.subTest(confirm=confirm,targets=targets),self.assertRaises(Refused):self.store.delete_all(confirm,targets)
            self.unchanged()

    def test_all_targets_are_prevalidated_before_the_first_removal(self):
        atomic(self.store.path(self.ids[-1]).parent/'unknown.txt','keep')
        with self.assertRaisesRegex(Refused,'unrecognized'):self.store.delete_all(True,self.targets())
        self.unchanged()

    def test_changed_history_or_new_chat_invalidates_old_confirmation(self):
        targets=self.targets();c=self.store.get(self.ids[-1]);c.summary='Changed';c.save()
        with self.assertRaisesRegex(Refused,'changed'):self.store.delete_all(True,targets)
        self.assertEqual(self.store.path(self.ids[0]).read_bytes(),self.before[self.ids[0]])
        targets=self.targets();new=self.store.create()['id']
        with self.assertRaisesRegex(Refused,'list changed'):self.store.delete_all(True,targets)
        self.assertTrue(self.store.path(new).exists())

    def test_title_only_change_does_not_invalidate_history_confirmation(self):
        targets=self.targets();self.store.rename(self.ids[0],'Cosmetic title')
        self.assertEqual(self.store.delete_all(True,targets),dict(deleted=2))

    def test_linked_target_or_unexpected_root_storage_is_rejected(self):
        linked=self.store.path(self.ids[-1]).parent/'meta.txt';original=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda p:p==linked or original(p)),self.assertRaisesRegex(Refused,'Linked'):
            self.store.delete_all(True,self.targets())
        self.unchanged();atomic(self.store.root/'foreign.txt','preserve')
        with self.assertRaisesRegex(Refused,'unexpected storage'):self.store.delete_all(True,self.targets())
        self.unchanged()

    def test_busy_conversation_refuses_bulk_delete_and_resets_afterward(self):
        with self.store.changing(self.ids[0]),self.assertRaisesRegex(Refused,'busy'):
            self.store.delete_all(True,self.targets())
        self.unchanged();self.assertEqual(self.store.delete_all(True,self.targets())['deleted'],2)

    def test_partial_io_failure_is_reported_without_false_success(self):
        remove=self.store.remove;calls=[]
        def fail(id,target):
            if calls:raise PermissionError('locked fixture file')
            calls.append(id);remove(id,target)
        with patch.object(self.store,'remove',side_effect=fail),self.assertRaisesRegex(Refused,'after 1 of 2'):
            self.store.delete_all(True,self.targets())
        self.assertFalse((self.store.root/self.ids[0]).exists());self.assertTrue(self.store.path(self.ids[1]).exists())
        self.assertFalse(self.store.clearing);self.assertTrue(self.store.create()['id'])

    def test_bulk_flag_prevents_new_creation_or_mutation_during_removal(self):
        original=self.store.remove
        def checked(id,target):
            with self.assertRaisesRegex(Refused,'in progress'):self.store.create()
            with self.assertRaisesRegex(Refused,'busy'):self.store.rename(id,'No')
            original(id,target)
        with patch.object(self.store,'remove',side_effect=checked):self.store.delete_all(True,self.targets())
        self.assertFalse(self.store.clearing)

    def test_storage_change_after_prevalidation_stops_with_honest_partial_result(self):
        original=self.store.remove
        def changed(id,target):
            if id==self.ids[-1]:atomic(self.store.path(id).parent/'unexpected.txt','preserve')
            original(id,target)
        with patch.object(self.store,'remove',side_effect=changed),self.assertRaisesRegex(Refused,'after 1 of 2.*storage changed'):
            self.store.delete_all(True,self.targets())
        self.assertTrue(self.store.path(self.ids[-1]).exists())

    def test_bulk_preserves_cli_harness_other_installation_and_legacy_original(self):
        sentinels=[self.root/'native/qwen.json',self.root/'sessions/task/state.log',self.root/'other-install/chat/history.txt']
        for p in sentinels:atomic(p,'preserve')
        source=self.root/'chat/history.txt';legacy=chat.Conversation(source,'unused',8192);legacy.goal='Old';legacy.turns=[dict(role='user',content='Hi'),dict(role='assistant',content='Hello')];legacy.save();raw=source.read_bytes()
        store=chat.Conversations(self.root/'chat','unused',8192);rows=store.listing()['conversations']
        self.assertEqual(store.delete_all(True,[dict(id=r['id'],revision=r['revision']) for r in rows])['deleted'],2)
        restored=chat.Conversations(self.root/'chat','unused',8192)
        self.assertEqual(restored.listing()['conversations'],[]);self.assertEqual(source.read_bytes(),raw)
        for p in sentinels:self.assertEqual(p.read_text(),'preserve')
        self.assertEqual(restored.delete_all(True,[]),dict(deleted=0));self.assertTrue(restored.create()['id'])


class ChatLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('chat.ROOT',self.root));self.stack.enter_context(patch('chat.status'))
        self.timers=self.stack.enter_context(patch('chat.threading.Timer'))
        self.tokens=self.stack.enter_context(patch('chat.secrets.token_urlsafe',return_value='private-browser-token'))
        self.stack.enter_context(patch.object(chat.Conversation,'tokens',return_value=100))
        self.stack.enter_context(patch.object(chat.Conversation,'complete',return_value='Answer'))
        self.servers=[];self.workers=[];self.errors=[];self.ready=threading.Event()
        real=chat.ChatServer
        def factory(*args,**kwargs):
            server=real(*args,**kwargs);self.servers.append(server);return server
        self.stack.enter_context(patch('chat.ChatServer',side_effect=factory));self.addCleanup(self.stop)
        self.identity='a'*64

    def start(self,idle=3):
        self.ready.clear()
        def run():
            try:chat.serve('unused',8192,0,identity=self.identity,idle_seconds=idle,ready=self.ready.set)
            except BaseException as e:self.errors.append(e);self.ready.set()
        worker=threading.Thread(target=run);self.workers.append(worker);worker.start();self.assertTrue(self.ready.wait(5));self.assertFalse(self.errors)
        self.server=self.servers[-1];self.base=f'http://127.0.0.1:{self.server.server_port}';return worker

    def stop(self):
        for server,worker in zip(self.servers,self.workers):
            if worker.is_alive():server.shutdown();worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertFalse(self.errors)

    def api(self,path,data=None,headers=None,timeout=5):
        h={'Content-Type':'application/json','X-Chat-Token':'private-browser-token'}
        if headers:h.update(headers)
        req=urllib.request.Request(self.base+path,json.dumps(data or {}).encode(),h)
        with urllib.request.urlopen(req,timeout=timeout) as response:return json.load(response)

    def test_no_clients_expires_and_removes_only_owner_record(self):
        worker=self.start(.6);store=chat.Conversations(self.root/'sessions/chat','unused',8192);id=store.create()['id'];raw=store.path(id).read_bytes()
        worker.join(3);self.assertFalse(worker.is_alive());self.assertFalse((chat.chat_home()/'owner.txt').exists())
        self.assertEqual(store.path(id).read_bytes(),raw)

    def test_image_upload_and_retrieval_require_auth_origin_and_conversation_ownership(self):
        import urllib.error
        from tests.test_vision import png
        self.start();a,b=self.api('/new')['id'],self.api('/new')['id'];raw=png()
        def upload(id,headers=None):
            h={'Content-Type':'image/png','X-Chat-Token':'private-browser-token'};h.update(headers or {})
            return urllib.request.urlopen(urllib.request.Request(self.base+'/upload?id='+id,raw,h),timeout=5)
        for headers in ({'X-Chat-Token':'bad'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            with self.subTest(headers=headers),self.assertRaises(urllib.error.HTTPError) as error:upload(a,headers)
            self.assertEqual(error.exception.code,403);error.exception.close()
        with upload(a) as response:image=json.load(response)['image']
        req=urllib.request.Request(self.base+'/image',json.dumps(dict(id=a,image=image)).encode(),{'Content-Type':'application/json','X-Chat-Token':'private-browser-token'})
        with urllib.request.urlopen(req,timeout=5) as response:
            self.assertEqual(response.headers['Content-Type'],'image/png');self.assertTrue(response.read().startswith(b'\x89PNG'))
        for data,headers in ((dict(id=b,image=image),None),(dict(id=a,image='../outside'),None),(dict(id=a,image=image),{'X-Chat-Token':'bad'})):
            with self.subTest(data=data),self.assertRaises(urllib.error.HTTPError) as error:self.api('/image',data,headers)
            error.exception.close()
        self.api('/send',dict(id=a,message='Look',images=[image]))
        self.assertEqual(self.api('/history',dict(id=a))['turns'][0]['images'],[image])

    def test_legacy_http_view_is_expected_unavailable_and_delete_still_requires_auth_and_confirmation(self):
        from tests.test_chat_management import legacy_v2
        self.start();id=self.api('/new')['id'];store=chat.Conversations(self.root/'sessions/chat','unused',8192)
        path=store.path(id);legacy_v2(path);raw=path.read_bytes();row=self.api('/conversations')['conversations'][0]
        view=self.api('/history',dict(id=id,limit=40));self.assertTrue(view['legacy']);self.assertNotIn('turns',view)
        self.assertIn('legacy v2',view['unavailable']);self.assertEqual(path.read_bytes(),raw)
        for headers in ({'X-Chat-Token':'bad'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            with self.subTest(headers=headers),self.assertRaises(urllib.error.HTTPError) as error:self.api('/delete',dict(id=id,confirm=True,revision=row['revision']),headers)
            self.assertEqual(error.exception.code,403);error.exception.close();self.assertTrue(path.is_file())
        with self.assertRaises(urllib.error.HTTPError) as error:self.api('/delete',dict(id=id,confirm=False,revision=row['revision']))
        self.assertEqual(error.exception.code,400);error.exception.close()
        self.assertTrue(self.api('/delete',dict(id=id,confirm=True,revision=row['revision']))['deleted'])
        self.assertFalse(path.parent.exists())

    def test_heartbeats_from_any_tab_refresh_and_last_disappearance_expires(self):
        worker=self.start(.8)
        for tab in ('A','B','B','B'):
            self.assertTrue(self.api('/heartbeat',dict(tab=tab))['connected']);time.sleep(.3);self.assertTrue(worker.is_alive())
        worker.join(3);self.assertFalse(worker.is_alive())

    def test_reopen_startup_guard_prevents_idle_shutdown_race(self):
        worker=self.start(.6)
        with lock(chat.chat_home()/'start.lock'):
            time.sleep(.9);self.assertTrue(worker.is_alive());self.assertTrue(self.api('/heartbeat')['connected'])
        self.assertTrue(worker.is_alive());worker.join(3);self.assertFalse(worker.is_alive())

    def test_reopen_is_live_authenticated_and_keeps_token_and_process(self):
        self.start();path=chat.chat_home()/'owner.txt';raw=path.read_bytes()
        self.assertNotIn(b'private-browser-token',raw)
        self.assertTrue(chat.reopen(self.server.server_port,self.identity));self.assertEqual(len(self.servers),1)
        self.assertEqual(self.tokens.call_count,1);self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.timers.call_count,2)
        urls=[c.kwargs['args'][0] for c in self.timers.call_args_list];self.assertEqual(urls[0],urls[1])

    def test_reopen_works_while_generation_blocks_and_no_idle_exit_or_bulk_race(self):
        entered,release=threading.Event(),threading.Event();result=[];deleted=[]
        def answer(*_):entered.set();self.assertTrue(release.wait(5));return 'Saved answer'
        self.start(.6);id=self.api('/new')['id'];snapshot=self.api('/conversations')['revision']
        with patch.object(chat.Conversation,'complete',side_effect=answer):
            send=threading.Thread(target=lambda:result.append(self.api('/send',dict(id=id,message='Pending'))));send.start();self.assertTrue(entered.wait(3))
            try:
                time.sleep(.9);self.assertTrue(self.workers[0].is_alive());self.assertTrue(chat.reopen(self.server.server_port,self.identity))
                self.assertTrue(self.api('/heartbeat')['connected'])
                self.assertTrue(self.workers[0].is_alive())
                def bulk():
                    try:deleted.append(self.api('/delete-all',dict(confirm=True,revision=snapshot)))
                    except urllib.error.HTTPError as e:deleted.append(e.code);e.close()
                deleting=threading.Thread(target=bulk);deleting.start();time.sleep(.1);self.assertFalse(deleted)
            finally:release.set();send.join(5)
            deleting.join(5)
        self.assertEqual(result[0]['turns'][-1]['content'],'Saved answer')
        self.assertEqual(deleted,[400])
        self.assertEqual(len(self.api('/history',dict(id=id))['turns']),2)

    def test_closed_initiating_client_does_not_abort_generation_or_saved_history(self):
        import http.client
        entered,release=threading.Event(),threading.Event()
        def answer(*_):entered.set();self.assertTrue(release.wait(5));return 'Completed after tab closed'
        worker=self.start(.6);id=self.api('/new')['id']
        with patch.object(chat.Conversation,'complete',side_effect=answer):
            conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
            conn.request('POST','/send',json.dumps(dict(id=id,message='Finish safely')),{'Content-Type':'application/json','X-Chat-Token':'private-browser-token'})
            self.assertTrue(entered.wait(3));conn.close()
            try:time.sleep(.9);self.assertTrue(worker.is_alive())
            finally:release.set()
            worker.join(3);self.assertFalse(worker.is_alive())
        store=chat.Conversations(self.root/'sessions/chat','unused',8192)
        self.assertEqual(store.history(id)['turns'][-1]['content'],'Completed after tab closed')

    def test_idle_restart_preserves_saved_history_and_metadata(self):
        first=self.start(.6);id=self.api('/new')['id'];self.api('/send',dict(id=id,message='Remember this'))
        path=self.root/'sessions/chat/conversations'/id/'history.txt';raw=path.read_bytes();meta=path.with_name('meta.txt').read_bytes()
        first.join(3);self.assertFalse(first.is_alive());self.start(3)
        self.assertEqual(self.api('/history',dict(id=id))['turns'][-1]['content'],'Answer')
        self.assertEqual(path.read_bytes(),raw);self.assertEqual(path.with_name('meta.txt').read_bytes(),meta)

    def test_bulk_endpoint_enforces_headers_and_supports_large_lists_with_small_confirmation(self):
        store=chat.Conversations(self.root/'sessions/chat','unused',8192)
        for _ in range(600):store.create()
        self.start(20)
        revision=self.api('/conversations',timeout=30)['revision'];body=dict(confirm=True,revision=revision)
        self.assertLess(len(json.dumps(body)),128)
        for header in ({'X-Chat-Token':'bad'},{'Host':'other.example'},{'Origin':'https://other.example'}):
            with self.assertRaises(urllib.error.HTTPError) as error:self.api('/delete-all',body,header)
            self.assertEqual(error.exception.code,403);error.exception.close()
        self.assertEqual(len(store.listing()['conversations']),600)
        started=time.monotonic()
        self.assertEqual(self.api('/delete-all',body,timeout=30)['deleted'],600)
        atomic(ROOT/'logs/chat-lifecycle-audit/bulk-performance.txt',f'Conversations: 600\nConfirmation bytes: {len(json.dumps(body).encode())}\nDelete elapsed seconds: {time.monotonic()-started:.3f}\n')
        self.assertEqual(self.api('/conversations')['conversations'],[])

    def test_unauthenticated_calls_do_not_reveal_tokens_or_pin_idle_lifetime(self):
        worker=self.start(.6)
        for route in ('/reopen','/heartbeat','/delete-all'):
            with self.assertRaises(urllib.error.HTTPError) as error:self.api(route,{}, {'X-Chat-Token':'invalid'})
            body=error.exception.read().decode();self.assertEqual(error.exception.code,403);self.assertNotIn('private-browser-token',body);error.exception.close()
        with urllib.request.urlopen(self.base,timeout=3) as response:self.assertNotIn('private-browser-token',response.read().decode())
        worker.join(3);self.assertFalse(worker.is_alive())

    def test_bad_token_requests_return_403_instead_of_windows_socket_reset(self):
        self.start()
        for _ in range(8):
            with self.assertRaises(urllib.error.HTTPError) as error:self.api('/conversations',{}, {'X-Chat-Token':'bad'})
            self.assertEqual(error.exception.code,403);error.exception.read();error.exception.close()

    def test_settings_mismatch_and_foreign_installation_fail_closed(self):
        self.start();count=self.timers.call_count
        with self.assertRaisesRegex(Refused,'different settings'):chat.reopen(self.server.server_port,'b'*64)
        self.assertEqual(self.timers.call_count,count)
        other=self.root/'other-install';(other/'sessions/chat').mkdir(parents=True);(other/'sessions/chat/owner.txt').write_bytes((chat.chat_home()/'owner.txt').read_bytes())
        with patch('chat.ROOT',other),self.assertRaises(Refused):chat.reopen(self.server.server_port,self.identity)
        self.assertEqual(self.timers.call_count,count);self.assertTrue(self.workers[0].is_alive())

    def test_stale_record_returns_to_normal_bind_and_corrupt_records_are_refused(self):
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        path=chat.chat_home()/'owner.txt';chat.owner_record(path,port,self.identity,secrets.token_bytes(32))
        self.assertFalse(chat.reopen(port,self.identity));atomic(path,'not an owned record')
        with self.assertRaisesRegex(Refused,'ownership record'):chat.reopen(port,self.identity)

    def test_unrelated_server_cannot_claim_ownership_or_be_stopped(self):
        class Foreign(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)));self.send_response(200);self.end_headers();self.wfile.write(b'{"opened":true,"identity":"fake","proof":"fake"}')
        with ThreadingHTTPServer(('127.0.0.1',0),Foreign) as server:
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                path=chat.chat_home()/'owner.txt';chat.owner_record(path,server.server_port,self.identity,secrets.token_bytes(32))
                with self.assertRaisesRegex(Refused,'ownership could not be verified'):chat.reopen(server.server_port,self.identity)
                self.assertTrue(thread.is_alive());self.timers.assert_not_called()
                path.unlink()
                with self.assertRaisesRegex(Refused,'occupied'):
                    chat.launch(SimpleNamespace(chat_port=server.server_port,context=8192),lambda:self.fail('Unrelated port must not load a model'))
                self.assertTrue(thread.is_alive())
            finally:server.shutdown();thread.join(3)

    def test_owned_reopen_never_acquires_another_backend_lease(self):
        args=SimpleNamespace(chat_port=0,context=8192,server=Path('server'),model=Path('model'),profile='MID',batch=512,ubatch=256,threads=8,port=8080,spec_type='none')
        entered=[];exited=[]
        @contextlib.contextmanager
        def connect():
            entered.append(True)
            try:yield SimpleNamespace(url='unused')
            finally:exited.append(True)
        original=chat.serve
        def serve(*a,**kw):
            callback=kw.get('ready')
            def published():
                if callback:callback()
                self.ready.set()
            kw.update(idle_seconds=.8,ready=published);return original(*a,**kw)
        def run():
            try:chat.launch(args,connect)
            except BaseException as error:self.errors.append(error);self.ready.set()
        with patch('chat.serve',side_effect=serve):
            worker=threading.Thread(target=run);self.workers.append(worker);worker.start();self.assertTrue(self.ready.wait(5));self.assertFalse(self.errors)
            self.assertEqual(chat.launch(args,connect),0);self.assertEqual(len(entered),1)
            worker.join(3);self.assertFalse(worker.is_alive());self.assertEqual(len(exited),1)

    def test_simultaneous_launches_wait_for_publication_and_share_one_chat(self):
        args=SimpleNamespace(chat_port=0,context=8192)
        loading,release=threading.Event(),threading.Event();entries=[];results=[]
        @contextlib.contextmanager
        def connect():
            entries.append(True);loading.set();self.assertTrue(release.wait(5));yield SimpleNamespace(url='unused')
        original=chat.serve
        def serve(*a,**kw):
            kw['idle_seconds']=1;return original(*a,**kw)
        def run():
            try:results.append(chat.launch(args,connect))
            except BaseException as error:self.errors.append(error)
        with patch('chat.serve',side_effect=serve):
            first=threading.Thread(target=run);second=threading.Thread(target=run)
            first.start();self.assertTrue(loading.wait(3));second.start()
            try:time.sleep(.2);self.assertEqual(len(entries),1)
            finally:release.set()
            second.join(4);first.join(4)
            self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertFalse(self.errors);self.assertEqual(results,[0,0]);self.assertEqual(len(self.servers),1);self.assertEqual(len(entries),1)

    def test_user_protected_control_secret_roundtrips_in_a_separate_python_process(self):
        payload=chat.protect(b'x'*32)
        code='import chat,sys; from pathlib import Path; chat.ROOT=Path(sys.argv[1]); assert chat.protect(bytes.fromhex(sys.argv[2]),decrypt=True)==b"x"*32; print("ok")'
        result=subprocess.run([sys.executable,'-B','-c',code,str(self.root),payload.hex()],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'ok')

    def test_main_checks_chat_reopen_before_backend_connection(self):
        import harness
        with patch('harness.gguf_metadata',return_value={'qwen35.context_length':262144}),patch('chat.launch',return_value=0) as launch,patch('backend.connection') as connect,patch('harness.status'):
            self.assertEqual(harness.main(['chat','--server','server','--model','model','--qwen','qwen','--spec-type','none']),0)
        launch.assert_called_once();connect.assert_not_called()

    def test_chat_idle_shutdown_releases_its_lease_without_killing_cli(self):
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        backend_root=self.root/'backend';backend_root.mkdir()
        config=dict(exe='fake',model='fake',context=8192,batch=512,ubatch=256,port=port,thinking=1024,threads=2,spec_type='none',gpu='0')
        processes=[]
        def launcher(command):
            code='from tests.test_backend import fixture_owner; import json,sys; fixture_owner(json.loads(sys.argv[1]),sys.argv[2])'
            with (backend_root/'owner.log').open('ab') as log:
                processes.append(subprocess.Popen([sys.executable,'-B','-c',code,json.dumps(config),str(backend_root)],stdout=log,stderr=subprocess.STDOUT))
        cli=backend.SharedBackend(config,backend_root,launcher)
        args=SimpleNamespace(chat_port=0,context=8192,server=Path('server'),model=Path('model'),profile='MID',batch=512,ubatch=256,threads=8,port=port,spec_type='none')
        original=chat.serve
        def serve(*a,**kw):
            callback=kw.get('ready')
            def published():
                if callback:callback()
                self.ready.set()
            kw.update(idle_seconds=.6,ready=published);return original(*a,**kw)
        def run():
            try:chat.launch(args,lambda:backend.SharedBackend(config,backend_root,launcher))
            except BaseException as e:self.errors.append(e);self.ready.set()
        try:
            cli.__enter__()
            with patch('chat.serve',side_effect=serve):
                worker=threading.Thread(target=run);self.workers.append(worker);worker.start();self.assertTrue(self.ready.wait(5));self.assertFalse(self.errors)
                worker.join(3);self.assertFalse(worker.is_alive())
            self.assertEqual(request(cli.url+'/echo',dict(client='CLI remains')),dict(client='CLI remains'))
            self.assertIsNone(processes[0].poll());cli.__exit__();processes[0].wait(timeout=5)
        finally:
            cli.__exit__()
            for p in processes:
                if p.poll() is None:p.kill()
                p.wait(5)


if __name__=='__main__':unittest.main()
