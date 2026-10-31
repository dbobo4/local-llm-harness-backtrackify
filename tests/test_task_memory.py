"""Task-bound instruction snapshots and explicit planner notes; no GPU calls."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import harness
from harness import (Harness, Qwen, Refused, State, Task, atomic, parse_plan,
                     task_notes_context, task_notes_path, TASK_NOTES_LIMIT, PLAN_SCHEMA, REPORT_SCHEMA)
from interactive import capture, work
from tests.test_interactive import Setup
from tests.test_native import CLI, Endpoint


class InteractiveMemoryTests(Setup):
    def setUp(self):
        super().setUp()
        self.app=self.root/'application';self.app.mkdir();self.shared=self.app/'memory.txt'
        self.root_patch=patch('interactive.ROOT',self.app);self.root_patch.start();self.addCleanup(self.root_patch.stop)
        atomic(self.shared,'Shared instruction: preserve the public API.\n')
        atomic(self.project/'memory.txt','UNRELATED_PROJECT_INSTRUCTIONS')

    def run_task(self,session,agent=None):
        if agent is None:agent,_=self.agent()
        with patch('visualize.open_view'):return work(self.config,session,agent)

    def test_new_task_combines_root_and_conversation_for_all_roles_once(self):
        self.request('Earlier user constraint: use UTF-8.')
        session=self.request('Build the feature.');base,_=self.agent();prompts=[]
        class Agent:
            def call(_,role,prompt,*args,**kwargs):
                prompts.append((role,prompt));return base.call(role,prompt,*args,**kwargs)
        original=self.shared.read_bytes();self.assertEqual(self.run_task(session,Agent()),0)
        memory=(session/'memory.txt').read_text(encoding='utf-8')
        self.assertIn('Shared ROOT instructions:',memory);self.assertIn('Conversation evidence;',memory)
        self.assertIn('Earlier user constraint: use UTF-8.',memory)
        self.assertNotIn('UNRELATED_PROJECT_INSTRUCTIONS',memory)
        self.assertEqual((session/'goal.txt').read_text(),'Build the feature.\n')
        self.assertEqual({role for role,_ in prompts},{'PROMPT','ALGORITHM','TEST'})
        for role,prompt in prompts:
            self.assertEqual(prompt.count('Shared instruction: preserve the public API.'),1,role)
        self.assertEqual(self.shared.read_bytes(),original)

    def test_later_task_takes_a_new_snapshot_without_changing_the_first(self):
        first=self.request();self.assertEqual(self.run_task(first),0);saved=(first/'memory.txt').read_bytes()
        atomic(self.shared,'Second shared instruction')
        second=self.request('Build another feature.');self.assertEqual(self.run_task(second),0)
        self.assertEqual((first/'memory.txt').read_bytes(),saved)
        self.assertIn('Second shared instruction',(second/'memory.txt').read_text())
        self.assertNotIn('Shared instruction: preserve',(second/'memory.txt').read_text())

    def test_explicit_resume_keeps_its_goal_snapshot_and_completed_children(self):
        session=self.request();base,_=self.agent()
        class Interrupted:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                if kwargs.get('task_id')==1:raise KeyboardInterrupt
                return base.call(role,prompt,workspace,structured,**kwargs)
        with self.assertRaises(KeyboardInterrupt):self.run_task(session,Interrupted())
        saved=(session/'memory.txt').read_bytes();before=State.load(session/'state.log')
        self.shared.write_bytes(b'\xff')  # Resume must not even decode a later ROOT edit.
        self.request('Resume '+str(session))
        class Resume:
            def call(_,role,prompt,workspace,structured=False,task_id=None,**kwargs):
                if role=='PROMPT':return {'operations':parse_plan('EDIT | 1 | Integrate feature\nFINISH')}
                return dict(task_id=task_id,status='DONE',summary='Remaining scope checked')
        self.assertEqual(self.run_task(session,Resume()),0)
        self.assertEqual((session/'memory.txt').read_bytes(),saved)
        self.assertEqual((session/'goal.txt').read_text(),'Build the feature.\n')
        after=State.load(session/'state.log')
        for id in (2,3):self.assertEqual(after.tasks[id],before.tasks[id])

    def test_separate_outer_conversation_does_not_import_another_conversation(self):
        first=self.request('PRIVATE_OUTER_A');self.assertEqual(self.run_task(first),0)
        home=self.root/'other-outer';home.mkdir();journal=home/'native/projects/cpu/chats/current.jsonl';atomic(journal,'')
        config=dict(self.config,home=str(home));text='PRIVATE_OUTER_B'
        capture(config,dict(hook_event_name='UserPromptSubmit',cwd=str(self.project),session_id='other-native-session',transcript_path=str(journal),prompt=text))
        atomic(journal,json.dumps(dict(uuid=uuid.uuid4().hex,type='user',provenance='real_user',message=dict(parts=[dict(text=text)])))+'\n')
        payload=json.loads((home/'prompt.json').read_text());session=home/'tasks'/payload['request'];session.mkdir(parents=True)
        atomic(session/'input.json',json.dumps(payload));agent,_=self.agent()
        with patch('visualize.open_view'):self.assertEqual(work(config,session,agent),0)
        self.assertNotIn('PRIVATE_OUTER_A',(session/'memory.txt').read_text())
        self.assertEqual((session/'goal.txt').read_text(),'PRIVATE_OUTER_B\n')

    def test_plain_user_prompt_capture_does_not_read_root_or_create_tasks(self):
        self.shared.write_bytes(b'\xff')
        capture(self.config,dict(hook_event_name='UserPromptSubmit',cwd=str(self.project),session_id='chat-only',transcript_path=str(self.chat),prompt='Explain this function.'))
        self.assertFalse((self.home/'tasks').exists());self.assertFalse((self.project/'goal.txt').exists())

    def test_empty_or_absent_root_is_explicit_and_does_not_invent_instructions(self):
        for present in (True,False):
            if present:atomic(self.shared,'')
            else:self.shared.unlink()
            session=self.request();self.assertEqual(self.run_task(session),0)
            text=(session/'memory.txt').read_text()
            self.assertIn('(none)',text);self.assertIn('Conversation evidence;',text)
            self.assertEqual('memory.txt absent' in text,not present)

    def test_invalid_root_text_is_rejected_before_workspace_or_state_creation(self):
        for raw in (b'\xff',b'No NUL\x00',b'x'*20001):
            self.shared.write_bytes(raw);session=self.request()
            with self.subTest(raw=raw[:12]),self.assertRaises(Refused):self.run_task(session)
            self.assertFalse((session/'state.log').exists());self.assertFalse((session/'workspace').exists())
            self.assertEqual(self.shared.read_bytes(),raw)

    def test_unreadable_root_is_not_replaced_with_a_silent_default(self):
        session=self.request();original,exists=Path.open,Path.exists
        def open(path,*args,**kwargs):
            if path==self.shared:raise PermissionError('fixture denied')
            return original(path,*args,**kwargs)
        # Python 3.14 exists() can hide stat permission failures as False.
        with patch.object(Path,'exists',lambda p:False if p==self.shared else exists(p)),patch.object(Path,'open',open),self.assertRaisesRegex(Refused,'readable UTF-8'):self.run_task(session)
        self.assertFalse((session/'state.log').exists())

    def test_root_and_conversation_share_the_existing_total_size_bound(self):
        atomic(self.shared,'x'*19000);session=self.request('g'*2000)
        with self.assertRaisesRegex(Refused,'20000-character'):self.run_task(session)
        self.assertFalse((session/'memory.txt').exists())


class NotesSetup(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir=harness.ROOT/'sessions',prefix='task memory ')
        self.addCleanup(temp.cleanup);self.root=Path(temp.name);self.session=self.root/'task-a'

    def harness(self,agent,session=None):
        session=session or self.session;(session/'workspace').mkdir(parents=True,exist_ok=True)
        atomic(session/'goal.txt','Original authoritative goal.\n');atomic(session/'memory.txt','User-owned constraints.\n')
        State(started=1).save(session/'state.log')
        return Harness(session,agent)

    def plan(self,notes=None):
        value={'operations':parse_plan('ADD | 0 | ALGORITHM | | Implement assigned scope\nFINISH')}
        if notes is not None:value['task_notes']=notes
        return value

    def agent(self,proposals):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                calls.append((structured,prompt));return proposals.pop(0)
        return Agent(),calls


class TaskNotesTests(NotesSetup):
    def test_explicit_create_replace_and_clear_are_seen_once_by_later_roles(self):
        first='Keep signatures; trailing newlines are the observed defect.'
        agent,calls=self.agent([self.plan(first),{'operations':parse_plan('FINISH'),'task_notes':'Updated observation'},
                               {'operations':parse_plan('FINISH'),'task_notes':''}])
        h=self.harness(agent);goal=(self.session/'goal.txt').read_bytes();memory=(self.session/'memory.txt').read_bytes()
        h.plan();path=self.session/'task-notes.txt';self.assertEqual(path.read_text(),first)
        for context in (h.plan_context(),h.context(h.state.tasks[1],self.session/'review')):
            self.assertEqual(context.count(first),1);self.assertIn('never override the original goal',context)
        h.plan();self.assertIn(first,calls[1][1]);self.assertEqual(path.read_text(),'Updated observation')
        self.assertNotIn(first,h.plan_context());h.plan();self.assertEqual(path.read_bytes(),b'')
        self.assertNotIn('Task notes (',h.plan_context())
        self.assertEqual((self.session/'goal.txt').read_bytes(),goal);self.assertEqual((self.session/'memory.txt').read_bytes(),memory)

    def test_note_metadata_does_not_change_the_plan_graph_or_state_format(self):
        a,_=self.agent([self.plan('A useful observation')]);b,_=self.agent([self.plan()])
        first=self.harness(a);second=self.harness(b,self.root/'control')
        with patch('harness.time.strftime',return_value='fixed'):first.plan();second.plan()
        self.assertEqual(first.state.render(),second.state.render())
        self.assertEqual((first.session/'state.log').read_bytes(),(second.session/'state.log').read_bytes())
        self.assertTrue((first.session/'state.log').read_text().startswith('Backtrackify state 4\n'))

    def test_exact_unicode_note_limit_is_retained_without_byte_truncation(self):
        text='\U0001f642'*TASK_NOTES_LIMIT;agent,_=self.agent([self.plan(text)])
        h=self.harness(agent);h.plan()
        self.assertEqual((self.session/'task-notes.txt').read_text(encoding='utf-8'),text)
        self.assertEqual(h.plan_context().count(text),1)

    def test_omitted_note_on_a_later_accepted_plan_does_not_rewrite_it(self):
        agent,_=self.agent([self.plan('Retain this observation'),{'operations':parse_plan('FINISH')}])
        h=self.harness(agent);h.plan();before=(self.session/'task-notes.txt').read_bytes()
        with patch('harness.atomic',wraps=harness.atomic) as write:h.plan()
        self.assertNotIn('task-notes.txt',[call.args[0].name for call in write.call_args_list])
        self.assertEqual((self.session/'task-notes.txt').read_bytes(),before)

    def test_no_notes_has_no_extra_model_call_or_file_and_restart_preserves_saved_notes(self):
        agent,calls=self.agent([self.plan()]);h=self.harness(agent);h.plan()
        self.assertEqual(len(calls),1);self.assertFalse((self.session/'task-notes.txt').exists())
        atomic(self.session/'task-notes.txt','Durable note');before=h.state.render()
        restored=Harness(self.session,None)
        self.assertEqual(restored.state.render(),before);self.assertIn('Durable note',restored.plan_context())

    def test_notes_are_not_read_from_other_sessions_or_project_files(self):
        agent,_=self.agent([self.plan('ONLY_TASK_A')]);first=self.harness(agent);first.plan()
        second=self.harness(None,self.root/'task-b');atomic(second.workspace/'task-notes.txt','PROJECT_FILE_NOT_SESSION_MEMORY')
        self.assertNotIn('ONLY_TASK_A',second.plan_context());self.assertNotIn('PROJECT_FILE_NOT_SESSION_MEMORY',second.plan_context())
        self.assertFalse((second.session/'task-notes.txt').exists())

    def test_rejected_native_plan_cannot_publish_notes_and_text_correction_retains_old(self):
        bad={'operations':parse_plan('ADD | 0 | TEST | | Invalid self need | 1\nFINISH'),'task_notes':'MUST_NOT_COMMIT'}
        agent,calls=self.agent([bad,'ADD | 0 | TEST | | Corrected check\nFINISH']);h=self.harness(agent)
        atomic(self.session/'task-notes.txt','Previous valid note');h.plan()
        self.assertEqual([mode for mode,_ in calls],[True,False]);self.assertEqual((self.session/'task-notes.txt').read_text(),'Previous valid note')
        self.assertEqual(len(h.state.tasks),1);self.assertEqual(h.state.tasks[1].summary,'Corrected check')

    def test_repeated_invalid_proposals_remain_bounded_and_preserve_notes(self):
        bad={'operations':parse_plan('ADD | 0 | TEST | | Bad | 1\nFINISH'),'task_notes':'Discard this'}
        agent,calls=self.agent([bad,'Explanatory prose',bad]);h=self.harness(agent);atomic(self.session/'task-notes.txt','Keep')
        self.assertEqual(h.run(),2);self.assertEqual([mode for mode,_ in calls],[True,False,True])
        self.assertEqual(h.state.phase,'blocked');self.assertFalse(h.state.tasks)
        self.assertEqual((self.session/'task-notes.txt').read_text(),'Keep')

    def test_oversized_invalid_encoding_and_controls_never_commit_a_plan(self):
        for value in ('x'*(TASK_NOTES_LIMIT+1),'\ud800','bad\x00text',{'path':'../../memory.txt','text':'No'}):
            agent,calls=self.agent([self.plan(value),'prose',self.plan(value)])
            h=self.harness(agent,self.root/uuid.uuid4().hex)
            with self.subTest(value=repr(value)[:30]),self.assertRaises(Refused):h.plan()
            self.assertFalse(State.load(h.path).tasks);self.assertFalse((h.session/'task-notes.txt').exists())
            self.assertEqual(len(calls),3)

    def test_unknown_destination_arguments_cannot_write_root_goal_or_another_session(self):
        protected=[self.root/'memory.txt',self.root/'task-b/task-notes.txt']
        for path in protected:atomic(path,'Protected')
        value=self.plan('Untrusted');value['task_notes_path']='../../memory.txt'
        agent,_=self.agent([value,'prose',value]);h=self.harness(agent)
        with self.assertRaises(Refused):h.plan()
        self.assertFalse(h.state.tasks);self.assertEqual((h.session/'goal.txt').read_text(),'Original authoritative goal.\n')
        self.assertTrue(all(p.read_text()=='Protected' for p in protected))

    def test_path_looking_note_is_only_literal_text_in_this_session(self):
        agent,_=self.agent([self.plan('../../memory.txt and goal.txt are literal observations')]);h=self.harness(agent);h.plan()
        self.assertFalse((self.root/'memory.txt').exists())
        self.assertEqual((self.session/'goal.txt').read_text(),'Original authoritative goal.\n')
        self.assertIn('../../memory.txt',(self.session/'task-notes.txt').read_text())

    def test_linked_notes_and_linked_sessions_are_rejected_before_commit(self):
        agent,calls=self.agent([self.plan('Do not follow links')]);h=self.harness(agent);target=self.session/'task-notes.txt'
        with patch.object(Path,'is_symlink',lambda p:p==target),self.assertRaises(Refused):task_notes_context(self.session)
        with patch.object(Path,'is_symlink',lambda p:p==target),self.assertRaises(Refused):h.plan()
        with patch.object(Path,'is_junction',lambda p:p==self.session),self.assertRaises(Refused):task_notes_path(self.session)
        self.assertFalse(target.exists());self.assertFalse(calls);self.assertFalse(h.state.tasks)

    def test_bad_or_unreadable_saved_notes_are_not_omitted_or_rewritten(self):
        h=self.harness(None);path=self.session/'task-notes.txt'
        for raw in (b'\xff',b'x'*(TASK_NOTES_LIMIT+1),b'NUL\x00'):
            path.write_bytes(raw)
            with self.subTest(raw=raw[:10]),self.assertRaises(Refused):h.plan_context()
            self.assertEqual(path.read_bytes(),raw)
        atomic(path,'Saved observation');original,exists=Path.open,Path.exists
        def denied(p,*args,**kwargs):
            if p==path:raise PermissionError('fixture denied')
            return original(p,*args,**kwargs)
        with patch.object(Path,'exists',lambda p:False if p==path else exists(p)),patch.object(Path,'open',denied),self.assertRaisesRegex(Refused,'readable UTF-8'):h.plan_context()
        self.assertEqual(path.read_text(),'Saved observation')

    def test_interrupted_proposal_never_writes_candidate_notes(self):
        class Interrupted:
            def call(_,role,prompt,workspace,structured=False,**kwargs):raise KeyboardInterrupt
        h=self.harness(Interrupted());atomic(self.session/'task-notes.txt','Committed before interruption')
        with self.assertRaises(KeyboardInterrupt):h.plan()
        self.assertFalse(State.load(h.path).tasks);self.assertEqual((self.session/'task-notes.txt').read_text(),'Committed before interruption')

    def test_failed_state_commit_cannot_publish_new_notes(self):
        agent,_=self.agent([self.plan('New notes')]);h=self.harness(agent);atomic(self.session/'task-notes.txt','Old notes')
        save=h.save
        def fail_accepted_state():
            if h.state.tasks:raise OSError('state disk error')
            save()
        with patch.object(h,'save',side_effect=fail_accepted_state),self.assertRaises(OSError):h.plan()
        self.assertFalse(State.load(h.path).tasks);self.assertEqual((self.session/'task-notes.txt').read_text(),'Old notes')

    def test_failed_note_commit_reports_accepted_plan_without_retrying_it(self):
        agent,calls=self.agent([self.plan('New notes')]);h=self.harness(agent);atomic(self.session/'task-notes.txt','Old notes')
        write=harness.atomic
        def fail_note(path,text):
            if path.name=='task-notes.txt':raise OSError('notes disk error')
            write(path,text)
        with patch('harness.atomic',side_effect=fail_note),self.assertRaisesRegex(Refused,'Plan accepted; task notes were not saved'):h.plan()
        self.assertEqual(len(calls),1);self.assertEqual(len(State.load(h.path).tasks),1)
        self.assertEqual((self.session/'task-notes.txt').read_text(),'Old notes')

    def test_interrupt_after_plan_commit_keeps_old_note_and_restart_does_not_replan(self):
        agent,calls=self.agent([self.plan('New notes')]);h=self.harness(agent);atomic(self.session/'task-notes.txt','Old notes');write=harness.atomic
        def interrupted_note(path,text):
            if path.name=='task-notes.txt':raise KeyboardInterrupt
            write(path,text)
        with patch('harness.atomic',side_effect=interrupted_note),self.assertRaises(KeyboardInterrupt):h.plan()
        class Worker:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                self.assertEqual(role,'ALGORITHM');self.assertIn('Old notes',prompt)
                return dict(task_id=task_id,status='DONE',summary='Assigned scope completed')
        resumed=Harness(self.session,Worker());self.assertEqual(resumed.run(),0)
        self.assertEqual(len(calls),1);self.assertEqual(len(resumed.state.tasks),1)
        self.assertEqual((self.session/'task-notes.txt').read_text(),'Old notes')

    def test_worker_report_cannot_use_the_planner_note_channel(self):
        class Worker:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                return dict(task_id=task_id,status='DONE',summary='Claim',task_notes='Unauthorized')
        h=self.harness(Worker());h.state=harness.apply_plan(h.state,parse_plan('ADD | 0 | ALGORITHM | | Work\nFINISH'));h.save()
        h.step(h.state.eligible())
        self.assertNotEqual(h.state.tasks[1].status,'DONE');self.assertFalse((self.session/'task-notes.txt').exists())


@unittest.skipUnless(CLI.exists(),'Pinned Qwen CLI unavailable')
class NativeTaskNotesTests(NotesSetup):
    def test_native_optional_note_is_schema_constrained_and_committed_with_a_valid_plan(self):
        with Endpoint(True) as endpoint:
            endpoint.first_plan={'operations':parse_plan('ADD | 0 | TEST | | Verify existing scope'),
                                 'terminal':{'op':'FINISH'},'task_notes':'Native note: preserve UTF-8 \u2603.'}
            q=Qwen(str(CLI),'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',self.session)
            h=self.harness(q);h.plan()
            self.assertEqual((self.session/'task-notes.txt').read_text(encoding='utf-8'),'Native note: preserve UTF-8 \u2603.')
            self.assertEqual(q.counter,1);self.assertEqual(len(endpoint.requests),1)
            body=endpoint.requests[0];tools={t['function']['name']:t['function'] for t in body['tools']}
            schema=tools['structured_output']['parameters']
            self.assertEqual(schema['properties']['operations']['items']['oneOf'][0]['properties'],
                             PLAN_SCHEMA['properties']['operations']['items']['oneOf'][0]['properties'])
            self.assertEqual(schema['properties']['terminal'],PLAN_SCHEMA['properties']['terminal'])
            self.assertEqual(schema['properties']['task_notes']['maxLength'],TASK_NOTES_LIMIT)
            self.assertFalse({'edit','write_file','run_shell_command'}&tools.keys())
            self.assertEqual((body['thinking_budget_tokens'],body['max_tokens']),(8192,11264))

    def test_native_rejected_plan_cannot_publish_its_note_before_text_recovery(self):
        with Endpoint(True) as endpoint:
            endpoint.first_plan={'operations':[dict(op='ADD',parent_id=0,role='TEST',summary='Bad',needs=[1])],
                                 'terminal':{'op':'FINISH'},'task_notes':'Rejected candidate'}
            q=Qwen(str(CLI),'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',self.session)
            h=self.harness(q);atomic(self.session/'task-notes.txt','Prior note');h.plan()
            self.assertEqual(q.counter,2);self.assertEqual((self.session/'task-notes.txt').read_text(),'Prior note')
            self.assertEqual(len(h.state.tasks),2);self.assertIn('Prior note',str(endpoint.requests[1]['messages']))

    def test_native_worker_schemas_do_not_gain_the_note_permission(self):
        with Endpoint(False) as endpoint:
            endpoint.final_only=True;h=self.harness(None)
            atomic(self.session/'task-notes.txt','READ_ONLY_NATIVE_NOTE')
            q=Qwen(str(CLI),'MID',32768,f'http://127.0.0.1:{endpoint.http.server_port}',self.session)
            for role in ('ALGORITHM','TEST'):
                task=Task(1,0,role,'','Check this assigned scope')
                self.assertEqual(q.call(role,h.context(task,self.session/'review'),h.workspace,task_id=1)['status'],'DONE')
            for body in endpoint.requests:
                tool=next(t for t in body['tools'] if t['function']['name']=='structured_output')
                self.assertNotIn('task_notes',tool['function']['parameters']['properties'])
                for actual,expected in zip(tool['function']['parameters']['properties']['report']['oneOf'],REPORT_SCHEMA['properties']['report']['oneOf']):
                    self.assertEqual(actual['properties'],expected['properties']);self.assertEqual(actual['required'],expected['required'])
                self.assertEqual(str(body['messages']).count('READ_ONLY_NATIVE_NOTE'),1)
            self.assertEqual((self.session/'task-notes.txt').read_text(),'READ_ONLY_NATIVE_NOTE')


if __name__=='__main__':unittest.main()
