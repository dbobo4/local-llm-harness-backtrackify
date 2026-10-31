"""Headless Chromium with real Chat HTTP/storage and scripted model replies."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chat
from harness import ROOT, Refused, start_process, stop_process


def browser():
    for root in (os.environ.get('ProgramFiles',''),os.environ.get('ProgramFiles(x86)',''),os.environ.get('LOCALAPPDATA','')):
        for relative in ('Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe'):
            path=Path(root)/relative
            if path.is_file():return str(path)
    return shutil.which('chromium') or shutil.which('google-chrome')


CLI=Path(os.environ.get('BACKTRACKIFY_NATIVE_CLI',str(ROOT/'runtime/qwen/qwen-code/lib/cli.js')))
NODE=CLI.parent.parent/'node/node.exe'
NODE=str(NODE) if NODE.is_file() else shutil.which('node')
BROWSER=browser()


@unittest.skipUnless(BROWSER and NODE,'Chromium and Node required for real browser checks; no GPU/model')
class ChatBrowserTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='Chat browser ')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.servers=[];self.errors=[];self.token='browser-fixture-token'
        self.patches=contextlib.ExitStack();self.addCleanup(self.patches.close)
        real=chat.ChatServer
        def factory(*args):
            server=real(*args);self.servers.append(server);return server
        self.model_messages=[]
        def answer(_,messages,*budgets):
            self.model_messages.append(messages)
            if messages[-1]['content']=='invalid-completion':raise Refused('Chat output incomplete; history retained. Use a smaller request.')
            content=messages[-1]['content']
            if isinstance(content,list):content=content[0]['text']
            return content+' answer'
        self.patches.enter_context(patch('chat.ROOT',self.root))
        self.patches.enter_context(patch('chat.ChatServer',side_effect=factory))
        self.patches.enter_context(patch('chat.threading.Timer'))
        self.patches.enter_context(patch('chat.status'))
        self.patches.enter_context(patch('chat.secrets.token_urlsafe',side_effect=lambda _:self.token))
        self.patches.enter_context(patch.object(chat.Conversation,'tokens',return_value=100))
        self.patches.enter_context(patch.object(chat.Conversation,'complete',answer))
        def native(url,body,**kwargs):
            if url.endswith('/apply-template'):return dict(prompt=json.dumps(body['messages']))
            if url.endswith('/tokenize'):return dict(tokens=[1]*(2048 if 'Only A retained' in body['content'] else 1024))
            raise AssertionError('Unexpected model request: '+url)
        self.patches.enter_context(patch('chat.request',side_effect=native))
        store=chat.Conversations(self.root/'sessions/chat','http://unused-controlled-model',8192)
        self.a,self.b=store.create()['id'],store.create()['id']
        self.code='def greet(name):\r\n    print("Hello", name)\r\n    text = "'+'x'*250+'"\r\n\r\ngreet("World")\r\n'
        a=store.get(self.a);a.goal='Conversation A';a.summary='Only A retained context';a.rollovers=1
        a.archived=[dict(role='user',content='Archived A request'),dict(role='assistant',content='Archived A answer')]
        a.turns=[dict(role='user',content='Current A request\n<img src=x onerror="window.__xss=1">'),dict(role='assistant',content='## A useful answer\nA paragraph with `inline_code` and **emphasis**.\n\n- First point\n- Second point\n\n```python\r\n'+self.code+'```\r\n\n```javascript\nconst text = "<script>window.__xss=2</script>";\n```\n\n'+'long_code_'*80)]
        a.save();b=store.get(self.b);b.goal='Conversation B';b.turns=[dict(role='user',content='Only B request'),dict(role='assistant',content='Only B answer')];b.save()
        self.before=self.histories()
        self.start()

    def histories(self):
        return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'sessions/chat/conversations').glob('*/history.txt')}

    def start(self,port=0):
        count=len(self.servers)
        def run():
            try:chat.serve('http://unused-controlled-model',8192,port)
            except BaseException as error:self.errors.append(error)
        self.worker=threading.Thread(target=run);self.worker.start()
        self.addCleanup(self.stop)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if len(self.servers)>count:break
            if self.errors:raise self.errors[0]
            time.sleep(.02)
        self.assertGreater(len(self.servers),count)
        self.server=self.servers[-1];self.base=f'http://127.0.0.1:{self.server.server_port}'

    def stop(self):
        if getattr(self,'worker',None) and self.worker.is_alive():
            self.server.shutdown();self.worker.join(timeout=5)
            self.assertFalse(self.worker.is_alive())
        self.assertFalse(self.errors)

    def run_browser(self,scenario):
        config=dict(base=self.base,url=self.base+'/#'+self.token,token=self.token,nextToken='next-browser-fixture-token',a=self.a,b=self.b,code=self.code)
        if scenario in {'images','drop-area'}:
            import base64
            from tests.test_vision import png
            path=self.root/'picker.png';path.write_bytes(png())
            config['imagePath']=str(path);config['imageData']=base64.b64encode(png('blue')).decode()
        if scenario in {'screenshots','menu'}:
            destination=ROOT/'logs'/('chat-management-audit' if scenario=='screenshots' else 'chat-lifecycle-audit')
            destination.mkdir(parents=True,exist_ok=True);config['output']=str(destination)
        fixture=self.root/'fixture.json';fixture.write_text(json.dumps(config),encoding='utf-8')
        profile=self.root/'browser-profile';log=self.root/'browser.log'
        with log.open('wb') as output:
            # Test-only: restricted Windows hosts cannot start Chromium's sandboxed children.
            # The browser owns a temporary profile, visits only our fixture, and uses no GPU.
            process=start_process([BROWSER,'--headless=new','--disable-gpu','--no-sandbox','--remote-debugging-port=0','--user-data-dir='+str(profile),
                '--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-extensions','--disable-component-update',
                '--disable-sync','--disable-breakpad','--metrics-recording-only','about:blank'],stdout=output,stderr=subprocess.STDOUT,
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
            try:
                port=profile/'DevToolsActivePort';deadline=time.monotonic()+8
                while not port.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.03)
                self.assertTrue(port.exists(),'Owned headless browser did not start: '+log.read_text(encoding='utf-8',errors='replace')[-1200:])
                args=[NODE,str(ROOT/'tests/chat_browser.js'),port.read_text().splitlines()[0],scenario,str(fixture)]
                if scenario in {'restart','bulk'}:
                    runner=start_process(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
                    try:
                        marker=runner.stdout.readline();self.assertEqual(marker.strip(),'RESTART_REQUEST',marker)
                        chat_port=self.server.server_port;self.stop();self.token=config['nextToken'];self.start(chat_port)
                        output,_=runner.communicate('continue\n',timeout=30)
                        self.assertEqual(runner.returncode,0,output)
                    finally:stop_process(runner)
                else:
                    result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=40)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            finally:stop_process(process)

    def test_refresh_restores_auth_active_conversation_and_complete_archived_history(self):
        self.run_browser('refresh');self.assertEqual(self.histories(),self.before)

    def test_missing_invalid_and_new_url_tokens_are_handled_without_false_empty_history(self):
        self.run_browser('auth');self.assertEqual(self.histories(),self.before)

    def test_markdown_roles_exact_code_copy_and_untrusted_html_are_safe(self):
        self.run_browser('markdown');self.assertEqual(self.histories(),self.before)

    def test_dark_default_and_manual_themes_persist_without_system_or_persistent_auth_storage(self):
        self.run_browser('theme');self.assertEqual(self.histories(),self.before)

    def test_generation_saved_network_and_completion_errors_preserve_input(self):
        self.run_browser('send')
        store=chat.Conversations(self.root/'sessions/chat','unused',8192)
        self.assertEqual(len(store.history(self.b)['turns']),4)
        self.assertNotIn('invalid-completion',str(store.history(self.b)))

    def test_sidebar_switch_cannot_display_an_inflight_answer_in_another_chat(self):
        self.run_browser('switch')
        self.assertEqual(len(chat.Conversations(self.root/'sessions/chat','unused',8192).history(self.b)['turns']),2)

    def test_new_conversation_remains_separate_and_survives_page_refresh(self):
        self.run_browser('new')
        self.assertEqual(len(chat.Conversations(self.root/'sessions/chat','unused',8192).listing()['conversations']),3)

    def test_narrow_layout_keeps_history_sidebar_and_composer_separate(self):
        self.run_browser('layout');self.assertEqual(self.histories(),self.before)

    def test_real_server_restart_rejects_old_token_and_recovers_both_histories_with_new_link(self):
        self.run_browser('restart');self.assertEqual(self.histories(),self.before)

    def test_blocked_browser_storage_keeps_initial_fragment_usable_after_refresh(self):
        self.run_browser('storage');self.assertEqual(self.histories(),self.before)

    def test_stats_start_time_and_cosmetic_rename_remain_independent_and_persistent(self):
        self.run_browser('management')
        self.assertEqual(self.histories(),self.before)
        store=chat.Conversations(self.root/'sessions/chat','unused',8192)
        self.assertEqual(store.get(self.a).goal,'Conversation A')
        self.assertEqual(store.metadata(self.a)['title'],'<script>A renamed</script>')

    def test_errors_stay_visible_across_success_and_switch_until_dismissed(self):
        self.run_browser('notifications');self.assertEqual(self.histories(),self.before)

    def test_confirmed_deletion_preserves_other_conversation_then_allows_empty_state(self):
        self.run_browser('deletion')
        self.assertEqual(chat.Conversations(self.root/'sessions/chat','unused',8192).listing()['conversations'],[])

    def test_thousands_of_messages_load_backwards_without_scroll_jumps_or_context_changes(self):
        store=chat.Conversations(self.root/'sessions/chat','unused',8192);c=store.get(self.a)
        messages=[dict(role='user' if i%2==0 else 'assistant',content=f'Long message {i}\n'+('Retained text. '*25)) for i in range(6000)]
        c.archived,c.turns=messages[:-4],messages[-4:];c.save();self.before=self.histories()
        self.run_browser('long');self.assertNotEqual(self.histories(),self.before) # One intentional new pair.
        restored=store.get(self.a);self.assertEqual(restored.public()['turns'][:6000],messages)
        self.assertEqual(len(restored.public()['turns']),6002)

    def test_menu_has_three_dots_floats_without_clipping_and_supports_keyboard(self):
        self.run_browser('menu');self.assertEqual(self.histories(),self.before)

    def test_bulk_confirmation_cancel_delete_refresh_restart_and_new_conversation(self):
        self.run_browser('bulk')
        store=chat.Conversations(self.root/'sessions/chat','unused',8192)
        self.assertEqual(len(store.listing()['conversations']),1)
        self.assertFalse((store.root/self.a).exists());self.assertFalse((store.root/self.b).exists())

    def test_image_picker_drop_removal_history_view_reuse_and_refresh(self):
        import vision
        self.run_browser('images')
        store=chat.Conversations(self.root/'sessions/chat','unused',8192);turns=store.history(self.b)['turns']
        self.assertEqual(len(turns[-6]['images']),2);self.assertNotIn('images',turns[-4]);self.assertEqual(len(turns[-2]['images']),1)
        self.assertTrue(vision.has_images(self.model_messages[0]));self.assertFalse(vision.has_images(self.model_messages[1]))
        self.assertTrue(vision.has_images(self.model_messages[2]))
        self.assertEqual(len(store.history(self.a)['turns']),4)

    def test_legacy_selection_is_not_connection_failure_and_confirmed_delete_stays_available(self):
        from tests.test_chat_management import legacy_v2
        store=chat.Conversations(self.root/'sessions/chat','unused',8192);legacy_v2(store.path(self.b))
        raw=store.path(self.a).read_bytes();self.run_browser('legacy')
        self.assertFalse(store.path(self.b,existing=False).parent.exists());self.assertEqual(store.path(self.a).read_bytes(),raw)

    def test_main_history_drop_zone_accepts_image_and_preserves_attachment_pipeline(self):
        self.run_browser('drop-area')
        store=chat.Conversations(self.root/'sessions/chat','unused',8192);turns=store.history(self.b)['turns']
        self.assertEqual(len(turns[-2]['images']),1);self.assertEqual(turns[-2]['content'],'Image dropped above composer')

    def test_late_statistics_error_cannot_replace_a_new_conversations_state(self):
        self.run_browser('stale-stats');self.assertEqual(self.histories(),self.before)


if __name__=='__main__':unittest.main()
