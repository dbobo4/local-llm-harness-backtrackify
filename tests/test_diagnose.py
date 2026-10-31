"""CPU checks for root-cause evidence and isolated diagnostic replay; no GPU claim."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from harness import ROOT, ReasoningBuffer, Refused, State, Task, atomic, clone, files
from tests.diagnose import select_source, sse_chunks


def frame(delta,finish=None,**extra):
    return ('data: '+json.dumps(dict(id='probe',choices=[dict(index=0,delta=delta,finish_reason=finish)],**extra))+'\n\n').encode()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='diagnostic-unit-')
        self.root=Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def test_framing_preserves_unicode_text_tools_usage_and_sentinel(self):
        thoughts=['Ordinary number 1_','0','0','0','; useful reasoning, \u00e1rv\u00edzt\u0171r\u0151 text.']
        tool=frame({'tool_calls':[{'index':0,'function':{'name':'write_file','arguments':'{}'}}]})
        terminal=frame({},'tool_calls',usage={'completion_tokens':30})
        raw=b''.join(frame({'reasoning_content':part}) for part in thoughts)+tool+terminal+b'data: [DONE]\n\n'
        for size in (1,7,1000):
            buffer=ReasoningBuffer(64)
            result=b''.join(buffer.feed(raw[i:i+size]) for i in range(0,len(raw),size))+buffer.feed(b'',final=True)
            path=self.root/f'{size}.sse';path.write_bytes(result)
            text=''.join(choice['delta'].get('reasoning_content','') for chunk in sse_chunks(path) for choice in chunk['choices'])
            self.assertEqual(text,''.join(thoughts))
            self.assertIn(tool,result);self.assertIn(terminal,result)
            self.assertTrue(result.endswith(b'data: [DONE]\n\n'))

    def test_framing_does_not_hide_invalid_or_incomplete_stream(self):
        malformed=b'data: {broken\n\n'
        buffer=ReasoningBuffer(64)
        thought=frame({'reasoning_content':'incomplete reasoning'})
        result=buffer.feed(thought+malformed+b'data: {unfinished')+buffer.feed(b'',final=True)
        self.assertIn(malformed,result)
        self.assertTrue(result.endswith(b'data: {unfinished'))
        self.assertNotIn(b'[DONE]',result)

    def test_flush_before_every_tool_content_usage_and_terminal_event(self):
        thought=frame({'reasoning_content':'short thinking tail'})
        boundaries=[frame({'tool_calls':[{'index':0,'function':{'name':'edit','arguments':'{}'}}]}),
                    frame({'content':'DONE'}),frame({'role':'assistant'}),
                    frame({},usage={'completion_tokens':2}),frame({},'stop'),b'data: [DONE]\n\n',b': ping\n\n']
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                buffer=ReasoningBuffer(64)
                self.assertEqual(buffer.feed(thought),b'')
                result=buffer.feed(boundary)
                self.assertIn(b'short thinking tail',result)
                self.assertTrue(result.endswith(boundary))
                self.assertEqual(buffer.feed(b'',final=True),b'')

    def test_flush_on_response_choice_role_and_metadata_changes(self):
        initial=frame({'reasoning_content':'first'})
        changes=[dict(id='different'),dict(created=123),dict(model='different'),dict(system_fingerprint='different')]
        for fields in changes:
            buffer=ReasoningBuffer(64);buffer.feed(initial)
            data=json.loads(frame({'reasoning_content':'second'})[5:])
            data.update(fields)
            result=buffer.feed(('data: '+json.dumps(data)+'\n\n').encode())
            self.assertIn(b'first',result);self.assertNotIn(b'second',result)
            self.assertIn(b'second',buffer.feed(b'',final=True))
        for changed in (frame({'role':'assistant','reasoning_content':'second'}),
                        b'data: {"id":"probe","choices":[{"index":1,"delta":{"reasoning_content":"second"},"finish_reason":null}]}\n\n'):
            buffer=ReasoningBuffer(64);buffer.feed(initial)
            self.assertIn(b'first',buffer.feed(changed))
            self.assertIn(b'second',buffer.feed(b'',final=True))

    def test_logprobs_and_named_sse_events_are_forwarded_without_metadata_loss(self):
        payload=json.loads(frame({'reasoning_content':'metadata-bearing thought'})[5:])
        payload['choices'][0]['logprobs']={'content':[{'token':'x','logprob':-0.2}]}
        events=[('data: '+json.dumps(payload)+'\n\n').encode(),
                b'event: reasoning\nid: external-7\ndata: {"id":"probe","choices":[{"delta":{"reasoning_content":"named event"},"index":0}]}\n\n',
                frame({'reasoning_content':'timed event'},timings={'predicted_n':7})]
        for event in events:
            buffer=ReasoningBuffer(64);buffer.feed(frame({'reasoning_content':'prefix'}))
            result=buffer.feed(event)
            self.assertTrue(result.endswith(event))
            self.assertIn(b'prefix',result)
            self.assertEqual(buffer.feed(b'',final=True),b'')

    def test_crlf_byte_boundaries_and_final_tail_are_lossless(self):
        raw=(frame({'reasoning_content':'\u00e1rv\u00edz \u2014 \u6f22\u5b57 \U0001f600'})+frame({'reasoning_content':' then finish'})).replace(b'\n',b'\r\n')
        buffer=ReasoningBuffer(64)
        result=b''.join(buffer.feed(bytes([byte])) for byte in raw)+buffer.feed(b'',final=True)
        path=self.root/'unicode.sse';path.write_bytes(result)
        text=''.join(c['delta'].get('reasoning_content','') for chunk in sse_chunks(path) for c in chunk['choices'])
        self.assertEqual(text,'\u00e1rv\u00edz \u2014 \u6f22\u5b57 \U0001f600 then finish')
        self.assertNotIn(b'[DONE]',result)  # EOF cannot manufacture a successful completion.
        self.assertEqual(buffer.feed(b'',final=True),b'')

    def source_fixture(self):
        snapshot=self.root/'Backtrackify-TEST/source'
        session=snapshot/'sessions/longflow-source'
        workspace=session/'workspace';review=session/'reviews/1-1-case'
        atomic(workspace/'source.txt','Existing project content\n');clone(workspace,review)
        atomic(session/'goal.txt','Complete the local project.');atomic(session/'memory.txt','Constraints: standard library only.\n')
        State(tasks={1:Task(1,0,'ALGORITHM','source.txt','Repair the assigned behavior','FAILED',1,reason='Native loop',kind='loop')},next_id=2).save(session/'state.log')
        turn=session/'turns/1-1-case';turn.mkdir(parents=True)
        atomic(turn/'qwen.log',json.dumps({'type':'result','is_error':True,'error':{'message':'repetitive_thoughts'}})+'\n')
        user=dict(type='user',cwd=str(review),message={'parts':[{'text':'Assigned task 1: source.txt | Repair the assigned behavior\nInspect files.'}]})
        atomic(turn/'native/projects/test/chats/test.jsonl',json.dumps(user)+'\n')
        atomic(turn/'settings.json',json.dumps({'modelProviders':{'openai':[{'generationConfig':{'contextWindowSize':32768}}]}}))
        atomic(snapshot/'agents/algorithm.md','Original role prompt')
        return snapshot,session,turn

    def test_source_replay_is_read_only_and_refuses_partial_write_state(self):
        snapshot,session,turn=self.source_fixture()
        before=files(snapshot)
        source=select_source(snapshot)
        self.assertEqual(source['workspace'],session/'reviews/1-1-case')
        self.assertEqual(files(snapshot),before)
        with (turn/'qwen.log').open('a') as log:
            log.write(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'edit'}]}})+'\n')
        with self.assertRaisesRegex(Refused,'potential side effects'): select_source(snapshot)

    @unittest.skipUnless(os.name=='nt','Windows launcher validation')
    def test_diagnostic_flags_reject_unsafe_combinations_before_desktop_operations(self):
        for arguments in [('-Diagnose',),('-Gpu','-Diagnose'),('-Gpu','-Diagnose','-SourceSnapshot','unused','-Install'),('-Gpu','-LongFlow','-Diagnose','-SourceSnapshot','unused')]:
            result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'test.ps1'),*arguments],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,2,result.stdout+result.stderr)
            self.assertNotIn('DOWNLOAD',result.stdout);self.assertNotIn('SNAPSHOT | READY',result.stdout)


if __name__=='__main__': unittest.main()
