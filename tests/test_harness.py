from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harness import (PROFILES, SPECULATION, AgentFailure, Harness, Refused, State, apply_plan, atomic, clone, download, execute,
    files, generation, lock, parse_plan, port_free, safe_path, merge, apply_report, REPORT_SCHEMA, PLAN_SCHEMA,
    Server, start_process, stop_process, windows_servers)
from chat import Conversation


def plan():
    return parse_plan('ADD | 0 | ALGORITHM | calc.py | Implement addition\nADD | 0 | TEST | tests/test_calc.py | Independently test addition\nFINISH')


OBSERVED_FALLBACK = 'Explanation before the plan.\nADD | 0 | TEST | | Verify behavior\nFINISH'


class Temporary(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / 'sessions'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rel, text):
        atomic(self.root / rel, text)
        return self.root / rel


def report(id,status='DONE',**extra):
    return dict(task_id=id,status=status,summary='Assigned work completed') | extra


def running(s,id):
    s.tasks[id].status='RUNNING';s.tasks[id].attempts+=1
    return s


class PlanTests(Temporary):
    def test_invalid_batches_are_atomic(self):
        s=apply_plan(State(),plan());before=s.render()
        for ops in [parse_plan('ADD | 0 | ALGORITHM | other.py | Another task'),
                    parse_plan('ADD | 0 | ALGORITHM | other.py | Another task\nEDIT | 999 | Invalid\nFINISH'),
                    [dict(op='ADD',id=3,parent_id=0,role='TEST',path='tests.js',summary='Bad fields'),dict(op='FINISH')],
                    [dict(op='ADD',parent_id=True,role='TEST',path='tests.js',summary='Bool'),dict(op='FINISH')]]:
            with self.subTest(ops=ops),self.assertRaises(Refused):apply_plan(s,ops)
            self.assertEqual(s.render(),before)

    def test_plan_terminal_and_report_schema_are_objects(self):
        self.assertEqual(PLAN_SCHEMA['required'],['operations','terminal'])
        terminal=PLAN_SCHEMA['properties']['terminal']
        self.assertEqual(terminal['type'],'object')
        self.assertEqual(terminal['oneOf'][0]['properties']['op']['enum'],['FINISH'])
        self.assertIn('reason',terminal['oneOf'][1]['required'])
        branches=REPORT_SCHEMA['properties']['report']['oneOf']
        self.assertIn('reason',branches[1]['required'])
        self.assertNotIn('reason',branches[0]['required'])
        self.assertEqual(branches[1]['properties']['status']['enum'],['FAILED','BLOCKED'])

    def test_blocked_is_stop_only_and_plan_changes_are_rejected_atomically(self):
        s=apply_plan(State(),plan());s=apply_report(running(s,1),1,report(1))
        before=s.render()
        changes=[dict(op='ADD',parent_id=0,role='TEST',summary='More checks'),
                 dict(op='EDIT',id=2,summary='Refine checks'),dict(op='NEEDS',id=2,needs=[1]),
                 dict(op='DROP',id=2,reason='Superseded'),dict(op='REOPEN',id=1,reason='New defect')]
        for change in changes:
            with self.subTest(op=change['op']),self.assertRaisesRegex(Refused,'BLOCKED requires empty operations'):
                apply_plan(s,[change,dict(op='BLOCKED',reason='Revise my plan in the next round')])
            self.assertEqual(s.render(),before)
        stopped=apply_plan(s,parse_plan('BLOCKED | Required permission is unavailable'))
        self.assertEqual(stopped.tasks,s.tasks);self.assertEqual(stopped.phase,'blocked')
        self.assertFalse(stopped.closed);self.assertFalse(stopped.complete());self.assertIsNone(stopped.eligible())

    def test_protocol_rejects_prose_and_unsafe_paths(self):
        with self.assertRaises(Refused):parse_plan(OBSERVED_FALLBACK)
        for path in ['../x','C:/x','/x','a/../b','CON.txt','.qwen/settings.json','a|b','a.']:
            with self.subTest(path=path),self.assertRaises(Refused):safe_path(path)
        self.assertEqual(safe_path('src\\main.cpp'),'src/main.cpp')
        # Verification is project-independent, not limited to Python files.
        apply_plan(State(),parse_plan('ADD | 0 | TEST | spec/check.js | Verify behavior\nFINISH')).validate()

    def test_ids_survive_drop_restart_and_reject_old_state_formats(self):
        s=apply_plan(State(),plan());s=apply_plan(s,parse_plan('DROP | 1 | Superseded unstarted task\nFINISH'))
        s.save(self.root/'state.log');loaded=State.load(self.root/'state.log')
        s=apply_plan(loaded,parse_plan('ADD | 0 | ALGORITHM | x.cpp | Implement\nFINISH'))
        self.assertEqual(s.tasks[1].status,'DROPPED');self.assertEqual(s.tasks[3].id,3)
        for version in (1,2,3):
            self.write('old.log',f'Backtrackify state {version}\n')
            with self.assertRaisesRegex(Refused,'fresh'):State.load(self.root/'old.log')

    def test_children_can_reference_prior_adds_in_same_atomic_batch(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | a | Parent\nADD | 1 | TEST | b | Child\nFINISH'))
        self.assertEqual(s.tasks[1].status,'WAITING');self.assertEqual(s.eligible().id,2)

    def test_initial_needs_validation_preserves_empty_state_and_allows_forward_subtrees(self):
        base=State();before=base.render()
        for text in ('ADD | 0 | ALGORITHM | | Library | 1\nFINISH',
                     'ADD | 0 | ALGORITHM | | Library | 99\nFINISH',
                     'ADD | 0 | ALGORITHM | | Library\nADD | 0 | TEST | | Check | 1,1\nFINISH',
                     'ADD | 0 | ALGORITHM | | Library\nDROP | 1 | Superseded\nADD | 0 | TEST | | Check | 1\nFINISH'):
            with self.subTest(text=text),self.assertRaises(Refused):apply_plan(base,parse_plan(text))
            self.assertEqual(base.render(),before)
        state=apply_plan(base,parse_plan('ADD | 0 | TEST | | Release | 2\n'
            'ADD | 0 | ALGORITHM | | Integrate component\nADD | 2 | ALGORITHM | | Implement component\nFINISH'))
        order=[]
        while task:=state.eligible():
            order.append(task.id);state=apply_report(running(state,task.id),task.id,report(task.id))
        self.assertEqual(order,[3,2,1]);self.assertTrue(state.complete());self.assertEqual(base.render(),before)

    def test_needs_and_combined_cycles_are_rejected_atomically(self):
        base=apply_plan(State(),plan());before=base.render()
        for needs in ([99],[1],[2,2],[True],[-1],'2',[{}]):
            with self.subTest(needs=needs),self.assertRaises(Refused):
                apply_plan(base,[dict(op='NEEDS',id=1,needs=needs),dict(op='FINISH')])
            self.assertEqual(base.render(),before)
        for text in ['NEEDS | 1 | 2\nNEEDS | 2 | 1\nFINISH',
                     'ADD | 1 | TEST | | Child | 1\nFINISH',
                     'ADD | 1 | TEST | | Child\nADD | 3 | TEST | | Grandchild | 1\nFINISH',
                     'ADD | 1 | TEST | | Child | 2\nNEEDS | 2 | 1\nFINISH',
                     'NEEDS | 2 | 1\nDROP | 1 | Omit prerequisite\nFINISH',
                     'REOPEN | 1 | Not completed\nFINISH','REOPEN | 99 | Unknown\nFINISH']:
            with self.subTest(text=text),self.assertRaises(Refused):apply_plan(base,parse_plan(text))
            self.assertEqual(base.render(),before)
        clear=apply_plan(base,parse_plan('NEEDS | 2 | 1\nNEEDS | 2 | \nDROP | 1 | No active references remain\nFINISH'))
        self.assertEqual(clear.tasks[1].status,'DROPPED');self.assertEqual(clear.tasks[2].needs,[])

    def test_prerequisite_children_and_ordered_sibling_subtrees(self):
        cases=[
            # Observed inversion: post-check below an unperformed repair runs first.
            ('ADD | 0 | ALGORITHM | | Repair component\nADD | 1 | ALGORITHM | | Repair component again\n'
             'ADD | 2 | TEST | | Post-repair check\nFINISH',[3,2,1]),
            # Finish the repair subtree before its later sibling checks the result.
            ('ADD | 0 | TEST | | Accept component\nADD | 1 | ALGORITHM | | Repair component\n'
             'ADD | 1 | TEST | | Post-repair check\nADD | 2 | ALGORITHM | | Prepare repair\nFINISH',[4,2,3,1]),
            # A repair descendant really is a prerequisite for its checking ancestor.
            ('ADD | 0 | TEST | | Post-repair check\nADD | 1 | ALGORITHM | | Complete repair\n'
             'ADD | 2 | ALGORITHM | | Implement narrow prerequisite\nFINISH',[3,2,1])]
        for text,expected in cases:
            with self.subTest(expected=expected):
                s=apply_plan(State(),parse_plan(text));order=[]
                self.assertFalse(s.complete())  # Planning FINISH cannot finish unexecuted work.
                while task:=s.eligible():
                    order.append(task.id)
                    self.assertTrue(all(c.status in {'DONE','DROPPED'} for c in s.tasks.values() if c.parent==task.id))
                    s=apply_report(running(s,task.id),task.id,report(task.id));s.validate()
                self.assertEqual(order,expected);self.assertTrue(s.complete())

    def test_completed_and_split_tasks_cannot_be_edited(self):
        s=apply_plan(State(),plan());s=apply_report(running(s,1),1,report(1))
        for ops in ['EDIT | 1 | Bad\nFINISH','DROP | 1 | Superseded unstarted task\nFINISH','ADD | 1 | TEST | x | Bad\nFINISH']:
            with self.assertRaises(Refused):apply_plan(s,parse_plan(ops))
        s=apply_plan(s,parse_plan('ADD | 2 | TEST | a | Child\nFINISH'))
        with self.assertRaises(Refused):apply_plan(s,parse_plan('EDIT | 2 | Bad\nFINISH'))
        s=apply_report(running(s,3),3,report(3))
        self.assertEqual(s.tasks[2].status,'PENDING')
        with self.assertRaises(Refused):apply_plan(s,parse_plan('EDIT | 2 | Forget original scope\nFINISH'))

    def test_depth_limits_and_cycles(self):
        for depth in (5,7):
            s=State(max_depth=depth)
            ops=[dict(op='ADD',parent_id=i,role='ALGORITHM',path='source',summary=f'Scope {i}') for i in range(depth)]
            s=apply_plan(s,ops+[dict(op='FINISH')]);before=s.render()
            self.assertNotIn(depth,s.allowed_add_parents())
            with self.assertRaises(Refused):apply_plan(s,parse_plan(f'ADD | {depth} | TEST | t | Too deep\nFINISH'))
            self.assertEqual(s.render(),before)
            s.tasks[1].parent=depth
            with self.assertRaises(Refused):s.validate()

    def test_capacity_split_is_model_defined_and_cannot_repeat_whole_task(self):
        for count in (1,2,4):
            s=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Check all behavior\nFINISH'))
            t=s.tasks[1];t.status,t.kind,t.reason='FAILED','limit','Output exhausted'
            before=s.render()
            for text in ['EDIT | 1 | Check all again\nFINISH','ADD | 1 | TEST | | Check all behavior\nFINISH']:
                with self.assertRaises(Refused):apply_plan(s,parse_plan(text))
                self.assertEqual(s.render(),before)
            children=[dict(op='ADD',parent_id=1,role='TEST',summary=f'Model chosen scope {i}') for i in range(count)]
            result=apply_plan(s,children+[dict(op='FINISH')])
            self.assertEqual([t.summary for t in result.tasks.values()][1:],[x['summary'] for x in children])

    def test_failed_parent_continuation_preserves_scope_children_and_attempt_bounds(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Integrate full scope\n'
            'ADD | 1 | ALGORITHM | | Narrow prerequisite\nADD | 0 | TEST | | Independent result\nFINISH'))
        for id in (2,3):s=apply_report(running(s,id),id,report(id))
        parent=s.tasks[1];parent.status,parent.kind,parent.reason='FAILED','limit','Session-turn cap'
        for attempts in (1,2,3):
            parent.attempts=attempts;before=s.render()
            invalid=['EDIT | 1 | Replace original requirement\nFINISH','FINISH','DROP | 1 | Discard requirement\nFINISH']
            if attempts==3:invalid.append('EDIT | 1 | Integrate full scope\nFINISH')
            for text in invalid:
                with self.subTest(attempts=attempts,text=text),self.assertRaises(Refused):apply_plan(s,parse_plan(text))
                self.assertEqual(s.render(),before)
            if attempts<3:
                continued=apply_plan(s,parse_plan('EDIT | 1 | Integrate full scope\nFINISH'))
                self.assertEqual(continued.eligible().id,1);self.assertFalse(continued.complete())
                self.assertEqual(continued.tasks[1].attempts,attempts);self.assertEqual(continued.tasks[1].reason,parent.reason)
                self.assertEqual(continued.tasks[2],s.tasks[2]);self.assertEqual(continued.tasks[3],s.tasks[3])
                completed=apply_report(running(continued,1),1,report(1))
                self.assertTrue(completed.complete());self.assertEqual(completed.tasks[1].attempts,attempts+1)

    def test_deep_failure_restart_and_parent_reports_in_any_saved_order(self):
        for depth in (5,7):
            for reverse in (False,True):
                s=apply_plan(State(max_depth=depth),[dict(op='ADD',parent_id=i,role='TEST',summary=f'Scope {i}') for i in range(depth)]+[dict(op='FINISH')])
                s=apply_report(running(s,depth),depth,report(depth))
                self.assertFalse(s.complete());self.assertEqual(s.eligible().id,depth-1)
                s=apply_report(running(s,depth-1),depth-1,report(depth-1,'FAILED',reason='Observed edge defect'))
                if reverse:s.tasks=dict(reversed(list(s.tasks.items())))
                s.save(self.root/'nested.log')
                if reverse:
                    rows=(self.root/'nested.log').read_text(encoding='utf-8').splitlines()
                    atomic(self.root/'nested.log','\n'.join(rows[:2]+list(reversed(rows[2:])))+'\n')
                s=State.load(self.root/'nested.log');s.recover()
                ops=[dict(op='ADD',parent_id=depth-1,role=role,summary=scope) for role,scope in
                     [('ALGORITHM','Repair edge'),('TEST','Verify repair')]]+[dict(op='FINISH')]
                s=apply_plan(s,ops)
                self.assertTrue(all(s.tasks[i].status=='WAITING' for i in range(1,depth)))
                self.assertEqual(s.tasks[depth].status,'DONE')
                order=[]
                while task:=s.eligible():
                    order.append(task.id);s=apply_report(running(s,task.id),task.id,report(task.id));s.validate()
                self.assertEqual(order,[depth+1,depth+2,*range(depth-1,0,-1)])
                self.assertTrue(s.complete());self.assertEqual(s.tasks[depth-1].attempts,2)

    def test_pending_parent_cannot_start_with_unfinished_child(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | a | Parent\nADD | 1 | TEST | b | Child\nFINISH'))
        s.tasks[1].status='PENDING'
        self.assertEqual(s.eligible().id,2)
        s.recover();self.assertEqual(s.tasks[1].status,'WAITING')

    def test_report_validation_is_atomic_and_success_is_not_inferred(self):
        s=running(apply_plan(State(),plan()),1);before=s.render()
        invalid=[{},report(99),report(2),report(1,'VERIFIED'),report(1,'FAILED'),report(1,'BLOCKED',reason=''),
                 report(1,related_tasks=[999]),report(1,related_tasks=[1]),report(1,related_tasks=[2,2]),
                 report(1,related_tasks=[True]),report(True),report(1,summary=''),report(1,status_='DONE'),
                 report(1,reason='bad|row'),report(1,related_tasks=[{}]),dict(task_id=1,status={},summary='Invalid')]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(Refused):apply_report(s,1,value)
            self.assertEqual(s.render(),before)
        done=apply_report(s,1,report(1));self.assertEqual(done.tasks[1].status,'DONE')
        with self.assertRaises(Refused):apply_report(done,1,report(1))

    def test_related_ids_are_information_and_do_not_change_other_statuses(self):
        s=apply_plan(State(),plan())
        s=apply_report(running(s,1),1,report(1))
        s=apply_report(running(s,2),2,report(2,'FAILED',reason='Observed wrong sum',related_tasks=[1]))
        self.assertEqual(s.tasks[1].status,'DONE');self.assertEqual(s.tasks[2].kind,'agent')
        self.assertEqual(s.tasks[1].reason,'');self.assertEqual(s.tasks[2].related,[1]);self.assertFalse(s.closed)

    def test_optional_paths_reasoned_drop_and_false_finish(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Verify without file edits\nADD | 0 | ALGORITHM | | Optional work\nFINISH'))
        self.assertEqual(s.tasks[1].path,'')
        before=s.render()
        for ops in [[dict(op='DROP',id=2),dict(op='FINISH')],parse_plan('DROP | 2 | \nFINISH')]:
            with self.assertRaises(Refused):apply_plan(s,ops)
            self.assertEqual(s.render(),before)
        s=apply_plan(s,parse_plan('DROP | 2 | Requirement superseded by task 1\nFINISH'))
        self.assertIn('superseded',s.tasks[2].reason);self.assertFalse(s.complete())
        s=apply_report(running(s,1),1,report(1,'FAILED',reason='Behavior still wrong'))
        before=s.render()
        for ops in ['FINISH','EDIT | 1 | Retry\nDROP | 1 | Hide failure\nFINISH']:
            with self.assertRaises(Refused):apply_plan(s,parse_plan(ops))
            self.assertEqual(s.render(),before)
        stopped=apply_plan(s,parse_plan('BLOCKED | Cannot resolve the defect within this run'))
        stopped.save(self.root/'blocked.log')
        self.assertEqual(State.load(self.root/'blocked.log').reason,stopped.reason)
        self.assertFalse(stopped.complete())

    def test_atomic_save_failure_preserves_checkpoint(self):
        s=State();s.save(self.root/'state.log');before=(self.root/'state.log').read_bytes()
        with patch('harness.os.replace',side_effect=OSError('disk error')),self.assertRaises(OSError):s.save(self.root/'state.log')
        self.assertEqual((self.root/'state.log').read_bytes(),before)


class WorkflowTests(Temporary):
    def harness(self,agent,state=None):
        self.session=self.root/'session';(self.session/'workspace').mkdir(parents=True,exist_ok=True)
        atomic(self.session/'goal.txt','Build any project and verify its requested behavior.')
        atomic(self.session/'memory.txt','Constraint: preserve public interfaces.\n')
        (state or State()).save(self.session/'state.log')
        return Harness(self.session,agent)

    def test_dfs_delegation_order_survives_checkpoint_and_reordered_records(self):
        for parents,expected in [([0,0,1],[3,1,2]),([0,0,1,1,3,3],[5,6,3,4,1,2]),
                                 ([0,0,0,0,0,0,0,0,1],[9,1,2])]:
            for reverse in (False,True):
                with self.subTest(order=expected,reverse=reverse):
                    ops=[dict(op='ADD',parent_id=parent,role='TEST' if id==2 else 'ALGORITHM',summary=f'Scope {id}')
                         for id,parent in enumerate(parents,1)]
                    if len(parents)==9:
                        ops += [dict(op='DROP',id=id,reason='Optional unstarted scope no longer required') for id in range(3,9)]
                    calls=[]
                    class Agent:
                        def call(_,role,prompt,workspace,task_id=None,**kwargs):
                            calls.append(task_id);return report(task_id)
                    agent=Agent();h=self.harness(agent,apply_plan(State(),ops+[dict(op='FINISH')]))
                    if reverse:
                        rows=h.path.read_text(encoding='utf-8').splitlines()
                        atomic(h.path,'\n'.join(rows[:2]+list(reversed(rows[2:])))+'\n')
                        h=Harness(self.session,agent)
                    before=h.state.render()
                    with self.assertRaises(Refused):h.step(h.state.tasks[2])
                    self.assertEqual(h.state.render(),before)
                    with patch('harness.execute',side_effect=AssertionError('Project command')),patch('harness.start_process',side_effect=AssertionError('Project spawn')):
                        h.step(h.state.eligible())
                        saved=h.path.read_bytes();h=Harness(self.session,agent)
                        self.assertEqual(h.path.read_bytes(),saved)
                        self.assertEqual(h.run(),0)
                    self.assertEqual(calls,expected);self.assertTrue(h.state.complete())
                    self.assertTrue(all(t.attempts==1 for t in h.state.tasks.values() if t.status!='DROPPED'))

    def test_dfs_replanning_stays_in_failed_branch_through_nested_failures_and_restart(self):
        calls,contexts=[],[]
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                if role=='PROMPT':
                    contexts.append(prompt)
                    failed=next(t for t in h.state.tasks.values() if t.status=='FAILED')
                    return {'operations':[dict(op='ADD',parent_id=failed.id,role='ALGORITHM',summary=f'Narrow repair {len(contexts)}'),dict(op='FINISH')]}
                calls.append(task_id)
                if task_id==1 and calls.count(1)<=2:
                    return report(1,'FAILED',reason=f'Observed defect {calls.count(1)}')
                if task_id==3 and calls.count(3)==1:
                    return report(3,'FAILED',reason='Repair needs a narrower prerequisite')
                return report(task_id)
        state=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Original verification\nADD | 0 | TEST | | Independent branch\nFINISH'))
        agent=Agent();h=self.harness(agent,state)
        with patch('harness.execute',side_effect=AssertionError('Project command')):
            h.step(h.state.eligible());self.assertIsNone(h.state.eligible())  # FAILED must not expose root 2.
            h.plan();h.step(h.state.eligible())  # Child 3 itself fails.
            h.state.replans+=1;h.state.phase='planning';h.save()  # Next round interrupted before its first invocation.
            saved=h.path.read_bytes();h=Harness(self.session,agent)
            self.assertEqual(h.path.read_bytes(),saved)
            self.assertEqual(h.run(),0)
        self.assertEqual(calls,[1,3,4,3,1,5,1,2])
        self.assertEqual([t.attempts for t in h.state.tasks.values()],[3,1,2,1,1])
        self.assertIn('Observed defect 1',contexts[1]);self.assertIn('narrower prerequisite',contexts[1])
        self.assertIn('Observed defect 2',contexts[2]);self.assertTrue(h.state.complete())

    def test_interrupted_deep_branch_and_blocked_branch_never_skip_to_next_root(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                if role=='PROMPT':
                    self_test.assertIn('[interrupted]',prompt)
                    return {'operations':parse_plan('ADD | 3 | ALGORITHM | | Inspect interrupted work\nFINISH')}
                calls.append(task_id)
                if len(calls)==1:raise KeyboardInterrupt
                return report(task_id)
        self_test=self;agent=Agent()
        state=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Original check\nADD | 0 | TEST | | Later branch\nADD | 1 | ALGORITHM | | Repair\nFINISH'))
        h=self.harness(agent,state)
        with self.assertRaises(KeyboardInterrupt):h.step(h.state.eligible())
        self.assertEqual(State.load(h.path).tasks[3].status,'RUNNING')
        h=Harness(self.session,agent)
        self.assertEqual(h.run(),0);self.assertEqual(calls,[3,4,3,1,2])
        class Blocked:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                if role=='PROMPT':return {'operations':[dict(op='BLOCKED',reason='Current branch cannot proceed without permission')]}
                calls.append(task_id);return report(task_id,'BLOCKED',reason='Permission required')
        calls.clear();h=self.harness(Blocked(),state)
        self.assertEqual(h.run(),2);self.assertEqual(calls,[3]);self.assertEqual(h.state.tasks[2].status,'PENDING')
        self.assertFalse(h.state.complete());h.state.validate()

    def test_normal_workflow_has_no_project_execution_or_extra_planning(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                calls.append((role,task_id))
                return {'operations':plan()} if role=='PROMPT' else report(task_id)
        h=self.harness(Agent())
        with patch('harness.execute',side_effect=AssertionError('Standalone command')),patch('harness.start_process',side_effect=AssertionError('Standalone spawn')):
            self.assertEqual(h.run(),0)
            self.assertEqual(Harness(self.session,Agent()).run(),0)
        self.assertEqual(calls,[('PROMPT',None),('ALGORITHM',1),('TEST',2)])
        self.assertTrue(h.state.complete());self.assertEqual(h.state.replans,1)

    def test_finish_starts_workers_and_mixed_blocked_gets_only_one_correction(self):
        for mixed in (False,True):
            calls=[]
            summary='Fix CSV \u574f and preserve Unicode'
            ops=[dict(op='ADD',parent_id=0,role='ALGORITHM' if i%2 else 'TEST',summary=summary if i==1 else f'Check scope {i}')
                 for i in range(1,8)]
            self_test=self
            class Agent:
                def call(_,role,prompt,workspace,structured=False,task_id=None):
                    calls.append((role,structured,task_id))
                    if role=='PROMPT':
                        if structured:
                            return {'operations':ops+[dict(op='BLOCKED',reason='Non-ASCII and truncated summaries; resubmitting next round') if mixed else dict(op='FINISH')]}
                        self_test.assertIn('BLOCKED requires empty operations',prompt)
                        self_test.assertEqual(h.state.tasks,{})
                        self_test.assertEqual(State.load(h.path).next_id,1)
                        return '\n'.join(f'ADD | 0 | {op["role"]} | | {op["summary"]}' for op in ops)+'\nFINISH'
                    self_test.assertFalse(h.state.complete())
                    self_test.assertEqual(h.state.tasks[1].summary,summary)
                    return report(task_id)
            h=self.harness(Agent())
            with patch('harness.execute',side_effect=AssertionError('Project execution')),patch('harness.start_process',side_effect=AssertionError('Project spawn')):
                self.assertEqual(h.run(),0)
            self.assertEqual([id for role,_,id in calls if role!='PROMPT'],list(range(1,8)))
            self.assertEqual(len([role for role,_,_ in calls if role=='PROMPT']),1+int(mixed))
            self.assertEqual(h.state.actions,7);self.assertEqual(h.state.next_id,8)
            self.assertEqual(State.load(h.path).phase,'complete')

    def test_dependency_dfs_crosses_branches_returns_and_reuses_one_prerequisite(self):
        cases=[('ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Check | 1\nADD | 1 | ALGORITHM | | Repair\nFINISH',[3,1,2]),
               ('ADD | 0 | TEST | | Check | 4\nADD | 0 | ALGORITHM | | Independent\nADD | 0 | TEST | | Optional\n'
                'ADD | 0 | ALGORITHM | | Prerequisite\nADD | 0 | TEST | | Optional\nADD | 4 | ALGORITHM | | Preparation\n'
                'DROP | 3 | Unneeded optional work\nDROP | 5 | Unneeded optional work\nFINISH',[6,4,1,2]),
               ('ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Check A | 1\nADD | 0 | TEST | | Check B | 1\nFINISH',[1,2,3])]
        for text,expected in cases:
            with self.subTest(expected=expected):
                calls=[]
                class Agent:
                    def call(_,role,prompt,workspace,task_id=None,**kwargs):
                        task=h.state.tasks[task_id]
                        for id in task.needs:
                            self_test.assertEqual(h.state.tasks[id].status,'DONE')
                            self_test.assertIn(f'{id}: {h.state.tasks[id].summary}; Finished {id}',prompt)
                        calls.append(task_id);return report(task_id,summary=f'Finished {task_id}')
                self_test=self;agent=Agent();h=self.harness(agent,apply_plan(State(),parse_plan(text)))
                with patch('harness.execute',side_effect=AssertionError('Project execution')),patch('harness.start_process',side_effect=AssertionError('Project spawn')):
                    h.step(h.state.eligible());h=Harness(self.session,agent)
                    self.assertEqual(h.run(),0)
                self.assertEqual(calls,expected);self.assertEqual(len(calls),len(set(calls)))

    def test_reopen_transitively_invalidates_only_dependents_and_survives_restart(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):
                calls.append(task_id);return report(task_id,summary=f'Accepted work {task_id}')
        state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Check | 1\n'
                    'ADD | 0 | TEST | | Independent\nADD | 0 | TEST | | Final check | 2\nFINISH'))
        agent=Agent();h=self.harness(agent,state);self.assertEqual(h.run(),0)
        before=h.state.render()
        for text in ['NEEDS | 2 | \nFINISH','REOPEN | 1 | \nFINISH',
                     'REOPEN | 1 | Fix defect\nADD | 1 | ALGORITHM | | Narrow repair | 4\nFINISH']:
            with self.assertRaises(Refused):apply_plan(h.state,parse_plan(text))
            self.assertEqual(h.state.render(),before)
        h.state=apply_plan(h.state,parse_plan('REOPEN | 1 | Newly observed defect\nADD | 1 | ALGORITHM | | Narrow repair\nFINISH'))
        self.assertEqual([t.status for t in h.state.tasks.values()],['WAITING','PENDING','DONE','PENDING','PENDING'])
        self.assertEqual([h.state.tasks[i].attempts for i in range(1,5)],[1]*4)
        self.assertEqual(h.state.tasks[2].kind,'invalidated');self.assertFalse(h.state.complete())
        for i in range(70):h.state.event(str(i))
        h.save();h=Harness(self.session,agent)
        context=h.plan_context()
        self.assertIn('[reopened] Newly observed defect',context);self.assertIn('[invalidated]',context)
        self.assertIn('prior accepted report: Accepted work 2',context);self.assertIn('waiting needs 1',context)
        self.assertNotIn('Unaccepted partial work: reviews/2-',context)
        calls.clear()
        with patch('harness.execute',side_effect=AssertionError('Automatic retest')):self.assertEqual(h.run(),0)
        self.assertEqual(calls,[5,1,2,4]);self.assertEqual(h.state.tasks[3].attempts,1)

    def test_child_reopen_invalidates_deep_ancestors_and_cross_branch_dependents(self):
        for depth in (5,7):
            calls=[]
            class Agent:
                def call(_,role,prompt,workspace,task_id=None,**kwargs):
                    calls.append(task_id);return report(task_id)
            ops=[dict(op='ADD',parent_id=i,role='TEST',summary=f'Scope {i}') for i in range(depth)]
            ops += [dict(op='ADD',parent_id=0,role='TEST',summary='Dependent',needs=[1]),
                    dict(op='ADD',parent_id=0,role='TEST',summary='Independent'),dict(op='FINISH')]
            agent=Agent();h=self.harness(agent,apply_plan(State(max_depth=depth),ops));self.assertEqual(h.run(),0)
            h.state=apply_plan(h.state,parse_plan(f'REOPEN | {depth} | Repeat changed leaf\nFINISH'))
            self.assertEqual([h.state.tasks[i].status for i in range(1,depth)],['WAITING']*(depth-1))
            self.assertEqual(h.state.tasks[depth+1].status,'PENDING');self.assertEqual(h.state.tasks[depth+2].status,'DONE')
            h.save();rows=h.path.read_text(encoding='utf-8').splitlines()
            atomic(h.path,'\n'.join(rows[:2]+list(reversed(rows[2:])))+'\n')
            h=Harness(self.session,agent);calls.clear();self.assertEqual(h.run(),0)
            self.assertEqual(calls,[*range(depth,0,-1),depth+1]);h.state.validate()

    def test_dependency_changes_need_explicit_reopen_and_do_not_reset_attempts(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | A\nADD | 0 | TEST | | B | 1\nADD | 0 | TEST | | C | 2\nFINISH'))
        for id in (1,2,3):s=apply_report(running(s,id),id,report(id))
        s=apply_plan(s,parse_plan('REOPEN | 2 | Recheck against revised prerequisites\nNEEDS | 2 | \nFINISH'))
        self.assertEqual(s.tasks[1].status,'DONE');self.assertEqual(s.tasks[2].needs,[])
        self.assertEqual(s.tasks[3].status,'PENDING');self.assertEqual(s.tasks[3].kind,'invalidated')
        s.tasks[2].attempts=3;s.tasks[2].status='DONE';s.tasks[3].status='DONE';s.closed=True;s.validate()
        before=s.render()
        with self.assertRaises(Refused):apply_plan(s,parse_plan('REOPEN | 2 | Must repeat\nFINISH'))
        self.assertEqual(s.render(),before)
        stopped=apply_plan(s,parse_plan('BLOCKED | No attempts remain to repeat the accepted result'))
        self.assertEqual(stopped.tasks[2].attempts,3);self.assertEqual(stopped.phase,'blocked');self.assertFalse(stopped.complete())
        self.assertEqual(stopped.tasks,s.tasks)

    def test_failed_virtual_prerequisite_replans_before_original_root_and_keeps_links(self):
        calls,contexts=[],[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    contexts.append(prompt)
                    if structured:raise AgentFailure('context','Twelve tokens over context headroom')
                    return f'ADD | 3 | ALGORITHM | | Smaller prerequisite {len(contexts)//2}\nFINISH'
                calls.append(task_id)
                if task_id==3 and calls.count(3)==1:raise KeyboardInterrupt
                if task_id==3 and calls.count(3)==2:return report(3,'FAILED',reason='Observed prerequisite defect')
                return report(task_id)
        s=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Original | 3\nADD | 0 | TEST | | Independent\nADD | 0 | ALGORITHM | | Prerequisite\nFINISH'))
        agent=Agent();h=self.harness(agent,s)
        with self.assertRaises(KeyboardInterrupt):h.step(h.state.eligible())
        self.assertEqual(State.load(h.path).tasks[3].status,'RUNNING');h=Harness(self.session,agent)
        self.assertEqual(h.run(),0);self.assertEqual(calls,[3,4,3,5,3,1,2])
        self.assertIn('needs 3',contexts[0]);self.assertIn('[interrupted]',contexts[0])
        self.assertIn('Observed prerequisite defect',contexts[2])
        self.assertIn('Twelve tokens over',contexts[1]);self.assertEqual(h.state.tasks[1].needs,[3])

    def test_failure_checkpoint_restart_deepening_then_structured_success(self):
        contexts=[];worker_calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    contexts.append(prompt)
                    return {'operations':parse_plan('ADD | 2 | ALGORITHM | calc.py | Fix observed sum\nADD | 2 | TEST | spec.js | Verify repaired sum\nFINISH')}
                worker_calls.append(task_id)
                if task_id==2 and worker_calls.count(2)==1:return report(2,'FAILED',reason='Observed subtraction instead of addition')
                if task_id==3:atomic(workspace/'calc.py','Changed by the delegated worker\n')
                return report(task_id)
        h=self.harness(Agent(),apply_plan(State(),plan()))
        with patch('harness.execute',side_effect=AssertionError('Project command')):
            h.step(h.state.eligible());h.step(h.state.eligible())
            snapshot=h.path.read_bytes()
            h=Harness(self.session,Agent());self.assertEqual(h.path.read_bytes(),snapshot)
            self.assertEqual(h.run(),0)
        self.assertEqual(worker_calls,[1,2,3,4,2]);self.assertIn('Observed subtraction',contexts[0])
        self.assertIn('2 | 0 | TEST | FAILED',contexts[0]);self.assertTrue(h.state.complete())
        self.assertEqual(h.state.tasks[1].status,'DONE')  # File edits do not invent a failure.

    def test_nested_repair_precedes_post_check_and_original_failed_parent_after_restart(self):
        calls,contexts=[],[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    contexts.append(prompt)
                    return {'operations':parse_plan(
                        'ADD | 1 | TEST | | Check repaired component\n'
                        'ADD | 2 | ALGORITHM | | Complete component repair\n'
                        'ADD | 3 | ALGORITHM | | Implement narrow parsing prerequisite\nFINISH')}
                calls.append((task_id,role))
                if task_id==1 and len(calls)==1:return report(1,'FAILED',reason='Component rejected a valid input')
                if role=='TEST':self_test.assertEqual([id for id,r in calls if r=='ALGORITHM'],[4,3])
                return report(task_id)
        self_test=self;agent=Agent()
        h=self.harness(agent,apply_plan(State(),parse_plan('ADD | 0 | TEST | | Check complete contract\nFINISH')))
        with patch('harness.execute',side_effect=AssertionError('Standalone project command')):
            h.step(h.state.eligible());h.plan()
            self.assertEqual(h.state.eligible().id,4)
            h.step(h.state.eligible())
            checkpoint=h.path.read_bytes();h=Harness(self.session,agent)
            self.assertEqual(h.path.read_bytes(),checkpoint)
            self.assertEqual(h.state.tasks[1].status,'WAITING');self.assertEqual(h.state.tasks[3].status,'PENDING')
            self.assertEqual(h.run(),0)
        self.assertEqual(calls,[(1,'TEST'),(4,'ALGORITHM'),(3,'ALGORITHM'),(2,'TEST'),(1,'TEST')])
        self.assertEqual(h.state.tasks[1].attempts,2);self.assertTrue(h.state.complete())
        self.assertIn('Component rejected a valid input',contexts[0])
        for rule in ('child before parent at every depth','DFS postorder','whole subtree','creation-ID order','Explicit needs','parent_id is not a topic group'):
            self.assertIn(rule,contexts[0])
        instructions=(Path(__file__).resolve().parents[1]/'agents/prompt.md').read_text(encoding='utf-8')
        for rule in ('Children are prerequisites, not topic groups','A child must never need its parent or any ancestor',
                     'BAD: #1 TEST with child #2 ALGORITHM needs=[1]','GOOD: #1 TEST with child #2 ALGORITHM, no extra needs'):
            self.assertIn(rule,instructions)

    def test_blocked_dependency_is_reported_not_installed(self):
        contexts=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':contexts.append(prompt);return {'operations':[dict(op='BLOCKED',reason='Permission cannot be obtained in this run')]}
                return report(task_id,'BLOCKED',reason='Permission required to install external dependency')
        h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | app | Build\nFINISH')))
        with patch('harness.execute',side_effect=AssertionError('Install')),patch('harness.subprocess.run',side_effect=AssertionError('Install')):
            self.assertEqual(h.run(),2)
        self.assertEqual(h.state.tasks[1].status,'BLOCKED')
        self.assertIn('Permission required',contexts[0]);self.assertIn('work reported',contexts[0])
        restored=State.load(h.path);self.assertEqual(restored.tasks[1].reason,h.state.tasks[1].reason)

    def test_partial_failed_report_can_promote_work_without_success(self):
        class Agent:
            def call(_,role,prompt,workspace,**kwargs):
                atomic(workspace/'spec.js','partial verification artifact')
                return report(1,'FAILED',reason='Checks found an implementation defect')
        h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | TEST | spec.js | Verify\nFINISH')))
        with patch('harness.execute',side_effect=AssertionError('Project command')):h.step(h.state.eligible())
        self.assertEqual((h.workspace/'spec.js').read_text(),'partial verification artifact')
        self.assertEqual(h.state.tasks[1].status,'FAILED');self.assertFalse(h.state.complete())

    def test_scope_rejection_retains_valid_report_and_does_not_blindly_retry(self):
        for outcome in ('FAILED','DONE'):
            with self.subTest(outcome=outcome):
                calls=[]
                class Agent:
                    def call(_,role,prompt,workspace,**kwargs):
                        calls.append(role)
                        atomic(workspace/'tests/check.py','delegated verification')
                        atomic(workspace/"'",'')  # Observed Windows shell quoting artifact.
                        return report(1,outcome,summary='Read-side checks completed',
                                      reason='Trailing newline accepted by the date parser',related_tasks=[2])
                h=self.harness(Agent(),apply_plan(State(),parse_plan(
                    'ADD | 0 | TEST | tests/check.py | Check input\nADD | 0 | TEST | | Final check | 1\nFINISH')))
                with patch('harness.execute',side_effect=AssertionError('No project command')):
                    h.step(h.state.eligible())
                h=Harness(self.session,h.agent)
                task=h.state.tasks[1]
                self.assertEqual((task.status,task.kind,task.attempts),('FAILED','workspace',1))
                self.assertIsNone(h.state.eligible());self.assertEqual(calls,['TEST'])
                self.assertEqual(task.result,'Read-side checks completed');self.assertEqual(task.related,[2])
                context=h.plan_context()
                self.assertIn("unexpected file: '",context)
                self.assertIn('Trailing newline accepted by the date parser',context)
                self.assertIn('retained report (not current completion): Read-side checks completed',context)
                self.assertFalse((h.workspace/'tests/check.py').exists());self.assertFalse((h.workspace/"'").exists())
                self.assertTrue(list((self.session/'reviews').glob('*/tests/check.py')))
                self.assertFalse(h.state.complete());h.state.validate()

    def test_loop_context_split_preserves_work_and_reserves_parent_final_attempt(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    self_test.assertIn('[context]',prompt)
                    self_test.assertIn('prior unresolved cause: Repeated model output',prompt)
                    return {'operations':parse_plan('ADD | 1 | TEST | checks/child.py | Narrow check\nFINISH')}
                calls.append(task_id)
                if task_id==1 and calls.count(1)<3:
                    atomic(workspace/'checks/original.py','unaccepted work')
                    raise AgentFailure('loop' if calls.count(1)==1 else 'context',
                                       'Repeated model output' if calls.count(1)==1 else 'Context headroom exhausted')
                if task_id==3:
                    self_test.assertIn('Earlier review',prompt)
                    atomic(workspace/'checks/child.py','Completed narrower check')
                return report(task_id)
        self_test=self
        h=self.harness(Agent(),apply_plan(State(),parse_plan(
            'ADD | 0 | TEST | checks/original.py | Original check\nADD | 0 | TEST | | Final check | 1\nFINISH')))
        with patch('harness.execute',side_effect=AssertionError('No independent project execution')):
            self.assertEqual(h.run(),0)
        self.assertEqual(calls,[1,1,3,1,2]);self.assertEqual(h.state.tasks[1].attempts,3)
        self.assertFalse((h.workspace/'checks/original.py').exists())
        self.assertTrue((h.workspace/'checks/child.py').exists())
        self.assertEqual(len(list((self.session/'reviews').glob('1-*/checks/original.py'))),2)
        h=Harness(self.session,h.agent);self.assertTrue(h.state.complete());h.state.validate()

    def test_invalid_or_missing_reports_never_promote_partial_files(self):
        for response in [None,'DONE',{},report(2),report(1,'FAILED'),report(1,'UNKNOWN')]:
            class Agent:
                def call(_,role,prompt,workspace,**kwargs):
                    atomic(workspace/'app','partial change');return response
            h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | app | Build\nFINISH')))
            h.step(h.state.eligible())
            self.assertNotEqual(h.state.tasks[1].status,'DONE');self.assertFalse((h.workspace/'app').exists())
            self.assertTrue(list((self.session/'reviews').glob('*/app')))

    def test_backend_categories_reach_prompt_without_invented_code_defect(self):
        for kind in ('limit','timeout','context','stream','loop','qwen','output','protocol'):
            class Agent:
                def call(_,role,prompt,workspace,**kwargs):
                    atomic(workspace/'app','partial');raise AgentFailure(kind,'Specific '+kind+' failure')
            h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | app | Build\nFINISH')))
            for _ in range(3):
                t=h.state.eligible()
                if not t:break
                h.step(t)
            context=h.plan_context()
            self.assertIn('['+kind+'] Specific',context);self.assertIn('not proof of an implementation defect',context)
            self.assertFalse((h.workspace/'app').exists());self.assertLessEqual(h.state.tasks[1].attempts,3)
            if kind in {'limit','timeout','context'}:self.assertIn('smaller independent children',context)
            else:self.assertIn('no attempts remain',context)

    def test_loop_retry_limit_and_failed_fallback_are_bounded(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                calls.append((role,structured))
                if role=='PROMPT':return {'operations':[]} if structured else 'Invalid prose'
                raise AgentFailure('loop','repetitive_thoughts')
        h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | app | Build\nFINISH')))
        self.assertEqual(h.run(),2)
        self.assertIn('Bounded planning attempts exhausted',h.state.reason)
        self.assertEqual(State.load(h.path).phase,'blocked')
        self.assertEqual(calls,[('ALGORITHM',False)]*3+[('PROMPT',True),('PROMPT',False),('PROMPT',True)])
        self.assertEqual(h.state.tasks[1].status,'FAILED')

    def test_failed_parent_empty_finish_and_prose_fallback_block_without_losing_children(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                calls.append((role,structured,prompt))
                return {'operations':parse_plan('FINISH')} if structured else 'The files are in place.\nADD | 1 | ALGORITHM | | Another child\nFINISH'
        state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Integrate\nADD | 1 | ALGORITHM | | Strictness check\nFINISH'))
        state=apply_report(running(state,2),2,report(2));state.tasks[1].attempts=2
        state.tasks[1].status,state.tasks[1].kind,state.tasks[1].reason='FAILED','limit','Session-turn cap'
        before=state.tasks.copy();h=self.harness(Agent(),state)
        self.assertEqual(h.run(),2);self.assertEqual(len(calls),3)
        self.assertEqual(h.state.tasks,before);self.assertFalse(h.state.complete())
        self.assertIn('FINISH never retries',calls[1][2])
        self.assertIn('native: Unresolved FAILED/BLOCKED tasks: 1',h.state.reason)
        self.assertIn('text: Invalid plan line: The files are in place.',h.state.reason)
        loaded=State.load(h.path);self.assertEqual(loaded.phase,'blocked');self.assertEqual(loaded.tasks,before)

    def test_console_failure_after_commit_cannot_replay_a_plan_or_reclassify_a_report(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                calls.append(role)
                return {'operations':plan()} if role=='PROMPT' else report(task_id)
        h=self.harness(Agent())
        with patch('harness.status',side_effect=OSError('Console closed')),self.assertRaises(OSError):h.plan()
        self.assertEqual(calls,['PROMPT']);self.assertEqual(len(State.load(h.path).tasks),2)
        with patch('harness.status',side_effect=OSError('Console closed')),self.assertRaises(OSError):h.step(h.state.eligible())
        loaded=State.load(h.path);self.assertEqual(loaded.tasks[1].status,'DONE')
        self.assertFalse(any('[workspace]' in e or 'Planning snapshot' in e for e in loaded.events))

    def test_native_plan_error_has_one_fresh_text_correction(self):
        contexts=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                contexts.append(prompt)
                if structured:
                    atomic(workspace/'unaccepted','must not leak');raise AgentFailure('context','headroom exhausted')
                self_test.assertFalse((workspace/'unaccepted').exists())
                return 'ADD | 0 | TEST | check | Verify behavior\nFINISH'
        self_test=self;h=self.harness(Agent());h.plan()
        self.assertEqual(len(contexts),2);self.assertIn('headroom exhausted',contexts[1])
        self.assertEqual(h.state.tasks[1].role,'TEST');self.assertFalse((h.workspace/'unaccepted').exists())

    def test_invalid_native_and_prose_fallback_get_one_atomic_native_correction(self):
        calls=[];delegated=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role!='PROMPT':delegated.append(task_id);return report(task_id)
                calls.append((structured,prompt,workspace))
                self_test.assertFalse(State.load(h.path).tasks)
                if len(calls)==1:
                    return {'operations':parse_plan('ADD | 0 | ALGORITHM | | Build | 1\nFINISH')}
                if len(calls)==2:return 'State confirmed: ready to plan.\nFINISH'
                return {'operations':parse_plan('ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Verify | 1\nFINISH')}
        self_test=self;h=self.harness(Agent())
        self.assertEqual(h.run(),0)
        self.assertEqual([c[0] for c in calls],[True,False,True]);self.assertEqual(len({c[2] for c in calls}),3)
        self.assertIn('Task 1 needs itself',calls[2][1]);self.assertIn('Invalid plan line: State confirmed:',calls[2][1])
        self.assertEqual(delegated,[1,2]);self.assertEqual(h.state.replans,1)
        self.assertEqual([t.attempts for t in h.state.tasks.values()],[1,1])

    def test_planning_correction_intent_and_feedback_survive_restart(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role!='PROMPT':return report(task_id)
                calls.append((structured,prompt))
                if len(calls)==1:return {'operations':parse_plan('ADD | 0 | TEST | | Check | 1\nFINISH')}
                if len(calls)==2:raise KeyboardInterrupt
                return {'operations':parse_plan('ADD | 0 | TEST | | Check\nFINISH')}
        agent=Agent();h=self.harness(agent);h.state.replans=7;h.save()
        with self.assertRaises(KeyboardInterrupt):h.plan()
        saved=State.load(h.path);self.assertFalse(saved.tasks);self.assertEqual(saved.phase,'planning')
        self.assertEqual(saved.replans,8)
        for i in range(80):saved.event(f'Later observation {i}')
        saved.save(h.path);self.assertEqual(len(saved.events),40)
        paused=Harness(self.session,agent,run_seconds=1)
        with patch('harness.time.monotonic',side_effect=(0,2)):self.assertEqual(paused.run(),2)
        self.assertEqual(len(calls),2);self.assertEqual(State.load(h.path).phase,'planning')
        h=Harness(self.session,agent);self.assertEqual(h.run(),0)
        self.assertEqual([c[0] for c in calls],[True,False,True]);self.assertEqual(h.state.replans,8)
        self.assertIn('Task 1 needs itself',calls[2][1]);self.assertIn('interrupted before acceptance',calls[2][1])
        self.assertTrue(h.state.complete())

    def test_interrupted_final_correction_cannot_get_a_new_budget_or_execute_old_done_state(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                calls.append(structured)
                if len(calls)==3:raise KeyboardInterrupt
                return {'operations':[]} if structured else 'Prose is invalid'
        state=apply_report(running(apply_plan(State(),parse_plan('ADD | 0 | TEST | | Existing check\nFINISH')),1),1,report(1))
        h=self.harness(Agent(),state)
        with self.assertRaises(KeyboardInterrupt):h.plan()
        for i in range(80):h.state.event(f'Later observation {i}')
        h.save();self.assertEqual(len(h.state.events),40)
        before=h.state.tasks.copy();agent=h.agent;h=Harness(self.session,agent)
        self.assertEqual(h.run(),2);self.assertEqual(calls,[True,False,True])
        self.assertEqual(h.state.tasks,before);self.assertEqual(h.state.replans,1)
        self.assertEqual(h.state.phase,'blocked');self.assertIn('exhausted',h.state.reason)

    def test_planning_environment_backend_and_commit_errors_are_not_corrective_rejections(self):
        for error in (OSError('Disk unavailable'),AgentFailure('backend','Endpoint unavailable'),
                      AgentFailure('loop','Repeated ineffective tool calls'),subprocess.TimeoutExpired('qwen',5)):
            with self.subTest(error=error):
                calls=[]
                class Agent:
                    def call(_,role,prompt,workspace,structured=False,**kwargs):calls.append(structured);raise error
                h=self.harness(Agent())
                with self.assertRaises(type(error)):h.plan()
                self.assertEqual(calls,[True]);self.assertFalse(State.load(h.path).tasks)
                self.assertFalse(any('rejected:' in e for e in h.state.events))
        calls=[]
        class NoContext:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                calls.append(structured);raise AgentFailure('context','Even a fresh context cannot fit the goal')
        h=self.harness(NoContext())
        with self.assertRaisesRegex(Refused,'Even a fresh context'):h.plan()
        self.assertEqual(calls,[True,False]);h=Harness(self.session,h.agent)
        self.assertEqual(h.run(),2);self.assertEqual(calls,[True,False]);self.assertEqual(h.state.replans,1)
        calls=[]
        class Valid:
            def call(_,role,prompt,workspace,structured=False,**kwargs):calls.append(structured);return {'operations':plan()}
        h=self.harness(Valid());save=h.save
        def disk_error():
            if h.state.tasks:raise OSError('Cannot commit checkpoint')
            save()
        with patch.object(h,'save',side_effect=disk_error),self.assertRaises(OSError):h.plan()
        self.assertEqual(calls,[True]);self.assertFalse(h.state.tasks);self.assertFalse(State.load(h.path).tasks)

    def test_interrupt_running_agent_and_planning_preserves_committed_intent(self):
        class Interrupted:
            def call(_,role,prompt,workspace,**kwargs):
                atomic(workspace/'app','partial');raise KeyboardInterrupt
        h=self.harness(Interrupted(),apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | app | Build\nFINISH')))
        h.state.tasks[1].kind='agent';h.state.tasks[1].reason='Previously observed division defect'
        with self.assertRaises(KeyboardInterrupt):h.step(h.state.eligible())
        restored=State.load(h.path);self.assertEqual(restored.tasks[1].status,'RUNNING')
        restored.recover();self.assertEqual(restored.tasks[1].kind,'interrupted')
        self.assertIn('Previously observed division defect',restored.tasks[1].reason)
        self.assertEqual(restored.tasks[1].attempts,1);self.assertFalse((h.workspace/'app').exists())
        restored.phase='planning';restored.save(h.path)
        restored=State.load(h.path);restored.recover();restored.validate()
        self.assertEqual(restored.tasks[1].status,'FAILED');self.assertEqual(restored.phase,'planning')

    def test_failure_reasons_survive_event_rollover_and_restart(self):
        s=apply_report(running(apply_plan(State(),plan()),1),1,report(1,'FAILED',reason='Precise persistent cause',related_tasks=[2]))
        for i in range(70):s.event(str(i))
        h=self.harness(None,s);h=Harness(self.session,None)
        self.assertIn('Precise persistent cause',h.plan_context());self.assertIn('related tasks: 2',h.plan_context())

    def test_test_cannot_promote_implementation_but_any_verification_language_is_allowed(self):
        class Agent:
            def call(_,role,prompt,workspace,**kwargs):
                atomic(workspace/'implementation','bad');return report(1)
        h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | TEST | checks/spec.cpp | Verify\nFINISH')))
        h.step(h.state.eligible());self.assertFalse((h.workspace/'implementation').exists())
        self.assertNotEqual(h.state.tasks[1].status,'DONE')
        class ReadOnlyCheck:
            def call(_,role,prompt,workspace,**kwargs):
                atomic(workspace/'scratch.txt','Transient check output');return report(1)
        h=self.harness(ReadOnlyCheck(),apply_plan(State(),parse_plan('ADD | 0 | TEST | | Check behavior\nFINISH')))
        h.step(h.state.eligible())
        self.assertEqual(h.state.tasks[1].status,'DONE');self.assertFalse((h.workspace/'scratch.txt').exists())
        self.assertTrue(list((self.session/'reviews').glob('*/scratch.txt')))

    def test_test_artifact_delivery_survives_restart_and_missing_input_is_reported(self):
        for deliver in (False,True):
            with self.subTest(deliver=deliver):
                calls=[];contexts=[]
                class Agent:
                    def call(_,role,prompt,workspace,task_id=None,**kwargs):
                        calls.append(task_id)
                        if task_id==1:atomic(workspace/'feature.py','VALUE = 1\n')
                        if task_id==2:
                            atomic(workspace/'tests/test_feature.py','persistent verification\n')
                            return report(2,summary='Created tests/test_feature.py')
                        if task_id==3:
                            contexts.append(prompt)
                            if not (workspace/'tests/test_feature.py').is_file():
                                return report(3,'FAILED',summary='Inspected the delivered workspace',reason='Required input tests/test_feature.py is absent',related_tasks=[2])
                        return report(task_id)
                text='ADD | 0 | ALGORITHM | feature.py | Build feature\nADD | 0 | TEST | '+('tests/test_feature.py' if deliver else '')+' | Author reusable checks | 1\nADD | 0 | TEST | | Verify release | 1,2\nFINISH'
                h=self.harness(Agent(),apply_plan(State(),parse_plan(text)))
                h.step(h.state.eligible());h.step(h.state.eligible());accepted=h.state.tasks.copy()
                self.assertEqual((h.workspace/'tests/test_feature.py').exists(),deliver)
                h=Harness(self.session,h.agent);h.step(h.state.eligible())
                self.assertEqual(calls,[1,2,3]);self.assertEqual(h.state.tasks[2],accepted[2])
                self.assertEqual(h.state.tasks[3].status,'DONE' if deliver else 'FAILED')
                marker='2 [TEST report only; no file edits promoted]'
                self.assertEqual(marker in contexts[0],not deliver)
                if not deliver:
                    self.assertEqual(h.state.tasks[3].related,[2]);self.assertFalse(h.state.complete())
                    self.assertTrue(list((self.session/'reviews').glob('2-*/tests/test_feature.py')))

    def test_reopened_release_two_loops_exhaust_three_attempts_preserving_old_reports_and_children(self):
        calls=[];planning=[];rechecking=False
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    planning.append(prompt)
                    return {'operations':parse_plan('BLOCKED | Final task has no attempts left for its own report')}
                calls.append(task_id)
                if rechecking:raise AgentFailure('loop','Qwen loop: read_file_loop',report='Unaccepted text: all checks passed')
                return report(task_id,summary='Original accepted release report' if task_id==4 else 'Accepted component result')
        state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Integrate component\nADD | 1 | TEST | checks.py | Check component\nADD | 0 | TEST | | Independent check\nADD | 0 | TEST | | Release check | 1,3\nFINISH'))
        h=self.harness(Agent(),state);self.assertEqual(h.run(),0);self.assertEqual(calls,[2,1,3,4])
        accepted=h.state.tasks.copy();h.state=apply_plan(h.state,parse_plan('REOPEN | 4 | Recheck the changed release\nFINISH'));h.save()
        rechecking=True;h.step(h.state.eligible())
        self.assertEqual(h.state.tasks[4].status,'PENDING');self.assertEqual(h.state.tasks[4].attempts,2)
        h=Harness(self.session,h.agent);h.step(h.state.eligible())
        self.assertEqual(h.state.tasks[4].status,'FAILED');self.assertEqual(h.state.tasks[4].attempts,3)
        self.assertEqual(h.state.tasks[4].result,'Original accepted release report')
        context=h.plan_context()
        self.assertIn('retained report (not current completion): Original accepted release report',context)
        self.assertNotIn('work reported: Original accepted release report',context);self.assertNotIn('Unaccepted text',context)
        self.assertEqual(h.run(),2);self.assertEqual(calls,[2,1,3,4,4,4]);self.assertEqual(len(planning),1)
        self.assertIn('no attempts remain',planning[0])
        for id in (1,2,3):self.assertEqual(h.state.tasks[id],accepted[id])
        saved=State.load(h.path);self.assertEqual(saved.phase,'blocked');self.assertFalse(saved.complete())

    def test_file_promotion_rolls_back_partial_write_and_detects_concurrent_edits(self):
        atomic(self.root/'workspace/a','old a');atomic(self.root/'workspace/b','old b')
        before=clone(self.root/'workspace',self.root/'review')
        atomic(self.root/'review/a','new a');atomic(self.root/'review/b','new b')
        real_atomic=atomic
        def fail(path,data):
            if path.name=='b' and data==b'new b':raise OSError('write failed')
            real_atomic(path,data)
        with patch('harness.atomic',side_effect=fail),self.assertRaises(OSError):merge(self.root/'workspace',self.root/'review',before)
        self.assertEqual(files(self.root/'workspace'),before)
        atomic(self.root/'workspace/a','user edit')
        with self.assertRaisesRegex(Refused,'changed'):merge(self.root/'workspace',self.root/'review',before)

    def test_older_checkpoint_loads_only_accepted_reports_and_blocked_reason_survives(self):
        s=apply_plan(State(),plan());s=apply_report(running(s,1),1,report(1))
        s.save(self.root/'older.log')
        s=apply_report(running(s,2),2,report(2,'BLOCKED',reason='External permission is missing'))
        s.save(self.root/'current.log')
        older=State.load(self.root/'older.log');current=State.load(self.root/'current.log')
        self.assertEqual(older.tasks[1].status,'DONE');self.assertEqual(older.tasks[2].status,'PENDING')
        self.assertEqual(current.tasks[2].status,'BLOCKED');self.assertIn('permission',current.tasks[2].reason)
        self.assertFalse(older.complete());self.assertFalse(current.complete())

    def test_state_commit_failure_does_not_accept_report_or_replay_completed_operations(self):
        class Agent:
            def call(_,role,prompt,workspace,**kwargs):
                atomic(workspace/'source','unaccepted replacement');atomic(workspace/'new','unaccepted addition')
                (workspace/'old').unlink()
                return report(1)
        h=self.harness(Agent(),apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | source | Work\nFINISH')))
        atomic(h.workspace/'source','accepted base');atomic(h.workspace/'old','accepted old file');before=files(h.workspace)
        real_save=State.save
        def fail_done(state,path):
            if state.tasks[1].status=='DONE':raise OSError('State disk error')
            real_save(state,path)
        with patch.object(State,'save',fail_done):h.step(h.state.eligible())
        loaded=State.load(h.path)
        self.assertEqual(loaded.tasks[1].status,'FAILED');self.assertEqual(loaded.tasks[1].kind,'workspace')
        self.assertFalse(loaded.complete())
        self.assertEqual(files(h.workspace),before)
        self.assertTrue(list((self.session/'reviews').glob('*/new')))  # Failed promotion remains inspectable.

    def test_optional_history_warning_cannot_undo_a_committed_plan_or_worker_result(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,**kwargs):
                calls.append(role)
                return {'operations':plan()} if role=='PROMPT' else report(1)
        h=self.harness(Agent());(self.session/'history').mkdir()
        def warn(component,*args):
            if component=='VIEW':raise OSError('Console closed')
        with patch('visualize.record',side_effect=OSError('History disk error')),patch('harness.status',side_effect=warn):
            h.plan();h.step(h.state.eligible())
        state=State.load(h.path);self.assertEqual(state.tasks[1].status,'DONE');self.assertEqual(calls,['PROMPT','ALGORITHM'])
        self.assertEqual(len(state.tasks),2);self.assertFalse(any('[workspace]' in e or 'Planning snapshot' in e for e in state.events))

    def test_no_edit_reports_cannot_accept_a_concurrently_changed_workspace(self):
        for role,path in (('ALGORITHM','source'),('TEST','')):
            with self.subTest(role=role):
                class Agent:
                    def call(_,role,prompt,workspace,**kwargs):
                        atomic(h.workspace/'source','User edit while agent worked')
                        return report(1)
                h=self.harness(Agent(),apply_plan(State(),parse_plan(f'ADD | 0 | {role} | {path} | Check current source\nFINISH')))
                atomic(h.workspace/'source','Accepted base')
                self.assertIsNone(h.step(h.state.eligible()))
                state=State.load(h.path);self.assertEqual(state.tasks[1].status,'FAILED')
                self.assertEqual(state.tasks[1].kind,'workspace');self.assertFalse(state.complete())
                self.assertEqual((h.workspace/'source').read_text(),'User edit while agent worked')

    def test_wrong_task_duplicate_acceptance_and_unfinished_parent_reports_are_rejected(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | TEST | scope | Parent\nADD | 1 | TEST | child | Child\nFINISH'))
        h=self.harness(None,s)
        with self.assertRaises(Refused):h.step(h.state.tasks[1])
        bad=running(s,1)
        with self.assertRaisesRegex(Refused,'Parent status'):apply_report(bad,1,report(1))
        self.assertEqual(bad.tasks[1].status,'RUNNING')

    def test_parent_has_no_free_extra_attempt_after_split(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Verify full scope\nFINISH'))
        t=s.tasks[1];t.status,t.attempts,t.kind,t.reason='FAILED',3,'limit','Output exhausted'
        before=s.render()
        with self.assertRaisesRegex(Refused,'out of worker attempts'):
            apply_plan(s,parse_plan('ADD | 1 | ALGORITHM | | Narrow repair\nFINISH'))
        self.assertEqual(s.render(),before);self.assertNotIn(1,s.allowed_add_parents())
        stopped=apply_plan(s,parse_plan('BLOCKED | No attempts remain for the required parent report'))
        self.assertEqual(stopped.tasks[1].attempts,3);self.assertFalse(stopped.complete())
        # A retained v4 checkpoint from an older run must stop before executing futile children.
        legacy=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Parent\nADD | 1 | ALGORITHM | | Child\nFINISH'))
        legacy.tasks[1].attempts=3
        h=self.harness(None,legacy)
        with patch('harness.start_process',side_effect=AssertionError('Agent must not start')):self.assertEqual(h.run(),2)
        self.assertIn('own final report after children',h.state.reason)
        self.assertEqual(h.state.actions,0)

    def test_turn_cap_splits_after_first_attempt_then_parent_gives_its_own_report(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                calls.append((role,task_id))
                if role=='PROMPT':
                    self_test.assertIn('[limit]',prompt);self_test.assertIn('smaller independent children',prompt)
                    self_test.assertIn('not proof of an implementation defect',prompt)
                    return {'operations':parse_plan('ADD | 1 | ALGORITHM | source | Narrow remaining scope\nFINISH')}
                if task_id==1 and h.state.tasks[1].attempts==1:
                    atomic(workspace/'source','unaccepted partial work')
                    raise AgentFailure('limit','Qwen session-turn budget exhausted before the required final report')
                self_test.assertIn('16 session turns and 48 ordinary tool calls',prompt)
                return report(task_id)
        self_test=self;agent=Agent();h=self.harness(agent,apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | source | Complete scope\nADD | 0 | TEST | | Final check | 1\nFINISH')))
        h.step(h.state.eligible())
        self.assertEqual(h.state.tasks[1].status,'FAILED');self.assertEqual(h.state.tasks[1].attempts,1)
        self.assertFalse((h.workspace/'source').exists())
        h=Harness(self.session,agent)
        with patch('harness.execute',side_effect=AssertionError('Project command')),patch('harness.start_process',side_effect=AssertionError('Project spawn')):
            self.assertEqual(h.run(),0)
        self.assertEqual(calls,[('ALGORITHM',1),('PROMPT',None),('ALGORITHM',3),('ALGORITHM',1),('TEST',2)])
        self.assertEqual(h.state.tasks[1].attempts,2);self.assertTrue(h.state.complete())

    def test_replacing_parent_need_with_child_does_not_claim_the_whole_parent_result(self):
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):calls.append(task_id);return report(task_id)
        s=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Whole result\nADD | 0 | TEST | | Final check | 1\nADD | 1 | ALGORITHM | | Narrow part\nFINISH'))
        h=self.harness(Agent(),s);self.assertEqual(h.run(),0);completed=h.state
        # Genuine narrower consumers may change their declaration; the harness cannot infer its content.
        h.state=apply_plan(h.state,parse_plan('REOPEN | 2 | Only the narrow part is required now\nNEEDS | 2 | 3\nFINISH'))
        self.assertEqual(h.run(),0)
        h.state=apply_plan(h.state,parse_plan('REOPEN | 1 | Whole result changed\nFINISH'))
        self.assertEqual(h.state.tasks[2].status,'DONE')  # Removed edge has no implied invalidation.
        h.state=apply_plan(completed,parse_plan('REOPEN | 1 | Whole result changed\nFINISH'))
        self.assertIn('keep needs X unless',h.plan_context())
        self.assertEqual(h.state.tasks[2].status,'PENDING');self.assertEqual(h.state.eligible().id,1)
        self.assertEqual(h.state.tasks[2].kind,'invalidated');self.assertFalse(h.state.complete())
        self.assertEqual(h.run(),0)
        self.assertEqual(calls,[3,1,2,2,1,2])

    def test_whole_run_time_and_action_stops_are_distinct_from_worker_failures(self):
        for reason in ('time','action'):
            s=apply_plan(State(),plan())
            if reason=='action':s.actions=64
            h=self.harness(None,s)
            if reason=='time':h.run_seconds=21600  # Explicit diagnostic watchdog; never a production default.
            with patch('harness.time.monotonic',side_effect=[0,21601]),patch('harness.start_process',side_effect=AssertionError('Agent must not start')):
                self.assertEqual(h.run(),2)
            if reason=='time':
                self.assertEqual(h.state.phase,'ready');self.assertFalse(h.state.reason)
                self.assertIn('Run deadline reached',h.state.events[-1])
            else:self.assertIn('Run action limit reached',h.state.reason)
            self.assertTrue(all(t.status=='PENDING' and t.attempts==0 for t in h.state.tasks.values()))

    def test_production_has_no_total_deadline_and_diagnostic_deadline_is_explicit(self):
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):return report(task_id)
        state=apply_plan(State(),plan());state.started=time.time()-10*86400
        h=self.harness(Agent(),state);started=h.state.started
        h.run_seconds=21600
        with patch('harness.time.monotonic',side_effect=[0,21601]):self.assertEqual(h.run(),2)
        saved=State.load(h.path);self.assertEqual(saved.phase,'ready');self.assertFalse(saved.complete())
        h=Harness(self.session,Agent())
        with patch('harness.time.monotonic',return_value=100*86400):self.assertEqual(h.run(),0)
        self.assertEqual(h.state.started,started);self.assertIsNone(h.run_seconds)
        for invalid in (0,-1,float('inf'),True):
            with self.assertRaises(Refused):Harness(self.session,Agent(),invalid)
        self.assertEqual(Harness(self.session,Agent(),10*86400).run_seconds,10*86400)

    def test_worker_context_omits_unrelated_scopes_and_empty_ancestor_reviews(self):
        state=apply_plan(State(),parse_plan('ADD | 0 | TEST | tests/whole.py | Whole check\n'
            'ADD | 1 | TEST | tests/part.py | Narrow check\nADD | 0 | ALGORITHM | unrelated.py | UNRELATED_VERBOSE_SCOPE\nFINISH'))
        h=self.harness(None,state)
        old=self.session/'reviews/1-1-old';atomic(old/'ledger.py','unchanged unrelated source')
        text=h.context(h.state.tasks[2],self.session/'reviews/2-1-new')
        self.assertNotIn('Earlier review',text);self.assertNotIn('UNRELATED_VERBOSE_SCOPE',text)
        self.assertIn('3 | 0 | ALGORITHM | PENDING',text)
        atomic(old/'tests/whole.py','unaccepted useful test work')
        text=h.context(h.state.tasks[2],self.session/'reviews/2-1-new')
        self.assertIn(str((old/'tests/whole.py').resolve()),text)
        atomic(h.workspace/'tests/whole.py','unaccepted useful test work')
        self.assertNotIn('Earlier review',h.context(h.state.tasks[2],self.session/'reviews/2-1-new'))
        h.state.tasks[1].path='tests';atomic(old/'tests/additional.py','useful directory artifact')
        self.assertIn(str((old/'tests').resolve()),h.context(h.state.tasks[2],self.session/'reviews/2-1-new'))

    def test_resolved_causes_are_not_repeated_in_replanning_context(self):
        s=apply_plan(State(),plan());s=apply_report(running(s,1),1,report(1,summary='Historical implementation details'))
        s=apply_report(running(s,2),2,report(2,'FAILED',reason='Active failure',related_tasks=[1]))
        h=self.harness(None,s);text=h.plan_context()
        self.assertIn('Active failure',text);self.assertIn('related tasks: 1',text)
        self.assertNotIn('Historical implementation details',text)

    def test_static_budgets_remain_finite_with_final_output_headroom(self):
        for profile,(thinking,total,wall,depth) in PROFILES.items():
            for role in ('PROMPT','ALGORITHM','TEST'):
                self.assertEqual(generation(profile,role,32768),(thinking,total));self.assertGreaterEqual(total-thinking,1536)
            self.assertEqual(wall,{'LOW':3600,'MID':1800,'HIGH':1800,'EXTRA HIGH':2700}[profile]);self.assertIn(depth,(5,7))

    def test_reopen_of_exhausted_dependent_is_explained_and_atomic(self):
        s=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | library.py | Build library\nADD | 0 | TEST | | Check library | 1\nFINISH'))
        for id in (1,2):s=apply_report(running(s,id),id,report(id))
        s.tasks[2].attempts=3;s.validate();before=s.render()
        with self.assertRaisesRegex(Refused,'Task 2: No attempts remain'):
            apply_plan(s,parse_plan('REOPEN | 1 | New integration affects this result\nFINISH'))
        self.assertEqual(s.render(),before)
        h=self.harness(None,s);self.assertIn('DONE tasks with no remaining reports: 2',h.plan_context())

    def test_static_speculation_reaches_server_without_changing_model_or_context(self):
        for profile,mode in SPECULATION.items():
            server=Server('unused.exe','same.gguf',context=32768,threads=16,log=self.root/'server.log',spec_type=mode)
            with patch('harness.port_free'),patch('harness.windows_servers',return_value=[]),\
                 patch('vision.check_projector'),patch('vision.pillow'),\
                 patch('harness.start_process',side_effect=OSError('CPU launch capture')) as launched:
                with self.assertRaisesRegex(OSError,'CPU launch capture'):server.__enter__()
            command=launched.call_args.args[0]
            self.assertEqual(command[command.index('--spec-type')+1],mode)
            self.assertEqual(command[command.index('-m')+1],'same.gguf');self.assertEqual(command[command.index('-c')+1],'32768')
            self.assertIn('--mmproj-offload',command);self.assertEqual(command[command.index('--mmproj-device')+1],'CUDA0')
            if profile=='LOW':self.assertNotIn('--spec-draft-n-max',command)
            else:
                for flag,value in [('--spec-draft-n-max','2'),('--spec-draft-p-min','0.025'),
                                   ('--spec-ngram-mod-n-min','32'),('--spec-ngram-mod-n-max','64'),('--spec-ngram-mod-n-match','16')]:
                    self.assertEqual(command[command.index(flag)+1],value)

    def test_mtp_requires_embedded_draft_and_off_override_remains_available(self):
        from harness import main
        command=['load','--server','unused','--model','unused','--qwen','unused']
        with patch('harness.gguf_metadata',return_value={'qwen35.context_length':65536}),\
             patch('harness.backend_lock'),patch('harness.Server') as server:
            with self.assertRaisesRegex(Refused,'embedded MTP'):main(command)
            self.assertFalse(server.called)
            self.assertEqual(main(command+['--spec-type','none']),0)
            self.assertEqual(server.call_args.kwargs['spec_type'],'none')


class IsolationTests(Temporary):
    def test_headless_execution_cannot_wait_for_or_inject_parent_stdin(self):
        script=('from harness import execute; from pathlib import Path; import sys; '
                f'raise SystemExit(execute([sys.executable,"-c","import sys; print(repr(sys.stdin.read()))"],'
                f'Path({str(self.root)!r}),3,Path({str(self.root/"stdin.log")!r})))')
        with subprocess.Popen([sys.executable,'-B','-c',script],cwd=Path(__file__).resolve().parents[1],
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as parent:
            try:
                self.assertEqual(parent.wait(timeout=8),0)  # Parent's input stays open throughout the assertion.
            except BaseException:
                parent.kill();raise
        self.assertEqual((self.root/'stdin.log').read_text().strip(),"''")


    def test_download_integrity_cache_and_failed_partial_cleanup(self):
        import hashlib
        source=self.write('source.bin','pinned content\n')
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        destination=self.root/'download.bin'
        download(source.as_uri(),destination,digest)
        download(source.as_uri(),destination,digest)
        with self.assertRaisesRegex(Refused,'Existing file checksum differs'):
            download(source.as_uri(),destination,'0'*64)
        with self.assertRaisesRegex(Refused,'SHA-256 mismatch'):
            download(source.as_uri(),self.root/'bad.bin','0'*64)
        self.assertFalse((self.root/'bad.bin').exists())
        self.assertFalse((self.root/'bad.bin.part').exists())


    def test_subprocess_timeout_cleans_owned_process(self):
        import time
        start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            execute([sys.executable,'-c','import time; time.sleep(30)'],self.root,.2,self.root/'timeout.log')
        self.assertLess(time.monotonic()-start,10)


    def test_current_untracked_hidden_files_copied_assets_excluded(self):
        self.write('source/untracked.py', 'current = True\n')
        self.write('source/.hidden', 'keep\n')
        self.write('source/models/large.gguf', 'omit\n')
        self.write('source/.git/HEAD', 'omit\n')
        self.write('source/nested/.git','gitdir: unrelated-worktree')
        self.write('source/nested/models/domain.py','domain = True\n')
        self.write('source/local.ps1', 'omit secrets\n')
        clone(self.root / 'source', self.root / 'snapshot',snapshot=True)
        self.assertEqual(set(files(self.root / 'snapshot')), {'untracked.py', '.hidden', 'nested/models/domain.py'})
        clone(self.root/'source',self.root/'project')
        self.assertIn('models/large.gguf',files(self.root/'project'))
        self.assertIn('local.ps1',files(self.root/'project'))
        self.assertNotIn('nested/.git',files(self.root/'project'))
        self.assertEqual(safe_path('models/domain.py'),'models/domain.py')
        with self.assertRaises(Refused):
            clone(self.root / 'source', self.root / 'snapshot',snapshot=True)


    def test_port_ownership_and_session_lock(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            with self.assertRaisesRegex(Refused, 'occupied'):
                port_free(sock.getsockname()[1])
        with lock(self.root / 'lock'):
            with self.assertRaises(Refused):
                with lock(self.root / 'lock'):
                    pass
        with lock(self.root / 'lock'):
            pass  # prior process/turn releases ownership


@unittest.skipUnless(os.name == 'nt', 'Windows process ownership')
class WindowsProcessTests(Temporary):
    def setUp(self):
        super().setUp()
        import ctypes
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handles, self.processes = [], []

    def tearDown(self):
        # Retained handles only, including when the behavior under test fails.
        for p in self.processes:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=10)
        for handle in self.handles:
            if self.api.WaitForSingleObject(handle, 0) == 258:
                self.api.TerminateProcess(handle, 1)
                self.api.WaitForSingleObject(handle, 5000)
            self.api.CloseHandle(handle)
        super().tearDown()

    def watch(self, marker):
        deadline = time.monotonic() + 10
        while not marker.exists() or not marker.read_text():
            if time.monotonic() >= deadline:
                self.fail('CPU child did not report readiness: ' + str(marker))
            time.sleep(.02)
        handle = self.api.OpenProcess(0x100001, False, int(marker.read_text()))
        self.assertTrue(handle, 'Cannot retain CPU child handle')
        self.handles.append(handle)
        return handle

    def tree(self, marker, code=None):
        child = f'import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)'
        finish = 'time.sleep(60)' if code is None else f'sys.exit({code})'
        return (f'import os,sys,time,subprocess; from pathlib import Path\n'
                f'Path({str(marker.with_suffix(".root"))!r}).write_text(str(os.getpid()))\n'
                f'subprocess.Popen([sys.executable,"-B","-c",{child!r}])\n'
                f'while not Path({str(marker.with_suffix(".go"))!r}).exists(): time.sleep(.02)\n{finish}')

    def test_success_failure_and_timeout_reap_descendants_after_root_exit(self):
        for code in (0, 7, None):
            with self.subTest(code=code):
                marker = self.root / f'child-{code}.pid'
                def launch(*args, **kwargs):
                    p = start_process(*args, **kwargs)
                    self.processes.append(p)
                    self.watch(marker)
                    marker.with_suffix('.go').touch()
                    return p
                with patch('harness.start_process', side_effect=launch):
                    if code is None:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            execute([sys.executable, '-B', '-c', self.tree(marker)], self.root, .2, self.root/'timeout.log')
                    else:
                        self.assertEqual(execute([sys.executable, '-B', '-c', self.tree(marker, code)],
                                                 self.root, 10, self.root/'exit.log'), code)
                self.assertEqual(self.api.WaitForSingleObject(self.handles[-1], 5000), 0)

    def test_abrupt_python_owner_death_reaps_server_and_grandchild(self):
        marker = self.root/'abrupt.pid'
        command = [sys.executable, '-B', '-c', self.tree(marker)]
        script = ('from harness import execute; from pathlib import Path; '
                  f'execute({command!r},Path({str(self.root)!r}),60,Path({str(self.root/"owner.log")!r}))')
        owner = subprocess.Popen([sys.executable, '-B', '-c', script], cwd=Path(__file__).resolve().parents[1],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(owner)
        root, child = self.watch(marker.with_suffix('.root')), self.watch(marker)
        owner.kill()  # No finally/atexit: the same handle teardown as console process termination.
        owner.wait(timeout=10)
        for handle in (root, child):
            self.assertEqual(self.api.WaitForSingleObject(handle, 5000), 0)

    def test_keyboard_interrupt_reaps_owned_tree(self):
        marker = self.root/'interrupt.pid'
        def launch(*args, **kwargs):
            p = start_process(*args, **kwargs)
            self.processes.append(p)
            self.watch(marker)
            wait = p.wait
            def interrupt(*args, **kwargs):
                p.wait = wait
                raise KeyboardInterrupt
            p.wait = interrupt
            return p
        with patch('harness.start_process', side_effect=launch), self.assertRaises(KeyboardInterrupt):
            execute([sys.executable, '-B', '-c', self.tree(marker)], self.root, 30, self.root/'interrupt.log')
        self.assertEqual(self.api.WaitForSingleObject(self.handles[-1], 5000), 0)

    def test_cleanup_ignores_reused_pid_and_does_not_kill_other_process(self):
        command = [sys.executable, '-B', '-c', 'import time; time.sleep(60)']
        other = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        owned = start_process(command, creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.extend((other, owned))
        owned.pid = other.pid  # Simulate a stale/reused PID; actual handles/job membership differ.
        stop_process(owned)
        stop_process(owned)  # Repeated cleanup is safe.
        self.assertIsNotNone(owned.poll())
        self.assertIsNone(other.poll())

    def test_unowned_shared_asset_server_blocks_new_port_and_is_never_stopped(self):
        exe = self.root/'llama-server.exe'
        shutil.copyfile(sys.executable, exe)  # Harmless CPU process with the real image name.
        other = subprocess.Popen([str(exe), '-B', '-c', 'import time; time.sleep(60)'],
                                 env=dict(os.environ, PYTHONHOME=sys.base_prefix), creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(other)
        self.assertIn(other.pid, windows_servers())
        with patch('harness.start_process') as spawn, self.assertRaisesRegex(Refused, 'GPU ownership is unknown'):
            with Server(exe, self.root/'unused.gguf', port=0, log=self.root/'server.log'):
                self.fail('Must refuse even on a free, different port')
        spawn.assert_not_called()
        self.assertIsNone(other.poll())

    def test_server_normal_exception_interrupt_and_load_error_cleanup(self):
        for number, event in enumerate((None, RuntimeError('body failed'), KeyboardInterrupt(), 'load')):
            with self.subTest(event=event):
                def launch(command, **kwargs):
                    marker = self.root/f'server-{number}.pid'
                    script = f'import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)'
                    p = start_process([sys.executable, '-B', '-c', script], **kwargs)
                    self.processes.append(p)
                    self.watch(marker)  # Simulated health only after the CPU server actually starts.
                    return p
                server = Server('unused.exe', 'unused.gguf', port=0, log=self.root/'server.log')
                with patch('harness.windows_servers', return_value=[]), patch('harness.start_process', side_effect=launch), \
                     patch('vision.check_projector'),patch('vision.pillow'),\
                     patch('harness.request', side_effect=RuntimeError('load failed') if event == 'load' else None,
                           return_value={'status': 'ok','modalities':{'vision':True}}):
                    try:
                        with server:
                            if isinstance(event, BaseException):
                                raise event
                    except (RuntimeError, KeyboardInterrupt):
                        self.assertIsNotNone(event)
                self.assertIsNotNone(server.process.poll())
                self.assertTrue(server.handle.closed)


class ChatTests(Temporary):
    def test_browser_chat_opens_its_bound_port_automatically(self):
        from chat import serve
        for configured,actual in [(8081,8081),(0,12345)]:
            with patch('chat.ROOT',self.root),patch('chat.port_free'),patch('chat.ChatServer') as server,patch('chat.threading.Timer') as timer:
                server.return_value.__enter__.return_value.serve_forever.side_effect=KeyboardInterrupt()
                server.return_value.__enter__.return_value.server_port=actual
                self.assertEqual(serve('http://127.0.0.1:12345',32768,configured),0)
            self.assertTrue(timer.call_args.kwargs['args'][0].startswith(f'http://127.0.0.1:{actual}/#'))
            timer.return_value.start.assert_called_once()

    def test_plain_text_restart_and_rollover_retention(self):
        c = Conversation(self.root / 'chat.txt', 'http://unused', 8192)
        c.goal = 'Build a parser.\r\nPreserve Unicode: \u2603'
        c.summary = 'Constraint: no dependencies.'
        c.turns = [dict(role='user' if i%2==0 else 'assistant', content=f'message {i}\nmultiline') for i in range(8)]
        with patch.object(c,'tokens',return_value=200), patch.object(c,'complete',return_value='Goal: parser. Constraint: no dependencies. Findings: tokenizer is correct. Next: handle errors.'):
            c.compact(3072)
        restored = Conversation(self.root / 'chat.txt','http://unused',8192)
        self.assertEqual(restored.goal,c.goal)
        self.assertIn('no dependencies',restored.summary)
        self.assertEqual(len(restored.turns),2)
        self.assertEqual(restored.rollovers,1)
        self.assertIn('message 7',restored.turns[-1]['content'])

    def test_chat_failure_preserves_disk_and_memory_and_rejects_incomplete_pairs(self):
        c=Conversation(self.root/'chat.txt','http://unused',8192)
        with patch.object(c,'tokens',return_value=100),patch.object(c,'complete',return_value='Useful answer'):
            c.send('Original goal')
            before=c.path.read_bytes();turns=list(c.turns)
            with patch('chat.atomic',side_effect=OSError('disk full')),self.assertRaises(OSError):c.send('Next request')
            self.assertEqual(c.turns,turns);self.assertEqual(c.path.read_bytes(),before)
        c.turns += [dict(role='user',content='unpaired')];c.save()
        with self.assertRaisesRegex(Refused,'complete user/assistant'):Conversation(c.path,c.backend,c.context)

    def test_chat_rejects_inflated_or_unsaved_rollover_without_losing_history(self):
        c=Conversation(self.root/'chat.txt','http://unused',8192)
        c.goal='Preserve this goal';c.turns=[dict(role='user' if i%2==0 else 'assistant',content=str(i)) for i in range(6)];c.save()
        before=c.path.read_bytes();turns=list(c.turns)
        for failure in ('context','disk'):
            with patch.object(c,'tokens',side_effect=[100,8000] if failure=='context' else [100,100]),patch.object(c,'complete',return_value='Summary'):
                with patch('chat.atomic',side_effect=OSError('disk full') if failure=='disk' else None),self.assertRaises((Refused,OSError)):
                    c.compact(3072)
            self.assertEqual(c.turns,turns);self.assertEqual(c.summary,'');self.assertEqual(c.rollovers,0)
            self.assertEqual(c.path.read_bytes(),before)

    def test_overflow_refuses_before_generation(self):
        c = Conversation(self.root / 'chat.txt','http://unused',8192)
        with patch.object(c,'tokens',return_value=8000), patch.object(c,'complete') as complete:
            with self.assertRaises(Refused): c.send('too large')
            complete.assert_not_called()
        self.assertFalse(c.turns)
