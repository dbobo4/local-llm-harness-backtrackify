"""Bounded image ownership, v3 persistence and native request regressions; no GPU."""
import base64
import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chat
import interactive
import vision
from harness import ROOT, Refused, atomic


def png(color='red',size=(32,24),format='PNG'):
    Image,_=vision.pillow();image=Image.new('RGB',size,color);out=BytesIO()
    image.save(out,format=format);return out.getvalue()


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=chat.Conversations(self.root/'chat','unused',8192)
        self.id=self.store.create()['id'];self.owner=self.store.path(self.id).parent

    def image(self,color='red'):
        return self.store.upload(self.id,png(color),'image/png')['image']

    def test_profile_projectors_are_exact_pins_and_gpu_only(self):
        self.assertEqual(vision.PROJECTORS['MID'][1],'4ca720788d1e01f1bff70c033e0d0028fd02e502')
        self.assertEqual(vision.PROJECTORS['MID'][3],'cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e')
        self.assertEqual(vision.PROJECTORS['LOW'][1],'3885219b6810b007914f3a7950a8d1b469d598a5')
        self.assertEqual(vision.PROJECTORS['LOW'][3],'f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f')
        for profile in ('MID','HIGH','EXTRA HIGH'):self.assertEqual(vision.projector(profile,self.root),vision.projector('MID',self.root))
        source=(ROOT/'harness.py').read_text(encoding='utf-8')
        self.assertIn("'--mmproj-offload','--mmproj-device','CUDA0'",source)
        self.assertNotIn('--no-mmproj-offload',source)

    def test_install_vision_uses_pinned_downloads_and_owned_decoder_only(self):
        import install
        target=vision.projector('LOW',self.root);wheel=self.root/'runtime/vision'/('pillow-'+vision.PILLOW_VERSION+'-cp314-cp314-win_amd64.whl')
        wheel.parent.mkdir(parents=True);wheel.write_bytes(b'fixture')
        with patch('install.ROOT',self.root),patch('install.sys.version_info') as version,patch('harness.download') as download,patch('vision.check_projector') as check,patch('vision.pillow'),patch('install.unpack') as unpack:
            version.major=3;version.minor=14;install.install_vision(Path('Qwen3.5-9B-Q4_K_M.gguf'))
        url,path,digest=download.call_args_list[0].args
        self.assertIn(vision.PROJECTORS['LOW'][1],url);self.assertEqual(path,target);self.assertEqual(digest,vision.PROJECTORS['LOW'][3])
        self.assertEqual(download.call_args_list[1].args,('',wheel,vision.PILLOW_HASHES['314']))
        check.assert_called_once();unpack.assert_called_once_with(wheel,self.root/'runtime/vision/python')

    def test_space_preflight_accounts_for_required_projector_without_profile_fallback(self):
        import install
        from types import SimpleNamespace
        model=self.root/'models/Qwen3.8-27B-UD-Q3_K_XL.gguf';atomic(model,'existing')
        with patch('install.remaining_space',return_value=0):
            with self.assertRaisesRegex(Refused,'pinned profile unchanged'):install.disk_check(self.root,model,'unused',usage=lambda _:SimpleNamespace(free=vision.PROJECTORS['MID'][4]),root=self.root)
            install.disk_check(self.root,model,'unused',usage=lambda _:SimpleNamespace(free=vision.PROJECTORS['MID'][4]+128*1024**2),root=self.root)

    def test_missing_corrupt_and_unknown_projector_fail_without_replacement(self):
        path=self.root/'projector.gguf';model='Qwen3.8-27B-UD-Q3_K_XL.gguf'
        with self.assertRaisesRegex(Refused,'missing'):vision.check_projector(model,path)
        path.write_bytes(b'wrong projector')
        with self.assertRaisesRegex(Refused,'checksum/model mismatch'):vision.check_projector(model,path)
        self.assertEqual(path.read_bytes(),b'wrong projector')
        with self.assertRaisesRegex(Refused,'No pinned'):vision.check_projector('other.gguf',path)

    def test_required_vision_startup_fails_closed_before_launch_or_on_missing_modality(self):
        from harness import Server
        from unittest.mock import MagicMock
        model='Qwen3.8-27B-UD-Q3_K_XL.gguf';path=self.root/'missing.gguf'
        with patch('harness.port_free'),patch('harness.windows_servers',return_value=[]),patch('harness.start_process') as spawn:
            with self.assertRaisesRegex(Refused,'missing'):Server('unused',model,mmproj=path).__enter__()
            spawn.assert_not_called()
        process=MagicMock();process.poll.return_value=None
        with patch('harness.port_free'),patch('harness.windows_servers',return_value=[]),patch('vision.check_projector'),patch('vision.pillow'),patch('harness.start_process',return_value=process),patch('harness.stop_process') as stop,patch('harness.request',return_value=dict(status='ok',modalities=dict(vision=False))):
            with self.assertRaisesRegex(Refused,'without required vision'):Server('unused',model,log=self.root/'server.log',mmproj=path).__enter__()
            stop.assert_called_once_with(process)

    def test_gate_rejects_worker_images_and_enforces_exact_multimodal_headroom(self):
        from harness import InferenceGate,request
        from tests.test_native import Endpoint
        import urllib.error
        id=self.image();body=dict(messages=[dict(role='user',content=vision.mixed(self.owner,'Look',[id]))],max_tokens=11264,thinking_budget_tokens=8192,stream=False)
        for enabled,count in ((False,150),(True,22000),(True,-1)):
            with self.subTest(enabled=enabled,count=count),Endpoint(False) as endpoint:
                endpoint.token_count=count
                with InferenceGate('http://127.0.0.1:'+str(endpoint.http.server_port),32768,8192,11264,self.root/'gate.log',vision=enabled) as gate:
                    with self.assertRaises(urllib.error.HTTPError) as error:request(gate.url+'/v1/chat/completions',body)
                    error.exception.close();self.assertFalse(endpoint.requests)
                self.assertEqual(bool(endpoint.image_counts),enabled)

    def test_png_and_jpeg_decode_and_strip_private_metadata(self):
        Image,_=vision.pillow()
        from PIL.PngImagePlugin import PngInfo
        out=BytesIO();meta=PngInfo();meta.add_text('private','EXIF SECRET')
        Image.new('RGB',(32,24),'blue').save(out,format='PNG',pnginfo=meta)
        id=vision.store(self.owner,out.getvalue(),'image/png');raw,mime=vision.read(self.owner,id)
        self.assertNotIn(b'EXIF SECRET',raw);self.assertEqual(mime,'image/png')
        jpeg=vision.store(self.owner,png(format='JPEG'),'image/jpeg')
        self.assertTrue(jpeg.endswith('.jpg'));self.assertEqual(vision.store(self.owner,raw,mime),id)

    def test_unsafe_images_and_declared_mime_are_rejected(self):
        for raw,mime in ((b'broken','image/png'),(png(),'image/jpeg'),(png(),'image/webp'),(b'','image/png'),(b'x'*(vision.MAX_UPLOAD+1),'image/png')):
            with self.subTest(mime=mime,size=len(raw)),self.assertRaises(Refused):vision.store(self.owner,raw,mime)
        with self.assertRaisesRegex(Refused,'megapixels'):vision.normalize(png(size=(2049,2049)),'image/png')
        with patch('vision.MAX_IMAGE',1),self.assertRaisesRegex(Refused,'Normalized'):vision.normalize(png(),'image/png')
        Image,_=vision.pillow();out=BytesIO()
        Image.new('RGB',(3,3),'red').save(out,format='PNG',save_all=True,append_images=[Image.new('RGB',(3,3),'blue')])
        with self.assertRaisesRegex(Refused,'animated'):vision.normalize(out.getvalue(),'image/png')

    def test_owned_ids_reject_paths_links_corruption_and_cross_conversation(self):
        id=self.image();other=self.store.create()['id']
        with self.assertRaisesRegex(Refused,'missing'):self.store.image(other,id)
        for value in ('../history.txt',str(self.owner/'history.txt'),'A'*64+'.png',None):
            with self.subTest(value=value),self.assertRaises(Refused):vision.read(self.owner,value)
        with patch.object(Path,'is_junction',return_value=True),self.assertRaisesRegex(Refused,'Linked'):vision.read(self.owner,id)
        (self.owner/'images'/id).write_bytes(b'changed')
        with self.assertRaisesRegex(Refused,'checksum'):vision.read(self.owner,id)

    def test_two_distinct_images_and_no_remote_urls_or_invalid_encoding(self):
        a,b=self.image(),self.image('blue');body=dict(messages=[dict(role='user',content=vision.mixed(self.owner,'Inspect',[a,b]))])
        self.assertEqual(vision.checked_body(body),body)
        for ids in ([a,a],[a,b,a],[{}],None):
            with self.subTest(ids=ids),self.assertRaises(Refused):vision.references(ids)
        for url in ('https://example.com/a.png','file:///a.png','data:image/png;base64,broken'):
            bad=copy.deepcopy(body);bad['messages'][0]['content'][1]['image_url']['url']=url
            with self.subTest(url=url),self.assertRaises(Refused):vision.checked_body(bad)

    def test_v3_text_and_image_references_survive_restart_and_archive_pagination(self):
        id=self.image();c=self.store.get(self.id);c.goal='Original';c.summary='Findings';c.rollovers=2
        c.archived=[dict(role='user',content='Look',images=[id]),dict(role='assistant',content='Red')]
        c.turns=[dict(role='user',content='Continue'),dict(role='assistant',content='Done')];c.save()
        raw=c.path.read_bytes();self.assertTrue(raw.startswith(b'Backtrackify chat 3\n'));self.assertNotIn(b'base64',raw)
        restored=chat.Conversations(self.root/'chat','unused',8192)
        self.assertEqual(restored.history(self.id),self.store.history(self.id))
        self.assertEqual(restored.history(self.id,2,2)['turns'][0]['images'],[id])
        self.assertFalse(vision.has_images(c.messages()));self.assertNotIn(id,str(c.messages()))
        text=restored.create()['id'];self.assertTrue(restored.path(text).read_text().startswith('Backtrackify chat 3'))

    def test_invalid_image_reference_trailer_is_rejected_without_modifying_history(self):
        c=self.store.get(self.id);c.turns=[dict(role='user',content='Hi'),dict(role='assistant',content='Hello')];c.save()
        before=c.path.read_text()
        for trailer in ('images 1\n1 '+'a'*64+'.png\n','images 1\n0 ../escape\n','images 1\n9 '+'a'*64+'.png\n','images 0\nextra\n'):
            raw=before.replace('images 0\n',trailer);atomic(c.path,raw)
            with self.subTest(trailer=trailer),self.assertRaises(Refused):self.store.get(self.id)
            self.assertEqual(c.path.read_text(),raw)

    def test_only_explicit_pending_images_are_sent_and_reuse_is_explicit(self):
        id=self.image();sent=[]
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',side_effect=lambda messages,*_:sent.append(messages) or 'Red'):
            self.store.send(self.id,'Inspect',images=[id]);self.store.send(self.id,'Continue');self.store.send(self.id,'Inspect again',images=[id])
        self.assertTrue(vision.has_images(sent[0]));self.assertFalse(vision.has_images(sent[1]));self.assertTrue(vision.has_images(sent[2]))
        self.assertEqual(sum(vision.has_images([m]) for m in sent[2]),1)
        self.assertEqual(self.store.get(self.id).goal,'Inspect');self.assertEqual(len(self.store.history(self.id)['turns']),6)

    def test_multimodal_count_uses_identical_completion_body_and_no_generation(self):
        id=self.image();c=self.store.get(self.id);calls=[]
        def api(url,body,**kwargs):
            calls.append((url,copy.deepcopy(body)))
            if url.endswith('input_tokens'):return dict(input_tokens=200)
            return dict(choices=[dict(finish_reason='stop',message=dict(content='Red'))])
        with patch('chat.request',side_effect=api):c.send('Inspect',[id])
        self.assertEqual(len(calls),2);self.assertEqual(calls[0][1],calls[1][1])
        for count in (None,True,-1,'200'):
            with patch('chat.request',return_value=dict(input_tokens=count)),self.assertRaises(Refused):c.tokens(calls[0][1]['messages'])

    def test_multimodal_overflow_failed_generation_and_failed_save_do_not_commit(self):
        id=self.image();c=self.store.get(self.id);raw=c.path.read_bytes()
        with patch.object(c,'tokens',return_value=8000),self.assertRaises(Refused):c.send('Huge',[id])
        for error in (Refused('incomplete'),KeyboardInterrupt()):
            with patch.object(c,'tokens',return_value=100),patch.object(c,'complete',side_effect=error),self.assertRaises(type(error)):c.send('Look',[id])
        with patch.object(c,'tokens',return_value=100),patch.object(c,'complete',return_value='Answer'),patch('chat.atomic',side_effect=OSError('disk full')),self.assertRaises(OSError):c.send('Look',[id])
        self.assertEqual(c.path.read_bytes(),raw);self.assertEqual(c.turns,[])

    def test_rollover_archives_refs_without_replaying_pixels_or_changing_algorithm(self):
        id=self.image();c=self.store.get(self.id);c.goal='Original'
        c.turns=[dict(role='user',content='Look',images=[id]),dict(role='assistant',content='Red'),dict(role='user',content='Next'),dict(role='assistant',content='Okay')];c.save()
        with patch.object(c,'tokens',side_effect=[8000,100,100]),patch.object(c,'complete',side_effect=['Red rectangle','Answer']) as complete:c.send('Continue')
        self.assertEqual(c.rollovers,1);self.assertEqual(c.archived[0]['images'],[id])
        for call in complete.call_args_list:self.assertFalse(vision.has_images(call.args[0]))
        self.assertEqual(chat.Conversation(c.path,'unused',8192).public(),c.public())

    def test_failed_message_prunes_uncommitted_uploads_but_preserves_used_images(self):
        used=self.image()
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',return_value='Red'):self.store.send(self.id,'Look',images=[used])
        orphan=self.image('blue')
        with patch.object(chat.Conversation,'tokens',return_value=100),patch.object(chat.Conversation,'complete',side_effect=Refused('bad')),self.assertRaises(Refused):self.store.send(self.id,'Again',images=[orphan])
        self.assertTrue(vision.image_path(self.owner,used).exists());self.assertFalse((self.owner/'images'/orphan).exists())
        orphan=self.image('green');self.store.cleanup_images();self.assertFalse((self.owner/'images'/orphan).exists())
        interrupted=self.owner/'images'/(orphan+'.'+'a'*32+'.part');interrupted.write_bytes(b'partial')
        self.store.cleanup_images();self.assertFalse(interrupted.exists());self.assertTrue(vision.image_path(self.owner,used).exists())

    def test_delete_and_bulk_delete_remove_only_verified_owned_images(self):
        self.image();other=self.store.create()['id'];outside=self.root/'original.png';outside.write_bytes(png())
        self.store.delete(self.id,True,self.store.revision(self.store.path(self.id)))
        self.assertFalse(self.owner.exists());self.assertTrue(outside.exists())
        vision.store(self.store.path(other).parent,png(),'image/png');rows=self.store.listing()['conversations']
        self.assertEqual(self.store.delete_all(True,[dict(id=r['id'],revision=r['revision']) for r in rows]),dict(deleted=1))
        self.assertTrue(outside.exists())

    def test_unknown_or_linked_image_files_block_all_deletion_before_mutation(self):
        self.image();atomic(self.owner/'images/important.txt','preserve');raw=self.store.path(self.id).read_bytes()
        with self.assertRaisesRegex(Refused,'Unrecognized'):self.store.delete(self.id,True,self.store.revision(self.store.path(self.id)))
        self.assertEqual(self.store.path(self.id).read_bytes(),raw)
        (self.owner/'images/important.txt').unlink()
        with patch.object(Path,'is_symlink',return_value=True),self.assertRaises(Refused):vision.owned_files(self.owner)

    def payload(self):
        transcript=self.root/'native.jsonl';image=dict(inlineData=dict(mimeType='image/png',data=base64.b64encode(png()).decode()))
        row=dict(type='user',provenance='real_user',uuid='user-1',message=dict(parts=[dict(text='Fix this screenshot'),image]))
        atomic(transcript,json.dumps(row)+'\n')
        return dict(transcript_path=str(transcript),session_id='native-1',prompt='Fix this screenshot',previous_user=None)

    def test_current_authenticated_native_request_snapshots_actual_pixels_and_restart_catalog(self):
        payload=self.payload();session=self.root/'task-A';session.mkdir()
        interactive.snapshot_images(payload,session);ids=vision.task_images(session)
        self.assertEqual(len(ids),1);self.assertEqual(vision.read(session,ids[0])[1],'image/png')
        self.assertIn(str(vision.image_path(session,ids[0])),vision.task_image_context(session))
        before=(session/'task-images.txt').read_bytes();interactive.snapshot_images(payload,session)
        self.assertEqual((session/'task-images.txt').read_bytes(),before)
        other=self.root/'task-B';other.mkdir();self.assertEqual(vision.task_images(other),[])
        (other/'task-images.txt').write_bytes(before)
        with self.assertRaisesRegex(Refused,'manifest'):vision.task_images(other)
        self.assertFalse((session/'goal.txt').exists());self.assertFalse((session/'state.log').exists())

    def test_stale_forged_cross_request_and_remote_native_images_are_refused(self):
        payload=self.payload();session=self.root/'task';session.mkdir()
        for changes in (dict(prompt='invented'),dict(previous_user='old-user')):
            with self.subTest(changes=changes),self.assertRaises(Refused):interactive.snapshot_images(dict(payload,**changes),session)
        transcript=Path(payload['transcript_path']);row=json.loads(transcript.read_text())
        for part in (dict(fileData=dict(fileUri='file:///private.png')),dict(inlineData=dict(mimeType='image/png',data='invalid'))):
            row['message']['parts']=[dict(text=payload['prompt']),part];atomic(transcript,json.dumps(row)+'\n')
            with self.subTest(part=part),self.assertRaises(Refused):interactive.snapshot_images(payload,session)
        self.assertFalse((session/'task-images.txt').exists())


if __name__=='__main__':unittest.main()
