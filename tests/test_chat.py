"""Chat-only persistence and HTTP regressions; no model/GPU calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import chat
from harness import ROOT, Refused, atomic, clone


class ChatTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'history.txt'

    def conversation(self):
        c=chat.Conversation(self.path,'http://unused',8192)
        c.goal='Keep Unicode \u2603 and the public API.'
        c.turns=[dict(role='user' if i%2==0 else 'assistant',content=f'message {i}\r\nline') for i in range(6)]
        c.save();return c

    def unchanged(self,c,before,state):
        self.assertEqual(c.path.read_bytes(),before)
        self.assertEqual((c.goal,c.summary,c.turns,c.archived,c.rollovers),state)

    def snapshot(self,c):
        return c.path.read_bytes(),copy.deepcopy((c.goal,c.summary,c.turns,c.archived,c.rollovers))

    def test_only_complete_plain_text_responses_are_committed(self):
        c=self.conversation();before,state=self.snapshot(c)
        for reason in (None,'length','tool_calls','content_filter','unknown'):
            response={'choices':[{'message':{'content':'looks useful'},'finish_reason':reason}]}
            with self.subTest(reason=reason),patch.object(c,'tokens',return_value=100),patch('chat.request',return_value=response):
                with self.assertRaises(Refused):c.send('Continue')
                self.unchanged(c,before,state)
        response={'choices':[{'message':{'content':'Complete answer'},'finish_reason':'stop'}]}
        with patch.object(c,'tokens',return_value=100),patch('chat.request',return_value=response) as api:
            c.send('Continue')
        self.assertEqual(c.turns[-1]['content'],'Complete answer')
        body=api.call_args.args[1]
        self.assertEqual((body['max_tokens'],body['thinking_budget_tokens'],body['stream']),(3072,1024,False))

    def test_malformed_or_tool_completion_is_not_a_chat_answer(self):
        c=self.conversation();before,state=self.snapshot(c)
        bad=[{},None,{'choices':[]},{'choices':[None]},
             {'choices':[{'finish_reason':'stop','message':{'content':None}}]},
             {'choices':[{'finish_reason':'stop','message':{'content':'  '}}]},
             {'choices':[{'finish_reason':'stop','message':{'content':'text','tool_calls':[{}]}}]}]
        for response in bad:
            with self.subTest(response=response),patch.object(c,'tokens',return_value=100),patch('chat.request',return_value=response):
                with self.assertRaises(Refused):c.send('Continue')
                self.unchanged(c,before,state)

    def test_short_conversation_does_not_summarize_or_archive(self):
        c=chat.Conversation(self.path,'http://unused',8192)
        with patch.object(c,'tokens',return_value=100),patch.object(c,'complete',return_value='Answer') as complete:
            result=c.send('Original request')
        self.assertEqual(complete.call_count,1);self.assertEqual(c.rollovers,0);self.assertFalse(c.archived)
        restored=chat.Conversation(self.path,c.backend,c.context)
        self.assertEqual(restored.public(),result);self.assertEqual(restored.goal,'Original request')

    def test_snapshot_distributes_the_local_frontend_without_runtime_or_external_assets(self):
        destination=self.root/'snapshot'
        manifest=clone(ROOT,destination,snapshot=True)
        self.assertIn('chat.html',manifest);self.assertIn('tests/chat_browser.js',manifest)
        self.assertEqual((destination/'chat.html').read_text(encoding='utf-8'),chat.PAGE)
        result=subprocess.run([sys.executable,'-B','-c','import chat; print(len(chat.PAGE))'],cwd=destination,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(int(result.stdout),len(chat.PAGE))

    def test_rollovers_preserve_full_history_but_only_send_the_working_suffix(self):
        c=self.conversation();original=copy.deepcopy(c.turns)
        for i in range(2):
            with patch.object(c,'tokens',side_effect=[8000,100,100]),patch.object(c,'complete',side_effect=[f'Preserve API; summary {i}',f'answer {i}']) as complete:
                c.send(f'next {i}')
            model=complete.call_args.args[0]
            self.assertNotIn('message 0',str(model));self.assertIn(f'summary {i}',str(model))
            self.assertEqual(c.public()['turns'][:len(original)],original)
            c=chat.Conversation(self.path,c.backend,c.context)
        self.assertEqual(c.rollovers,2);self.assertEqual(len(c.public()['turns']),10)
        self.assertEqual([t['content'] for t in c.public()['turns']].count('next 0'),1)
        self.assertEqual(c.turns[-1]['content'],'answer 1')

    def test_pending_request_must_fit_before_any_rollover_is_saved(self):
        c=self.conversation();before,state=self.snapshot(c)
        with patch.object(c,'tokens',side_effect=[8000,100,8000]),patch.object(c,'complete',return_value='Short summary') as complete,patch('chat.atomic') as save:
            with self.assertRaisesRegex(Refused,'still exceeds'):c.send('Pending request')
        self.assertEqual(complete.call_count,1);save.assert_not_called();self.unchanged(c,before,state)

    def test_failed_or_interrupted_summary_preserves_original_history(self):
        c=self.conversation();before,state=self.snapshot(c)
        for failure in (OSError('disconnected'),KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__),patch.object(c,'tokens',side_effect=[8000,100]),patch.object(c,'complete',side_effect=failure):
                with self.assertRaises(type(failure)):c.send('Pending')
                self.unchanged(c,before,state)

    def test_invalid_summary_completion_cannot_replace_history(self):
        c=self.conversation();before,state=self.snapshot(c)
        response={'choices':[{'finish_reason':'length','message':{'content':'cut off summary'}}]}
        with patch.object(c,'tokens',side_effect=[8000,100]),patch('chat.request',return_value=response):
            with self.assertRaises(Refused):c.send('Pending')
        self.unchanged(c,before,state)

    def test_final_generation_failure_also_discards_candidate_rollover(self):
        c=self.conversation();before,state=self.snapshot(c)
        with patch.object(c,'tokens',side_effect=[8000,100,100]),patch.object(c,'complete',side_effect=['Useful summary',Refused('not completed')]):
            with self.assertRaises(Refused):c.send('Pending')
        self.unchanged(c,before,state)

    def test_failed_atomic_replace_preserves_archive_context_and_disk(self):
        c=self.conversation();before,state=self.snapshot(c)
        with patch.object(c,'tokens',side_effect=[8000,100,100]),patch.object(c,'complete',side_effect=['Useful summary','Answer']),patch('harness.os.replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):c.send('Pending')
        self.unchanged(c,before,state);self.assertFalse(list(self.root.glob('*.part')))

    def test_interrupted_commit_does_not_change_live_or_saved_state(self):
        c=self.conversation();before,state=self.snapshot(c)
        with patch.object(c,'tokens',return_value=100),patch.object(c,'complete',return_value='Answer'),patch('chat.atomic',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):c.send('Pending')
        self.unchanged(c,before,state)

    def test_invalid_archive_boundaries_are_refused(self):
        self.conversation();before=self.path.read_text(encoding='utf-8')
        for boundary in ('1','100','-2','not-a-number'):
            atomic(self.path,before.replace('archived 0\n',f'archived {boundary}\n'))
            with self.subTest(boundary=boundary),self.assertRaises(Refused):chat.Conversation(self.path,'unused',8192)

    def test_obsolete_history_is_refused_without_modification(self):
        old='Backtrackify chat 1\nrollovers 1\ngoal 4\nGoal\nsummary 3\nOld\nuser 2\nHi\nassistant 5\nHello\n'
        for version in (1,2):
            raw=old.replace('chat 1','chat '+str(version));atomic(self.path,raw)
            with self.assertRaises(Refused):chat.Conversation(self.path,'unused',8192)
            self.assertEqual(self.path.read_text(),raw)

    def store(self):
        return chat.Conversations(self.root/'chat','http://unused',8192)

    def test_conversations_have_independent_histories_summaries_and_rollovers(self):
        store=self.store();a,b=store.create()['id'],store.create()['id']
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',side_effect=lambda messages,*_:messages[-1]['content']+' answer'):
            for id,prefix in ((a,'A'),(b,'B')):
                for i in range(3):store.send(id,f'{prefix} {i}')
        c=store.get(a)
        with patch.object(c,'tokens',return_value=100),patch.object(c,'complete',return_value='Only A constraints'):c.compact(3072)
        self.assertEqual(store.get(a).rollovers,1);self.assertEqual(store.get(b).rollovers,0)
        self.assertEqual(store.get(b).summary,'');self.assertNotIn('B ',str(store.history(a)))
        self.assertNotIn('A ',str(store.history(b)))
        restarted=self.store()
        self.assertEqual({r['id'] for r in restarted.listing()['conversations']},{a,b})
        self.assertEqual(restarted.history(a),store.history(a));self.assertEqual(restarted.history(b),store.history(b))

    def test_chat_never_imports_root_or_harness_memory(self):
        atomic(self.root/'memory.txt','ROOT SECRET');atomic(self.root/'sessions/task/memory.txt','TASK SECRET')
        store=self.store();id=store.create()['id'];sent=[]
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',side_effect=lambda messages,*_:sent.append(messages) or 'Answer'):
            store.send(id,'Chat request')
        self.assertNotIn('SECRET',str(sent))

    def legacy(self):
        path=self.root/'chat/history.txt'
        atomic(path,'Backtrackify chat 1\nrollovers 1\ngoal 4\nGoal\nsummary 3\nOld\nuser 2\nHi\nassistant 5\nHello\n')
        return path

    def test_v3_restart_preserves_history_without_importing_legacy_source(self):
        source=self.legacy();raw=source.read_bytes();store=self.store()
        id=store.create()['id']
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',return_value='New answer'):
            store.send(id,'New request')
        restarted=self.store()
        self.assertEqual(len(restarted.listing()['conversations']),1)
        self.assertEqual(restarted.history(id),store.history(id))
        self.assertEqual(source.read_bytes(),raw)

    def test_obsolete_legacy_sources_are_preserved_and_not_imported(self):
        source=self.legacy()
        for raw in ('','Backtrackify chat 1\nrollovers 0\ngoal 0\n\nsummary 0\n\n',source.read_text()[:-2]):
            atomic(source,raw)
            with self.subTest(raw=raw[:20]):self.assertEqual(self.store().listing()['conversations'],[])
            self.assertEqual(source.read_text(),raw);self.assertFalse((source.parent/'conversations/legacy/history.txt').exists())

    def test_failed_new_save_preserves_existing_conversations_and_legacy_source(self):
        store=self.store();id=store.create()['id'];other=store.path(id).read_bytes();source=self.legacy();raw=source.read_bytes()
        with patch('chat.atomic',side_effect=OSError('disk full')),self.assertRaises(OSError):store.create()
        self.assertEqual(source.read_bytes(),raw);self.assertEqual(store.path(id).read_bytes(),other)
        restarted=self.store();self.assertEqual({r['id'] for r in restarted.listing()['conversations']},{id})
        self.assertEqual(source.read_bytes(),raw)

    def test_changed_legacy_source_never_overwrites_managed_v3_history(self):
        source=self.legacy();store=self.store();id=store.create()['id'];target=store.path(id);before=target.read_bytes()
        atomic(source,source.read_text().replace('Hello','HELLO'))
        self.assertEqual(len(self.store().listing()['conversations']),1)
        self.assertEqual(target.read_bytes(),before)
        self.assertIn('HELLO',source.read_text())

    def test_invalid_unknown_or_linked_conversation_paths_are_refused(self):
        store=self.store();id=store.create()['id']
        for value in (None,{},'../history','..\\history','/absolute','A'*32,'legacy/../other','0'*32):
            with self.subTest(value=value),self.assertRaises(Refused):store.history(value)
        with patch.object(Path,'is_symlink',return_value=True),self.assertRaisesRegex(Refused,'Linked'):store.history(id)

    def test_damaged_conversation_is_visible_without_hiding_other_history(self):
        store=self.store();bad,good=store.create()['id'],store.create()['id'];atomic(store.path(bad),'broken')
        rows={r['id']:r for r in store.listing()['conversations']}
        self.assertIn('error',rows[bad]);self.assertNotIn('error',rows[good])
        self.assertEqual(store.history(good)['turns'],[])

    def test_failed_new_conversation_does_not_leave_a_selectable_empty_file(self):
        store=self.store()
        with patch('chat.atomic',side_effect=OSError('disk full')),self.assertRaises(OSError):store.create()
        self.assertEqual(store.listing()['conversations'],[]);self.assertEqual(list(store.root.iterdir()),[])

    def test_actual_http_requests_are_serial_and_bound_to_their_conversation(self):
        real=chat.ChatServer;ready=threading.Event();servers=[];errors=[]
        def factory(*args):
            server=real(*args);servers.append(server);ready.set();return server
        def serve():
            try:chat.serve('http://unused',8192,0)
            except BaseException as error:errors.append(error)
        entered,release=threading.Event(),threading.Event();seen=[]
        def answer(_,messages,*budgets):
            text=messages[-1]['content'];seen.append(text)
            if text=='A request':entered.set();self.assertTrue(release.wait(5))
            return text+' answer'
        with patch('chat.ROOT',self.root),patch('chat.secrets.token_urlsafe',return_value='test-token'),patch('chat.ChatServer',side_effect=factory),patch('chat.threading.Timer'),patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',answer):
            worker=threading.Thread(target=serve);worker.start();self.assertTrue(ready.wait(5))
            url=f'http://127.0.0.1:{servers[0].server_port}'
            def api(path,data):
                req=urllib.request.Request(url+path,json.dumps(data).encode(),{'Content-Type':'application/json','X-Chat-Token':'test-token'})
                with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
            try:
                with urllib.request.urlopen(url,timeout=5) as response:
                    page=response.read().decode('utf-8')
                self.assertEqual(page,chat.PAGE);self.assertNotIn('test-token',page)
                for headers in ({},{'X-Chat-Token':'incorrect'},
                                {'X-Chat-Token':'test-token','Host':'foreign.example'},
                                {'X-Chat-Token':'test-token','Origin':'https://foreign.example'},
                                {'X-Chat-Token':'test-token','Origin':'null'}):
                    req=urllib.request.Request(url+'/conversations',b'{}',dict({'Content-Type':'application/json'},**headers))
                    with self.subTest(headers=headers),self.assertRaises(urllib.error.HTTPError) as rejected:
                        urllib.request.urlopen(req,timeout=5)
                    self.assertEqual(rejected.exception.code,403)
                    self.assertNotIn('test-token',rejected.exception.read().decode());rejected.exception.close()
                a,b=api('/new',{})['id'],api('/new',{})['id']
                with ThreadPoolExecutor(2) as pool:
                    first=pool.submit(api,'/send',dict(id=a,message='A request'));self.assertTrue(entered.wait(5))
                    second=pool.submit(api,'/send',dict(id=b,message='B request'));release.set()
                    self.assertEqual(first.result()['id'],a);self.assertEqual(second.result()['id'],b)
                self.assertEqual(seen,['A request','B request'])
                self.assertNotIn('B request',str(api('/history',dict(id=a))))
                self.assertNotIn('A request',str(api('/history',dict(id=b))))
                with ThreadPoolExecutor(2) as pool:
                    follow=[pool.submit(api,'/send',dict(id=a,message=f'follow-up {i}')) for i in range(2)]
                    self.assertTrue(all(f.result()['id']==a for f in follow))
                history=api('/history',dict(id=a))['turns']
                self.assertEqual(len(history),6)
                self.assertEqual([t['content'] for t in history if t['role']=='user'],['A request']+seen[2:])
                with self.assertRaises(urllib.error.HTTPError) as rejected:api('/history',dict(id='../outside'))
                rejected.exception.close()
                self.assertEqual(len(api('/conversations',{})['conversations']),2)
            finally:
                release.set();servers[0].shutdown();worker.join(timeout=5)
        self.assertFalse(worker.is_alive());self.assertFalse(errors)

    def test_sidebar_switch_cannot_display_an_inflight_answer_in_another_chat(self):
        node=ROOT/'runtime/qwen/qwen-code/node/node.exe'
        if not node.exists():node=shutil.which('node')
        if not node:self.skipTest('Node unavailable for browser-script control-flow check')
        script=chat.PAGE.split('<script>',1)[1].split('</script>',1)[0]
        fixture=r'''
const vm=require('vm'),assert=require('assert');let finish;
function element(text=''){return {value:'',ownText:text,children:[],dataset:{},style:{},scrollHeight:100,
 get textContent(){return this.ownText+this.children.map(c=>c.textContent).join('')},set textContent(v){this.ownText=v;this.children=[]},
 setAttribute(){},querySelector(){return this.children.find(c=>String(c.className).startsWith('message '))},replaceChildren(...nodes){this.ownText='';this.children=nodes},append(...nodes){this.children.push(...nodes)}}}
let form=element(),input=element(),view=element(),label=element(),nav=element(),send=element(),fresh=element();
const nodes={form,textarea:input,'#history':view,'#status':label,nav,'#new':fresh,'#send':send,'#title':element(),'#connection':element(),'#theme':element(),'#timeline':element(),'#app':element(),'#sidebar-toggle':element()};
for(const id of ['error-text','error-notice','error-close','context','rollovers','started','manage','rename-title','manage-title','manage-description','manage-cancel','manage-confirm','delete-all','conversation-menu','menu-rename','menu-delete','attach','image-files','image-drafts','image-view','chat-content'])nodes['#'+id]=element();
nodes['#image-view'].querySelector=()=>element();nodes['#image-view'].addEventListener=()=>{};
nodes['#conversation-menu'].addEventListener=()=>{};
function storage(){let values=new Map();return {getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)}}
let ctx={location:{hash:'#token',pathname:'/',search:''},history:{replaceState(){}},sessionStorage:storage(),localStorage:storage(),addEventListener(){},matchMedia:()=>({matches:true,addEventListener(){}}),document:{documentElement:element(),querySelector:s=>nodes[s],createElement:()=>element(),createTextNode:t=>element(t)}};ctx.window=ctx;
ctx.fetch=async(path,options)=>{let data=JSON.parse(options.body),result;if(path==='/send')result=await new Promise(r=>finish=()=>r({id:data.id,start:0,end:1,total:1,turns:[{role:'assistant',content:'A late answer'}]}));else if(path==='/conversations')result={conversations:[{id:'A',title:'A'},{id:'B',title:'B'}]};else result={id:data.id,start:0,end:1,total:1,turns:[{role:'assistant',content:data.id+' history'}]};return {ok:true,json:async()=>result}};
ctx.setInterval=()=>0;ctx.document.addEventListener=()=>{};
vm.createContext(ctx);vm.runInContext(SCRIPT,ctx);
(async()=>{await new Promise(setImmediate);input.value='A request';let sending=form.onsubmit({preventDefault(){}});await new Promise(setImmediate);await vm.runInContext("select('B')",ctx);const before=view.textContent;assert.match(before,/B history/);finish();await sending;assert.equal(view.textContent,before);assert.equal(vm.runInContext('active',ctx),'B')})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('SCRIPT',json.dumps(script))
        result=subprocess.run([str(node),'-e',fixture],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
