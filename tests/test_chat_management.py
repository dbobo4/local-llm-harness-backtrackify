"""Chat-only metadata, paged presentation and observational token statistics."""
import copy
import json
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chat
from harness import ROOT, Refused, atomic


def legacy_v2(path):
    atomic(path,'Backtrackify chat 2\nrollovers 1\narchived 0\nimported -\ngoal 4\nGoal\nsummary 3\nOld\nuser 2\nHi\nassistant 5\nHello\n')


class ChatManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=chat.Conversations(self.root,'unused',8192)
        self.id=self.store.create()['id']

    def seed(self,n=8,id=None):
        c=self.store.get(id or self.id);c.goal='Original immutable goal';c.summary='Preserve public API';c.rollovers=2
        turns=[dict(role='user' if i%2==0 else 'assistant',content=f'message {i}: '+('text '*50)) for i in range(n)]
        c.archived,c.turns=turns[:-4],turns[-4:];c.save();return c

    def test_legacy_is_only_structurally_recognized_for_unavailable_view_and_explicit_delete(self):
        path=self.store.path(self.id);legacy_v2(path);raw=path.read_bytes();row=self.store.listing()['conversations'][0]
        self.assertTrue(row['legacy']);self.assertTrue(row['revision']);self.assertEqual(row['error'],chat.LEGACY_MESSAGE)
        data=self.store.history(self.id,40);self.assertEqual(data['unavailable'],chat.LEGACY_MESSAGE);self.assertNotIn('turns',data)
        for operation in (lambda:self.store.get(self.id),lambda:chat.Conversation(path,'unused',8192)):
            with self.assertRaisesRegex(Refused,'format 3'):operation()
        for confirm in (False,None,1):
            with self.subTest(confirm=confirm),self.assertRaises(Refused):self.store.delete(self.id,confirm,row['revision'])
            self.assertEqual(path.read_bytes(),raw)
        self.store.delete(self.id,True,row['revision']);self.assertFalse(path.parent.exists())
        fresh=self.store.create()['id'];self.assertTrue(self.store.path(fresh).read_text().startswith('Backtrackify chat 3\n'))

    def test_mixed_legacy_bulk_deletion_preserves_source_and_unrelated_sessions(self):
        path=self.store.path(self.id);legacy_v2(path);other=self.store.create()['id']
        source=self.root/'history.txt';legacy_v2(source);outside=self.root/'sessions/task/state.log';atomic(outside,'preserve')
        rows=self.store.listing();self.assertEqual(len(rows['conversations']),2)
        self.assertEqual(self.store.delete_snapshot(True,rows['revision']),dict(deleted=2))
        self.assertTrue(source.is_file());self.assertEqual(outside.read_text(),'preserve')
        self.assertEqual(chat.Conversations(self.root,'unused',8192).listing()['conversations'],[])

    def test_legacy_delete_rejects_stale_busy_corrupt_linked_and_unexpected_storage(self):
        path=self.store.path(self.id);legacy_v2(path);raw=path.read_text();revision=self.store.revision(path)
        atomic(path,raw.replace('Hello','HELLO'))
        with self.assertRaisesRegex(Refused,'changed'):self.store.delete(self.id,True,revision)
        with self.store.changing(self.id),self.assertRaisesRegex(Refused,'busy'):self.store.delete(self.id,True,self.store.revision(path))
        for malformed in (raw[:-2],raw.replace('archived 0','archived 1'),raw.replace('imported -','imported unknown'),raw.replace('chat 2','chat 7')):
            atomic(path,malformed)
            with self.subTest(raw=malformed[:60]),self.assertRaises((Refused,ValueError)):self.store.delete(self.id,True,self.store.revision(path))
            self.assertTrue(path.is_file())
        atomic(path,raw);atomic(path.parent/'unexpected.txt','preserve')
        with self.assertRaisesRegex(Refused,'unrecognized'):self.store.delete(self.id,True,self.store.revision(path))
        (path.parent/'unexpected.txt').unlink();original=Path.is_junction
        with patch.object(Path,'is_junction',lambda p:p==path.parent or original(p)),self.assertRaisesRegex(Refused,'Linked'):self.store.delete(self.id,True,self.store.revision(path))
        self.assertEqual(path.read_text(),raw)

    def test_corrupt_legacy_blocks_bulk_before_any_valid_chat_is_removed(self):
        legacy_v2(self.store.path(self.id));other=self.store.create()['id'];raw=self.store.path(other).read_bytes()
        rows=self.store.listing();atomic(self.store.path(self.id),'Backtrackify chat 2\ncorrupted\n')
        with self.assertRaises(Refused):self.store.delete_snapshot(True,rows['revision'])
        self.assertEqual(self.store.path(other).read_bytes(),raw)

    def test_startup_image_cleanup_does_not_parse_histories_without_images(self):
        c=self.seed(6000);raw=c.path.read_bytes()
        with patch.object(self.store,'get',side_effect=AssertionError('Unnecessary full history read')):
            self.store.cleanup_images()
        self.assertEqual(c.path.read_bytes(),raw)

    def test_new_timestamp_is_real_and_stable_after_send_rename_and_restart(self):
        meta=self.store.metadata(self.id);self.assertLess(abs(meta['created_at']-time.time()*1000),5000)
        self.seed();before=self.store.path(self.id).read_bytes();stamp=self.store.path(self.id).stat().st_mtime_ns
        self.store.rename(self.id,'A different title')
        self.assertEqual(self.store.path(self.id).read_bytes(),before);self.assertEqual(self.store.path(self.id).stat().st_mtime_ns,stamp)
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',return_value='Done'):
            self.store.send(self.id,'Next question')
        restarted=chat.Conversations(self.root,'unused',8192)
        self.assertEqual(restarted.metadata(self.id),dict(created_at=meta['created_at'],title='A different title'))
        self.assertEqual(restarted.get(self.id).goal,'Original immutable goal')

    def test_old_history_without_metadata_remains_unchanged_and_time_unknown(self):
        c=self.seed();c.path.with_name('meta.txt').unlink();raw=c.path.read_bytes()
        row=self.store.listing()['conversations'][0];self.assertIsNone(row['created_at'])
        self.store.rename(self.id,'Legacy title');self.assertIsNone(self.store.metadata(self.id)['created_at'])
        self.assertEqual(c.path.read_bytes(),raw)

    def test_title_validation_and_atomic_failure_never_change_goal_or_metadata(self):
        self.seed();meta=self.store.metadata(self.id);raw=self.store.path(self.id).read_bytes()
        for title in ('',' ','x'*121,'two\nlines','\x00',None,[], 'bad\x85title','bad\u2028title','bad\u2029'):
            with self.subTest(title=title),self.assertRaises(Refused):self.store.rename(self.id,title)
        with patch('chat.atomic',side_effect=OSError('disk')),self.assertRaises(OSError):self.store.rename(self.id,'New')
        self.assertEqual(self.store.metadata(self.id),meta);self.assertEqual(self.store.path(self.id).read_bytes(),raw)
        self.store.rename(self.id,'<script>alert(1)</script>');self.assertIn('<script>',self.store.listing()['conversations'][0]['title'])

    def test_activity_uses_saved_history_not_rename_or_failed_generation(self):
        other=self.store.create()['id'];self.seed(id=other)
        now=time.time_ns();os.utime(self.store.path(self.id),ns=(now-100000000,now-100000000));os.utime(self.store.path(other),ns=(now,now))
        order=lambda:[r['id'] for r in self.store.listing()['conversations']]
        self.assertEqual(order(),[other,self.id]);self.store.rename(self.id,'Renamed');self.assertEqual(order(),[other,self.id])
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',side_effect=Refused('Incomplete')):
            with self.assertRaises(Refused):self.store.send(self.id,'Fail')
        self.assertEqual(order(),[other,self.id])
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',return_value='OK'):
            self.store.send(self.id,'Successful activity')
        self.assertEqual(order(),[self.id,other])

    def test_sidebar_reads_only_goal_prefix_without_parsing_full_histories(self):
        self.seed(10000)
        with patch.object(self.store,'get',side_effect=AssertionError('Full history parsed')):
            rows=self.store.listing()['conversations']
        self.assertEqual(rows[0]['title'],'Original immutable goal')

    def test_paging_crosses_archive_boundary_without_loss_duplicates_or_changes(self):
        c=self.seed(122);raw=c.path.read_bytes();expected=c.public()['turns'];pages=[];before=None
        while True:
            data=self.store.history(self.id,40,before);pages[0:0]=data['turns'];before=data['before']
            self.assertLessEqual(len(data['turns']),40)
            if before is None:break
        self.assertEqual(pages,expected);self.assertEqual(c.path.read_bytes(),raw)
        self.assertEqual(self.store.history(self.id),dict(id=self.id,**c.public()))
        self.assertEqual(chat.Conversations(self.root,'unused',8192).history(self.id,40),self.store.history(self.id,40))

    def test_page_indices_remain_valid_after_new_messages_and_rollover(self):
        c=self.seed(100);old=self.store.history(self.id,40);c.archived+=c.turns[:-2];c.turns=c.turns[-2:]+[dict(role='user',content='Next'),dict(role='assistant',content='Answer')];c.rollovers+=1;c.save()
        previous=self.store.history(self.id,40,old['start']);latest=self.store.history(self.id,40)
        self.assertEqual(previous['end'],old['start']);self.assertEqual(latest['total'],102)
        self.assertEqual(old['turns'],self.store.history(self.id,40,100)['turns'])

    def test_invalid_page_values_are_rejected_before_a_send_can_commit(self):
        raw=self.store.path(self.id).read_bytes()
        for limit,before in ((0,None),(101,None),(True,None),('40',None),(40,-1),(40,True),(40,100)):
            with self.subTest(limit=limit,before=before),self.assertRaises(Refused):self.store.history(self.id,limit,before)
        with patch.object(chat.Conversation,'send') as send,self.assertRaises(Refused):self.store.send(self.id,'X',limit=101)
        send.assert_not_called();self.assertEqual(self.store.path(self.id).read_bytes(),raw)

    def test_paged_send_checks_metadata_before_generation_and_does_not_reload_after_saving(self):
        raw=self.store.path(self.id).read_bytes();meta=self.store.metadata(self.id)
        with patch.object(self.store,'metadata',side_effect=OSError('cannot read')),patch.object(chat.Conversation,'send') as send:
            with self.assertRaises(OSError):self.store.send(self.id,'Request',40)
        send.assert_not_called();self.assertEqual(self.store.path(self.id).read_bytes(),raw)
        with patch.object(self.store,'metadata',side_effect=[meta,OSError('post-save read')]) as read,patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',return_value='Accepted answer'):
            page=self.store.send(self.id,'Request',40)
        self.assertEqual(read.call_count,1);self.assertEqual(page['turns'][-1]['content'],'Accepted answer')
        self.assertEqual(self.store.history(self.id)['turns'],page['turns'])

    def test_read_cache_reuses_pages_but_never_supplies_mutable_model_state(self):
        self.seed(100)
        with patch.object(self.store,'get',wraps=self.store.get) as load:
            self.store.history(self.id,40);self.store.history(self.id,40,60);self.assertEqual(load.call_count,1)
            c=self.store.get(self.id);c.summary='changed';c.save();self.store.history(self.id,40)
            self.assertEqual(load.call_count,3)
        for _ in range(6):self.store.history(self.store.create()['id'],40)
        self.assertLessEqual(len(self.store.views),4)

    def test_stats_use_native_template_and_tokenizer_for_active_context_only(self):
        c=self.seed(100);before=copy.deepcopy(c.public());calls=[]
        def native(url,body,**kwargs):
            calls.append((url,body,kwargs))
            return {'prompt':'NATIVE RENDERED'} if url.endswith('/apply-template') else {'tokens':list(range(2048))}
        with patch('chat.request',side_effect=native):
            stats=self.store.stats(self.id);self.assertEqual(self.store.stats(self.id),stats)
        self.assertEqual(len(calls),2);self.assertEqual(calls[0][1],dict(messages=c.messages(),add_generation_prompt=True))
        self.assertEqual(calls[1][1],dict(content='NATIVE RENDERED',add_special=True));self.assertEqual(calls[0][2]['timeout'],3)
        self.assertEqual(stats['context_percent'],25);self.assertEqual(stats['rollovers'],2)
        self.assertNotIn('message 0:',str(calls));self.assertIn('message 99:',str(calls));self.assertEqual(c.public(),before)

    def test_unavailable_measurement_is_neutral_cached_and_does_not_affect_history(self):
        self.seed();raw=self.store.path(self.id).read_bytes()
        for value in (OSError('Offline'),{'prompt':None},{'tokens':'invalid'}):
            self.store.measured.clear()
            with patch('chat.request',side_effect=value if isinstance(value,Exception) else None,return_value=value) as native:
                self.assertIsNone(self.store.stats(self.id)['context_percent']);self.store.stats(self.id)
                self.assertEqual(native.call_count,1)
        self.assertEqual(self.store.path(self.id).read_bytes(),raw)

    def test_stats_are_independent_and_invalidated_after_actual_rollover_and_restart(self):
        self.seed();other=self.store.create()['id']
        with patch('chat.request',side_effect=[{'prompt':'a'},{'tokens':[1]*200},{'prompt':'b'},{'tokens':[1]*400}]):
            self.assertEqual(self.store.stats(self.id)['prompt_tokens'],200);self.assertEqual(self.store.stats(other)['prompt_tokens'],400)
        with patch.object(chat.Conversation,'tokens',side_effect=[8000,100,100]),patch.object(chat.Conversation,'complete',side_effect=['Summary','Answer']):
            page=self.store.send(self.id,'Continue',40)
        self.assertEqual(page['rollovers'],3)
        restarted=chat.Conversations(self.root,'unused',8192)
        with patch('chat.request',side_effect=[{'prompt':'after'},{'tokens':[1]*300}]):
            self.assertEqual(restarted.stats(self.id)['prompt_tokens'],300)
        self.assertEqual(restarted.history(other)['rollovers'],0)

    def test_delete_requires_confirmation_and_current_revision_preserves_other_data(self):
        other=self.store.create()['id'];raw=self.store.path(other).read_bytes();revision=self.store.revision(self.store.path(self.id))
        for confirm,version in ((False,revision),(1,revision),(True,'stale'),(True,None)):
            with self.subTest(confirm=confirm,version=version),self.assertRaises(Refused):self.store.delete(self.id,confirm,version)
        self.store.delete(self.id,True,revision);self.assertFalse((self.store.root/self.id).exists());self.assertEqual(self.store.path(other).read_bytes(),raw)
        self.store.delete(other,True,self.store.revision(self.store.path(other)))
        self.assertEqual(chat.Conversations(self.root,'unused',8192).listing()['conversations'],[])

    def test_delete_refuses_unknown_files_invalid_ids_and_linked_storage(self):
        path=self.store.path(self.id);atomic(path.parent/'user-data.txt','Preserve')
        with self.assertRaisesRegex(Refused,'unrecognized'):self.store.delete(self.id,True,self.store.revision(path))
        self.assertTrue(path.exists());(path.parent/'user-data.txt').unlink()
        for value in ('../outside','legacy/../x',None,'0'*32):
            with self.subTest(id=value),self.assertRaises(Refused):self.store.delete(value,True,'x')
        for method in ('is_symlink','is_junction'):
            with patch.object(Path,method,return_value=True),self.assertRaises(Refused):self.store.delete(self.id,True,'x')

    def test_linked_metadata_is_rejected_and_failed_delete_preserves_history(self):
        path=self.store.path(self.id);original=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda p:p.name=='meta.txt' or original(p)):
            with self.assertRaisesRegex(Refused,'Linked'):self.store.rename(self.id,'Change')
            with self.assertRaisesRegex(Refused,'Linked'):self.store.delete(self.id,True,self.store.revision(path))
        self.assertTrue(path.exists())
        with patch.object(Path,'unlink',side_effect=PermissionError('busy file')):
            with self.assertRaises(PermissionError):self.store.delete(self.id,True,self.store.revision(path))
        self.assertTrue(path.exists())

    def test_new_http_routes_keep_auth_origin_host_validation_and_compatibility(self):
        # Exercise the real Handler without a socket; usable in network-disabled CI.
        outer=self;responses=[]
        class Server:
            server_port=9777
            def __init__(self,address,handler):self.handler=handler
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def call(self,path,data,headers=None):
                body=json.dumps(data).encode();h=object.__new__(self.handler);h.path=path;h.rfile=io.BytesIO(body)
                h.headers={'Host':'127.0.0.1:9777','Origin':'http://127.0.0.1:9777','X-Chat-Token':'fixture-token','Content-Length':str(len(body))}
                if headers:h.headers.update(headers)
                h.reply=lambda code,result,*_:responses.append((code,result));h.do_POST();return responses[-1]
            def serve_forever(self):
                for route in ('/history','/stats','/rename','/delete','/delete-all','/heartbeat','/reopen','/conversations','/new','/send'):
                    for header in ({'X-Chat-Token':'wrong'},{'Host':'other'},{'Origin':'null'}):
                        code,result=self.call(route,{},header);outer.assertEqual(code,403);outer.assertNotIn('fixture-token',str(result))
                code,result=self.call('/new',{});outer.assertEqual(code,200);id=result['id']
                code,result=self.call('/send',dict(id=id,message='Hi',limit=40));outer.assertEqual(code,200);outer.assertEqual(result['total'],2)
                outer.assertEqual(self.call('/history',dict(id=id))[1]['turns'],result['turns'])
                outer.assertEqual(self.call('/stats',dict(id=id))[1]['context_percent'],25)
                outer.assertEqual(self.call('/rename',dict(id=id,title='Display only'))[0],200)
                outer.assertEqual(self.call('/delete',dict(id=id,confirm=False,revision=result['revision']))[0],400)
                outer.assertEqual(self.call('/delete',dict(id=id,confirm=True,revision=result['revision']))[0],200)
                outer.assertEqual(self.call('/conversations',{})[1]['conversations'],[])
        with patch('chat.ROOT',self.root),patch('chat.ChatServer',Server),patch('chat.port_free'),patch('chat.threading.Timer'),patch('chat.status'),patch('chat.secrets.token_urlsafe',return_value='fixture-token'),patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',return_value='Hello'),patch('chat.request',side_effect=[{'prompt':'native'},{'tokens':[1]*2048}]):
            self.assertEqual(chat.serve('unused',8192,9777),0)

    def test_delete_preserves_original_legacy_source_without_reimport(self):
        legacy=self.root/'history.txt';old=chat.Conversation(legacy,'unused',8192);old.goal='Old';old.turns=[dict(role='user',content='Hi'),dict(role='assistant',content='Hello')];old.save();raw=legacy.read_bytes()
        store=chat.Conversations(self.root,'unused',8192);store.delete(self.id,True,store.revision(store.path(self.id)))
        restarted=chat.Conversations(self.root,'unused',8192)
        self.assertNotIn('legacy',[r['id'] for r in restarted.listing()['conversations']]);self.assertEqual(legacy.read_bytes(),raw)
        atomic(legacy,legacy.read_text().replace('Hello','HELLO'))
        self.assertEqual(chat.Conversations(self.root,'unused',8192).listing()['conversations'],[])

    def test_delete_and_rename_refuse_inflight_conversation_without_stopping_request(self):
        entered,release=threading.Event(),threading.Event();errors=[]
        def answer(*_):entered.set();self.assertTrue(release.wait(5));return 'Answer'
        def send():
            try:self.store.send(self.id,'Request')
            except BaseException as e:errors.append(e)
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',side_effect=answer):
            thread=threading.Thread(target=send);thread.start();self.assertTrue(entered.wait(5))
            try:
                for operation in (lambda:self.store.rename(self.id,'Title'),lambda:self.store.delete(self.id,True,self.store.revision(self.store.path(self.id)))):
                    with self.assertRaisesRegex(Refused,'busy'):operation()
            finally:release.set();thread.join(5)
        self.assertFalse(errors);self.assertEqual(len(self.store.history(self.id)['turns']),2)

    def test_long_history_response_and_cache_work_are_bounded_by_page_size(self):
        measurements=[]
        for count in (100,1000,10000):
            c=self.seed(count);raw=c.path.read_bytes();start=time.perf_counter();page=self.store.history(self.id,40);elapsed=time.perf_counter()-start
            full=self.store.history(self.id);small=len(json.dumps(page).encode());large=len(json.dumps(full).encode())
            self.assertEqual(len(page['turns']),40);self.assertEqual(page['turns'][-1],full['turns'][-1]);self.assertLess(small,large)
            self.assertEqual(c.path.read_bytes(),raw)
            measurements.append(dict(messages=count,page_messages=40,page_bytes=small,full_bytes=large,cold_page_ms=round(elapsed*1000,3)))
        self.assertLess(measurements[-1]['page_bytes'],measurements[0]['page_bytes']*1.1)
        self.assertLess(measurements[-1]['page_bytes'],measurements[-1]['full_bytes']/100)
        atomic(ROOT/'logs/chat-management-audit/performance.json',json.dumps(measurements,indent=2)+'\n')


if __name__=='__main__':unittest.main()
