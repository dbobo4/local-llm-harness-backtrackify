"""Read-only rendering and optional plaintext history; no GPU or visible test windows."""
import copy
import base64
from contextlib import ExitStack, nullcontext
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from harness import ROOT, Harness, State, AgentFailure, apply_plan, apply_report, parse_plan, atomic, lock, start_process, stop_process
from tests.test_harness import report, running
from visualize import open_view, read_history, render, work_times, columns, shorten, glyphs, viewport, last_plan, RESET, BACKGROUND, COLORS, GREEN, CYAN, AMBER, RED, MUTED, planned_order, task_name, throughput, process_stamp, owner_ended, view_channel, viewer_active, following, acceptance_result


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='view test ')
        self.session=Path(self.temp.name);self.path=self.session/'state.log'
        self.environment=patch.dict(os.environ,BACKTRACKIFY_VIEW_HOME=str(self.session/'view-home'),WT_SESSION='CPU-view-terminal')
        self.environment.start();self.addCleanup(self.environment.stop)
        (self.session/'workspace').mkdir()
        atomic(self.session/'goal.txt','Complete and check the assigned work.')
        atomic(self.session/'memory.txt','Preserve scope.\n')

    def tearDown(self):self.temp.cleanup()

    def state(self):
        return apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | | Build\nADD | 0 | TEST | | Check | 1\nFINISH'))

    def test_managed_viewer_identity_ignores_worker_pid_and_console_changes(self):
        owner=self.session/'native-cli';kernel=Mock();kernel.GetConsoleWindow.return_value=0
        with patch.dict(os.environ,WT_SESSION=''),patch('visualize.ctypes.windll',Mock(kernel32=kernel)):
            with patch('visualize.os.getpid',return_value=1001):
                old=view_channel(ROOT);first=view_channel(ROOT,owner=owner)
            with patch('visualize.os.getpid',return_value=2002):
                self.assertNotEqual(old,view_channel(ROOT));self.assertEqual(first,view_channel(ROOT,owner=owner))
            with patch.dict(os.environ,WT_SESSION='different-terminal'):
                self.assertEqual(first,view_channel(ROOT,owner=owner))
        self.assertNotEqual(first,view_channel(ROOT,owner=self.session/'another-cli'))
        self.state().save(self.path);atomic(self.session/'view.log','ENV | '+str(first)+'\n');atomic(first,str(self.session)+'\n')
        with lock(first.with_suffix('.lock')):
            self.assertNotEqual(first,view_channel(ROOT,self.session,owner=self.session/'another-cli'))

    @unittest.skipUnless(os.name=='nt','Windows viewer lease and console controls')
    def test_real_viewer_process_follows_resets_quits_and_reopens_without_duplicate(self):
        owner=self.session/'native-cli';channel=view_channel(ROOT,owner=owner);processes=[]
        control=self.session/'viewer-key.txt';titles=self.session/'viewer-title.txt';outputs=[]
        fixture=r'''
import sys
from pathlib import Path
from unittest.mock import Mock,patch
from visualize import main
session,channel,control,titles=map(Path,sys.argv[1:]);pending=[]
def pressed():return bool(pending) or control.exists()
def key():
 if not pending:
  value=control.read_text();control.unlink();pending.extend({'left':'\xe0K','up':'\xe0H'}.get(value,value))
 return pending.pop(0)
kernel=Mock();kernel.GetStdHandle.return_value=1;kernel.GetConsoleMode.return_value=0
kernel.SetConsoleTitleW.side_effect=lambda title:titles.write_text(title,encoding='utf-8')
with patch('visualize.ctypes.windll',Mock(kernel32=kernel)),patch('msvcrt.kbhit',side_effect=pressed),patch('msvcrt.getwch',side_effect=key):
 raise SystemExit(main([str(session),'--follow',str(channel)]))
'''
        def launched(*args,**kwargs):
            target=Path(channel.read_text().strip());output=self.session/('screen-'+str(len(processes))+'.log');outputs.append(output)
            with output.open('wb') as log:
                process=start_process([sys.executable,'-B','-c',fixture,str(target),str(channel),str(control),str(titles)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            processes.append(process);return subprocess.CompletedProcess([],0,b'',b'')
        def screen():
            return outputs[-1].read_text(encoding='utf-8',errors='replace').rsplit('BACKTRACKIFY',1)[-1] if outputs else ''
        def wait_for(check):
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                if check():return
                time.sleep(.05)
            self.fail('Viewer condition not reached; last screen: '+screen()[:2400])
        state=self.state();state.started=time.time()-300;state.tasks[1].summary='FIRST TASK ONLY';state.save(self.path)
        try:
            with patch('visualize.subprocess.run',side_effect=launched) as launch:
                open_view(self.session,ROOT,owner=owner);wait_for(lambda:'FIRST TASK ONLY' in screen())
                first_pid=processes[0].pid
                for id in (1,2):state=apply_report(running(state,id),id,report(id))
                state.phase='complete';state.save(self.path);completed=self.path.read_bytes()
                wait_for(lambda:'100%' in screen());atomic(control,'left');wait_for(lambda:'HISTORY' in screen())
                atomic(control,'up');wait_for(lambda:not control.exists())
                other=self.session/'second-session';other.mkdir()
                next_state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | next.py | SECOND TASK ONLY\nFINISH'));next_state.save(other/'state.log')
                open_view(other,ROOT,owner=owner);wait_for(lambda:'second-session' in screen() and 'next.py' in screen())
                self.assertEqual(launch.call_count,1);self.assertEqual(processes[0].pid,first_pid);self.assertIsNone(processes[0].poll())
                self.assertIn('0/1 DONE',screen());self.assertIn('0%',screen());self.assertNotIn('FIRST TASK ONLY',screen());self.assertNotIn('HISTORY',screen())
                self.assertRegex(screen(),r'Elapsed [0-5]s');self.assertIn('second-session',titles.read_text(encoding='utf-8'))
                next_state=apply_report(running(next_state,1),1,dict(task_id=1,status='BLOCKED',summary='Cannot continue',reason='Fixture blocked'))
                next_state.phase='blocked';next_state.reason='Fixture blocked';next_state.save(other/'state.log');blocked=(other/'state.log').read_bytes()
                wait_for(lambda:'BLOCKED' in screen());atomic(control,'q');self.assertEqual(processes[0].wait(6),0)
                self.assertFalse(viewer_active(channel));self.assertTrue(channel.exists())
                third=self.session/'third-session';third.mkdir();self.state().save(third/'state.log')
                open_view(third,ROOT,owner=owner);self.assertEqual(launch.call_count,2);self.assertNotEqual(processes[1].pid,first_pid)
                stop_process(processes[1]);self.assertFalse(viewer_active(channel))  # X/crash releases the OS lease too.
                fourth=self.session/'fourth-session';fourth.mkdir();self.state().save(fourth/'state.log')
                open_view(fourth,ROOT,owner=owner);self.assertEqual(launch.call_count,3);wait_for(lambda:'fourth-session' in screen())
                self.assertEqual(self.path.read_bytes(),completed);self.assertEqual((other/'state.log').read_bytes(),blocked)
        finally:
            for process in processes:stop_process(process)

    def test_managed_viewer_startup_failure_is_bounded_and_does_not_claim_liveness(self):
        self.state().save(self.path);owner=self.session/'native-cli';channel=view_channel(ROOT,owner=owner)
        with patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')),patch('visualize.time.monotonic',side_effect=[0,6]):
            with self.assertRaisesRegex(OSError,'did not claim'):open_view(self.session,ROOT,owner=owner)
        self.assertFalse(viewer_active(channel));self.assertEqual(following(channel),self.session.resolve())

    def test_managed_viewer_owners_keep_independent_live_leases_and_follow_targets(self):
        owners=[self.session/'cli-a',self.session/'cli-b'];channels=[]
        for owner in owners:channels.append(view_channel(ROOT,owner=owner))
        self.state().save(self.path)
        other=self.session/'other-task';other.mkdir();self.state().save(other/'state.log')
        later=self.session/'next-task';later.mkdir();self.state().save(later/'state.log')
        with ExitStack() as leases:
            def opened(*args,**kwargs):
                leases.enter_context(lock(channels[opened.count].with_suffix('.lock')));opened.count+=1
                return subprocess.CompletedProcess([],0,b'',b'')
            opened.count=0
            with patch('visualize.subprocess.run',side_effect=opened) as launch:
                open_view(self.session,ROOT,owner=owners[0]);open_view(other,ROOT,owner=owners[1])
                self.assertEqual(launch.call_count,2)
                open_view(later,ROOT,owner=owners[0]);self.assertEqual(launch.call_count,2)
                self.assertTrue(viewer_active(channels[0]));self.assertTrue(viewer_active(channels[1]))
                self.assertEqual(following(channels[0]),later.resolve());self.assertEqual(following(channels[1]),other.resolve())
        self.assertFalse(viewer_active(channels[0]));self.assertFalse(viewer_active(channels[1]))

    def replan_harness(self,plans,interrupt=8):
        s=apply_plan(State(),parse_plan('ADD | 0 | TEST | | Accept feature\n'
            'ADD | 1 | ALGORITHM | | Implement input\nADD | 1 | ALGORITHM | | Build report | 2\n'
            'ADD | 0 | TEST | | Duplicate verification\nADD | 0 | ALGORITHM | | Configure output\n'
            'ADD | 0 | TEST | | Final check | 1,5\nFINISH'))
        s.replans=1
        (self.session/'history').mkdir();s.save(self.path)
        plans=iter(plans);calls=[]
        class Agent:
            def call(_,role,prompt,workspace,structured=False,task_id=None):
                if role=='PROMPT':
                    value=next(plans)
                    return {'operations':parse_plan(value)} if structured else value
                calls.append(task_id)
                if task_id==2 and calls.count(2)==1:raise AgentFailure('limit','session turn limit')
                if task_id==interrupt and calls.count(task_id)==1:raise AgentFailure('stream','Interrupted before report')
                return report(task_id,summary=f'Completed scope {task_id}')
        h=Harness(self.session,Agent())
        self.assertEqual(h.state.eligible().id,2);h.step(h.state.eligible())
        return h

    def split_plan(self):
        return ('ADD | 2 | ALGORITHM | | Repair dates\nADD | 2 | ALGORITHM | | Repair CSV | 7\n'
                'ADD | 0 | TEST | | Check report | 3\nDROP | 4 | Duplicate verification\n'
                'NEEDS | 3 | 2,7\nEDIT | 5 | Configure deterministic output\nFINISH')

    def test_persisted_replan_diff_then_clean_tree_and_historical_retries(self):
        with patch('harness.time.strftime',return_value='2026-10-25 12:00:00'):
            h=self.replan_harness([self.split_plan()]);h.plan()
        frames=read_history(self.session);commit=len(frames)-1
        self.assertIn(frames[commit][1].events[-1],frames[commit-1][1].events)
        colored=render(frames,width=240);plain=re.sub(r'\x1b\[[0-9;]*m','',colored)
        self.assertIn('LAST PLAN · ADD #7,#8,#9',plain)
        for id in (7,8,9):
            row=next(line for line in plain.splitlines() if f'#{id} ALGORITHM' in line or f'#{id} TEST' in line)
            self.assertNotIn('+ NEW',row)
        self.assertIn('NEEDS',next(line for line in plain.splitlines() if '#3 ' in line and 'ALGORITHM' in line))
        self.assertIn('↳ needs #2 → #2,#7',plain)
        self.assertIn('EDIT',next(line for line in plain.splitlines() if '#5 ' in line and 'ALGORITHM' in line))
        dropped=next(line for line in colored.splitlines() if '#4 ' in line and 'TEST' in line)
        parent=next(line for line in colored.splitlines() if '#2 ' in line and 'ALGORITHM' in line)
        self.assertIn('\x1b[9;'+MUTED+'m',dropped);self.assertIn('DROPPED',dropped)
        self.assertIn('WAITING',parent);self.assertNotIn('\x1b[9;',parent)
        self.assertIn('Waiting for children · previous Turn limit',plain)
        self.assertNotIn('+ NEW',colored)
        expanded=render(frames,width=240,color=False,details=True)
        self.assertIn('Previous attempt: session turn limit',expanded)
        self.assertIn('was Configure output',expanded);self.assertIn('needs #2 → #2,#7',expanded)
        h.step(h.state.eligible())  # #7 starts and reports DONE; both checkpoints are real.
        frames=read_history(self.session)
        started=next(i for i,(_,s) in enumerate(frames) if s.tasks.get(7) and s.tasks[7].status=='RUNNING')
        history=render(frames,index=started,width=240,color=False)
        self.assertIn('RUNNING',history);self.assertNotIn('+ NEW',history);self.assertIn('▶ #7 ALGORITHM · RUNNING',history)
        clean=render(frames,width=240,color=False)
        self.assertNotIn('Clean current tree',clean);self.assertNotIn('+ NEW',clean);self.assertNotIn('#4 ',clean)
        self.assertIn('DONE',next(line for line in clean.splitlines() if '#7 ' in line and 'ALGORITHM' in line))
        self.assertEqual(h.state.tasks[2].status,'WAITING')
        self.assertEqual(h.state.tasks[3].needs,[2,7])
        h.step(h.state.eligible());self.assertEqual(h.state.tasks[8].status,'PENDING')
        h.step(h.state.eligible());self.assertEqual(h.state.tasks[8].attempts,2)
        frames=read_history(self.session)
        retry=next(i for i,(_,s) in enumerate(frames) if s.tasks.get(8) and s.tasks[8].status=='RUNNING' and s.tasks[8].attempts==2)
        self.assertNotIn('+ NEW',render(frames,index=retry,color=False,width=240))
        self.assertEqual(h.state.tasks[2].status,'PENDING')  # Never auto-DONE after its children.
        before={str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()}
        self.assertIn('DROPPED',render(frames,index=commit,color=False,width=240))
        for width in (24,46,80,120):
            text=render(frames,index=commit,width=width,details=True)
            self.assertTrue(all(columns(row)<=width for row in text.splitlines()))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()})

    def test_reopen_and_forward_invalidation_are_visible_only_in_their_accepted_phase(self):
        repair='REOPEN | 5 | Output needs a correction\nADD | 5 | ALGORITHM | | Narrow output repair\nFINISH'
        h=self.replan_harness([self.split_plan(),repair],interrupt=None)
        h.plan();self.assertEqual(h.run(),0)
        h.plan();frames=read_history(self.session);commit=len(frames)-1
        text=render(frames,width=240,color=False)
        self.assertIn('REOPEN',next(row for row in text.splitlines() if '#5 ' in row and 'ALGORITHM' in row))
        self.assertIn('INVALIDATED',next(row for row in text.splitlines() if '#6 ' in row and 'TEST' in row))
        self.assertEqual(h.state.tasks[3].status,'DONE');self.assertEqual(h.state.tasks[5].attempts,1)
        self.assertNotIn('#4 ',text)  # An old drop is not presented as a new change.
        h.step(h.state.eligible())
        self.assertEqual(h.state.tasks[5].status,'PENDING')
        self.assertNotIn('REPLAN DIFF',render(read_history(self.session),width=240,color=False))
        h=Harness(self.session,h.agent);self.assertEqual(h.run(),0)
        self.assertEqual(h.state.tasks[5].attempts,2);self.assertEqual(h.state.tasks[6].attempts,2)
        self.assertEqual(h.state.tasks[3].attempts,1)
        past=render(read_history(self.session),index=commit,width=240,color=False)
        self.assertIn('REOPEN',past);self.assertIn('INVALIDATED',past);self.assertIn('ADD #10',past);self.assertNotIn('+ NEW',past)

    def test_rejected_native_and_text_plans_never_appear_as_applied_changes(self):
        invalid='ADD | 2 | ALGORITHM | | PROPOSED_ONLY\nNEEDS | 7 | 2\nFINISH'
        h=self.replan_harness([invalid,'Invalid plan prose',invalid])
        self.assertEqual(h.run(),2)
        self.assertEqual(h.state.next_id,7);self.assertNotIn(7,h.state.tasks)
        frames=read_history(self.session)
        for i in range(len(frames)):
            text=render(frames,index=i,width=240,color=False)
            self.assertIsNone(last_plan(frames,i))
            self.assertNotIn('REPLAN DIFF',text);self.assertNotIn('+ NEW',text)
            self.assertNotIn('PROPOSED_ONLY',text)
        rejected=next(i for i,(_,s) in enumerate(frames) if s.phase=='planning' and any('rejected:' in e for e in s.events))
        self.assertIn('children/needs prerequisites:',render(frames,index=rejected,width=240,color=False,details=True))

    def test_failed_commit_and_interrupted_planning_do_not_create_diff_frames(self):
        h=self.replan_harness(['ADD | 2 | ALGORITHM | | UNCOMMITTED_DRAFT\nFINISH',
                               'ADD | 2 | ALGORITHM | | Accepted repair\nADD | 5 | TEST | | Temporary draft\n'
                               'DROP | 8 | No longer needed\nFINISH'],interrupt=7)
        real_save=State.save
        def save(state,path):
            if any(t.summary=='UNCOMMITTED_DRAFT' for t in state.tasks.values()):raise OSError('Checkpoint write failed')
            real_save(state,path)
        with patch.object(State,'save',save),self.assertRaisesRegex(OSError,'Checkpoint write failed'):h.plan()
        self.assertEqual(h.state.next_id,7);self.assertEqual(State.load(self.path).next_id,7)
        self.assertTrue(all(t.summary!='UNCOMMITTED_DRAFT' for _,s in read_history(self.session) for t in s.tasks.values()))
        h=Harness(self.session,h.agent);h.state.recover();h.save();h.plan()  # Explicit resume after storage recovers.
        frames=read_history(self.session);commit=len(frames)-1
        self.assertEqual(h.state.next_id,9);self.assertEqual(h.state.tasks[7].summary,'Accepted repair')
        self.assertTrue(all(t.summary!='UNCOMMITTED_DRAFT' for _,s in frames for t in s.tasks.values()))
        self.assertIn('LAST PLAN · ADD #7',render(frames,width=240,color=False))
        plain=render(frames,width=240,color=False)
        retired=next(row for row in plain.splitlines() if '#8 ' in row and 'TEST' in row)
        self.assertIn('DROPPED',retired);self.assertNotIn('+ NEW',retired)
        parent=next(row for row in plain.splitlines() if '#5 ' in row and 'ALGORITHM' in row)
        self.assertNotIn('SPLIT',parent);self.assertNotIn('DROPPED',parent)
        h.step(h.state.eligible())  # First new worker is interrupted before any accepted report.
        self.assertEqual(h.state.tasks[7].status,'PENDING')
        self.assertNotIn('+ NEW',render(read_history(self.session),width=240,color=False))
        h.step(h.state.eligible())
        self.assertEqual(h.state.tasks[7].attempts,2)
        h.state.replans+=1;h.state.phase='planning';h.save()
        h.state.recover();h.save()
        frames=read_history(self.session)
        self.assertEqual(last_plan(frames,len(frames)-1),commit)
        self.assertNotIn('REPLAN DIFF',render(frames,width=240,color=False))
        self.assertIn('LAST PLAN · ADD #7',render(frames,index=commit,width=240,color=False))

    def deep_frames(self):
        scope='Repair \u6570\u636e CSV, e\u0301 and \U0001f469\u200d\U0001f4bb; '+('preserve useful data '*7)+'TAIL_SENTINEL'
        plan=('ADD | 0 | TEST | | Accept the complete repair | 4\n'
              'ADD | 0 | TEST | | Final acceptance | 1,3,4,5,6,7,8,9\n'
              'ADD | 1 | ALGORITHM | | Complete the implementation | 4\n'
              'ADD | 0 | ALGORITHM | | Prepare shared schema\n'
              'ADD | 3 | TEST | | Check parsing\nADD | 5 | ALGORITHM | | Repair input\n'
              'ADD | 6 | TEST | | Check malformed input\nADD | 7 | ALGORITHM | | Repair CSV rows\n'
              f'ADD | 8 | ALGORITHM | | {scope}\nFINISH')
        s=apply_plan(State(max_depth=7),parse_plan(plan));s.started=100;s.replans=2
        frames=[(100,copy.deepcopy(s))]
        running(s,4);frames.append((110,copy.deepcopy(s)))
        s=apply_report(s,4,report(4,summary='Shared schema is ready'));frames.append((130,copy.deepcopy(s)))
        running(s,9);frames.append((140,copy.deepcopy(s)))
        s=apply_report(s,9,report(9,'FAILED',reason='Row 17 rejects a valid refund; '+('inspect the CSV field '*8)))
        frames.append((170,copy.deepcopy(s)))
        return frames

    def test_reference_panel_has_flat_roots_short_labels_and_compact_plan_summary(self):
        planning=State(phase='planning',replans=1,started=100)
        state=apply_plan(planning,parse_plan(
            'ADD | 0 | TEST | tests/test_ledger.py | Ledger unit tests; FULL_SCOPE_ONLY_IN_DETAILS\n'
            'ADD | 0 | TEST | tests/test_cli.py | CLI checks\n'
            'ADD | 0 | ALGORITHM | ledger.py | Repair ledger\n'
            'ADD | 0 | ALGORITHM | miniledger.py | Repair CLI | 3\n'
            'ADD | 0 | TEST | | Final verification | 1,2,3,4\nFINISH'))
        active=copy.deepcopy(state);running(active,1);active.actions+=1
        frames=[(100,planning),(110,state),(324,active)]
        text=render(frames,now=428,width=120,color=False)
        rows=[row.strip() for row in text.splitlines()]
        self.assertTrue(rows[0].startswith('BACKTRACKIFY · LIVE TREE'))
        self.assertTrue(rows[0].endswith('LIVE · PLAN 1'))
        self.assertIn('Elapsed 5m 28s',text);self.assertIn('0/5 DONE · 0%',text)
        self.assertIn('▶ #1 TEST · RUNNING · Check test_ledger.py · 1m 44s · 1/3',text)
        self.assertIn('○ #4 ALGORITHM · PENDING · Repair miniledger.py · needs #3',text)
        self.assertIn('needs #1,#2,#3,#4',text)
        self.assertNotIn('├',text);self.assertNotIn('└',text)  # Roots are not fictitious children.
        self.assertIn('CURRENT · #1 · Check test_ledger.py',text);self.assertIn('attempt 1/3',text)
        self.assertIn('LAST PLAN · ADD #1,#2,#3,#4,#5',text)
        self.assertNotIn('FULL_SCOPE_ONLY_IN_DETAILS',text)
        self.assertNotIn('DFS #1',text);self.assertNotIn('try 0/3',text)
        expanded=render(frames,now=428,width=120,color=False,details=True)
        self.assertNotIn('FULL_SCOPE_ONLY_IN_DETAILS',expanded);self.assertNotIn('Errors',expanded)
        self.assertNotIn('DFS',expanded);self.assertIn('PLANNED EXECUTION ORDER',expanded)
        for terminal_width in (92,120,180):
            rows=render(frames,now=428,width=terminal_width,color=False).splitlines()
            self.assertTrue(all(columns(row)==terminal_width for row in rows))
            self.assertTrue(all(len(row)-len(row.lstrip())>=2 for row in rows))
            self.assertEqual(len(rows[1].strip()),88)  # The content does not stretch with the window.

    def test_panel_background_survives_colors_clipping_blank_rows_and_resize(self):
        frames=self.deep_frames()
        for width,height in ((120,40),(80,24),(46,14),(32,7),(12,3),(1,1)):
            with self.subTest(width=width,height=height):
                text=render(frames,width=width,now=180)
                screen,_=viewport(text,width,height)
                self.assertEqual(len(screen.splitlines()),height)
                self.assertTrue(all(columns(row)==width for row in screen.splitlines()))
                for row in screen.splitlines():
                    self.assertTrue(row.startswith(RESET))
                    for escape in re.findall(r'\x1b\[([0-9;]*)m',row):
                        self.assertNotEqual(escape,'0')  # A style reset must not expose a black patch.
                        if escape.startswith('0;'):self.assertIn(BACKGROUND,escape)
                self.assertNotIn('\x1b[48;5;236m',screen)
        live=render(frames[:-1],now=165,width=120)
        for code in (GREEN,CYAN,AMBER,MUTED):self.assertIn('\x1b['+code+'m',live)
        self.assertIn('\x1b['+RED+'m',render(frames,now=180,width=120))
        page,_=viewport(render(frames,width=120,color=False),120,40)
        lines=page.splitlines();nav=next(i for i,row in enumerate(lines) if 'Q Quit' in row)
        self.assertTrue(all(not row.strip() for row in lines[nav+1:]))  # Compact panel, not a bottom-anchored log.

    def test_narrow_history_keeps_mode_dfs_and_whole_dependency_ids(self):
        frames=self.deep_frames()
        text=render(frames,index=3,now=999,width=46,color=False)
        screen,_=viewport(text,46,10)
        self.assertIn('HISTORY 4/5',screen);self.assertIn('▶ #9',screen)
        self.assertIn('Q',screen)
        state=frames[3][1]
        self.assertEqual(state.tasks[9].status,'RUNNING')
        for width in (32,46,60):
            rows=render(frames,width=width,color=False).splitlines()
            row=next(row for row in rows if '#2 TEST' in row)
            if 'needs ' in row:
                self.assertRegex(row.strip(),r'needs (?:#\d+(?:,#\d+)*(?: \+\d+)?|\+\d+)$')

    def test_planned_order_is_the_real_scheduler_projection_and_stops_at_unresolved_work(self):
        state=apply_plan(State(),parse_plan(
            'ADD | 0 | TEST | | Check repaired module | 3\nADD | 0 | ALGORITHM | | Independent work\n'
            'ADD | 0 | ALGORITHM | module.py | Repair module\nADD | 3 | ALGORITHM | | Repair narrow input\nFINISH'))
        original=state.render();expected=['#4','#3','#1','#2']
        self.assertEqual(planned_order(state),expected);self.assertEqual(state.render(),original)
        calls=[]
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):calls.append(f'#{task_id}');return report(task_id)
        state.save(self.path);h=Harness(self.session,Agent());self.assertEqual(h.run(),0)
        self.assertEqual(calls,expected)
        reopened=apply_plan(h.state,parse_plan('REOPEN | 3 | A new requirement\nADD | 3 | ALGORITHM | | Focused correction\nFINISH'))
        reopened.save(self.path)
        self.assertEqual(planned_order(State.load(self.path)),['#5','#3','#1'])
        failed=apply_report(running(reopened,5),5,report(5,'FAILED',reason='A prerequisite is missing'))
        before=failed.render()
        self.assertEqual(planned_order(failed),['#5 [replan]']);self.assertEqual(before,failed.render())
        text=render([(100,failed)],width=120,color=False)
        self.assertIn('PLANNED EXECUTION ORDER · if reports succeed',text)
        self.assertNotIn('#5 → #3',text)

    def test_warning_history_distinguishes_failed_attempt_from_waiting_split_and_clean_success(self):
        h=self.replan_harness([self.split_plan()]);h.plan();frames=read_history(self.session)
        failed=next(i for i,(_,state) in enumerate(frames) if state.tasks[2].status=='FAILED')
        old=render(frames,index=failed,width=120,color=False,details=True)
        self.assertIn('#2 ALGORITHM · FAILED',old);self.assertIn('Cause: session turn limit',old)
        live=render(frames,width=120,color=False)
        self.assertIn('#2 ALGORITHM · WAITING',live)
        self.assertIn('Waiting for children · previous Turn limit',live)
        self.assertNotIn('#2 ALGORITHM · FAILED',live);self.assertNotIn('session turn limit',live)
        self.assertIn('Previous attempt: session turn limit',render(frames,width=120,color=False,details=True))
        row=next(row for row in live.splitlines() if '#3 ALGORITHM' in row)
        self.assertTrue(row.strip().startswith('└─'))  # Warning text must never corrupt a sibling's tree prefix.
        clean=apply_report(running(self.state(),1),1,report(1,reason='Informational success note'))
        text=render([(100,clean)],width=120,color=False,details=True)
        self.assertNotIn('D/Enter',text);self.assertNotIn('ERROR DETAILS',text)
        self.assertNotIn('Informational success note',text);self.assertIn('OK · #1',text)
        self.assertNotIn('+ NEW',text);self.assertNotIn('Clean current tree',text);self.assertNotIn('DFS',text)
        rejected=self.state();rejected.tasks[1].status='FAILED';rejected.tasks[1].attempts=1
        rejected.tasks[1].kind='protocol';rejected.tasks[1].reason='TEST may promote only its assigned artifact path: checks.py'
        text=render([(100,rejected)],width=120,color=False)
        self.assertIn('File scope rejected',text);self.assertNotIn('Invalid report',text)
        tiny,_=viewport(render([(100,rejected)],width=12,color=False),12,3)
        self.assertIn('D',tiny);self.assertIn('Q',tiny)

    def test_navigation_is_conditional_and_activity_does_not_invent_throughput(self):
        state=self.state();running(state,1)
        simple=render([(100,state)],now=500,width=120,color=False)
        page,_=viewport(simple,120,40)
        self.assertIn('Working · -- t/s',page);self.assertNotIn('Scroll',page);self.assertNotIn('D/Enter',page)
        self.assertEqual(task_name(state.tasks[1]),'Build')
        state.tasks[1].path='src/miniledger.py';state.tasks[1].summary='Repair miniledger.py: verbose instructions; lots more'
        self.assertEqual(task_name(state.tasks[1]),'Repair miniledger.py')
        self.assertIn('CURRENT · #1 · Repair miniledger.py',render([(100,state)],now=500,width=120,color=False))
        historical,_=viewport(render([(100,state)],index=0,now=999,width=120,color=False),120,40)
        self.assertNotIn('t/s',historical);self.assertIn('History',historical)
        frames=self.deep_frames()
        for width in (32,46,68,120):
            text=render(frames,width=width,color=False,details=True)
            page,_=viewport(text,width,16)
            self.assertIn('Q',page);self.assertTrue('Scroll' in page or '↑↓' in page)
            self.assertTrue(all(columns(row)==width for row in page.splitlines()))
        page,_=viewport(render(frames,width=120,color=False,details=True),120,24)
        self.assertIn('ERROR DETAILS',page);self.assertIn('Row 17 rejects',page)

    def test_throughput_uses_native_counts_for_current_attempt_only_and_never_history(self):
        state=self.state();running(state,1)
        turn=self.session/'turns/one';usage=turn/'native/usage/token-usage-2026-10.jsonl'
        init=dict(type='system',subtype='init',session_id='own',cwd=str(self.session/'reviews/1-1-owned'))
        atomic(turn/'qwen.log','warning before stream\n'+json.dumps(init)+'\n')
        row=dict(sessionId='own',source='main',outputTokens=450,thoughtsTokens=300,apiDurationMs=10000)
        foreign=dict(row,sessionId='foreign',outputTokens=9000)
        atomic(usage,json.dumps(row)+'\n'+json.dumps(foreign)+'\n{"partial":')
        before={str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()}
        self.assertEqual(throughput(self.session,state),45.0)  # Thoughts are already included, not added twice.
        screen,_=viewport(render([(100,state)],width=120,color=False,rate=45.0),120,40)
        self.assertIn('Working · last API 45.0 t/s',screen)
        self.assertNotIn('t/s',render([(100,state)],index=0,width=120,color=False,rate=45.0))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()})
        state.tasks[1].attempts=2;self.assertIsNone(throughput(self.session,state))
        state.tasks[1].attempts=1
        for invalid in (dict(row,apiDurationMs=0),dict(row,outputTokens=float('nan')),dict(row,outputTokens=True)):
            atomic(usage,json.dumps(invalid)+'\n');self.assertIsNone(throughput(self.session,state))
        state.tasks[1].status='PENDING';self.assertIsNone(throughput(self.session,state))

    def test_realistic_deep_tree_failure_details_and_completed_plan(self):
        frames=self.deep_frames();before=[state.render() for _,state in frames]
        live=render(frames[:-1],now=165,width=130)
        plain=re.sub(r'\x1b\[[0-9;]*m','',live)
        self.assertIn('1/9 DONE · 11%',plain);self.assertIn('25s',plain)
        self.assertIn('  └─',plain);self.assertIn('                 └─',plain)
        self.assertEqual(plain.count('▶'),1)
        selected=next(line for line in plain.splitlines() if '▶' in line)
        self.assertIn('#9',selected);self.assertIn('RUNNING',selected)
        for code in (GREEN,CYAN,AMBER,MUTED):self.assertIn(f'\x1b[{code}m',live)
        self.assertNotIn('\x1b[48;5;236m',live)
        self.assertNotIn('Shared schema is ready',plain);self.assertNotIn('EVENT |',plain)
        retained=copy.deepcopy(frames[2][1]);retained.events=[]
        self.assertIn('OK · #4',render([(130,retained)],now=150,color=False))
        self.assertNotIn('Shared schema is ready',render([(130,retained)],now=150,color=False,details=True))
        failed=render(frames,now=180,width=100)
        self.assertIn('\x1b['+RED+'m',failed);self.assertIn('Row 17 rejects a valid refund',failed)
        self.assertNotIn('TAIL_SENTINEL',failed)
        expanded=render(frames,now=180,width=100,color=False,details=True)
        self.assertIn('TAIL_SENTINEL',expanded)
        self.assertIn('needs #1,#3,#4,#5,#6,#7,#8,#9',expanded)
        self.assertIn('1/3',expanded);self.assertIn('Worker reported: Assigned work completed',expanded)
        blocked=copy.deepcopy(frames[-1][1]);blocked.block('Permission is required to continue')
        self.assertIn('STOP',render(frames+[(180,blocked)],color=False))
        self.assertIn('\x1b['+RED+'m',render(frames+[(180,blocked)]))
        s=apply_plan(frames[-1][1],parse_plan('EDIT | 9 | Finish the remaining CSV repair\nFINISH'))
        stamp=200
        while task:=s.eligible():
            running(s,task.id);frames.append((stamp,copy.deepcopy(s)))
            s=apply_report(s,task.id,report(task.id));stamp+=20;frames.append((stamp,copy.deepcopy(s)))
        s.phase='complete';s.validate();frames.append((stamp,s))
        self.assertIn('9/9 DONE · 100%',render(frames,now=999,color=False))
        self.assertEqual(before,[state.render() for _,state in frames[:len(before)]])

    def test_unicode_cell_widths_and_colored_truncation_do_not_split_glyphs(self):
        for text,size in [('CSV',3),('\u6570\u636e',4),('e\u0301',1),('\U0001f469\u200d\U0001f4bb',2),
                          ('\U0001f44d\U0001f3fd',2),('\u2764\ufe0f',2),('\U0001f1ed\U0001f1fa',2)]:
            with self.subTest(text=ascii(text)):
                self.assertEqual(columns(text),size)
                self.assertEqual(columns('\x1b[96m'+text+'\x1b[0m'),size)
        text='A e\u0301 \u6570\u636e \U0001f469\u200d\U0001f4bb end'
        for width in range(1,25):
            clipped=shorten('\x1b[96m'+text+'\x1b[0m',width)
            self.assertLessEqual(columns(clipped),width)
            plain=re.sub(r'\x1b\[[0-9;]*m','',clipped).removesuffix('…')
            self.assertFalse(plain.endswith('\u200d'))
            if '\x1b' in clipped:self.assertTrue(clipped.endswith((RESET,'\x1b[0m')))
        self.assertEqual(list(glyphs('e\u0301')) ,['e\u0301'])
        frames=self.deep_frames()
        for details in (False,True):
            for width in (1,12,24,32,48,80,120):
                text=render(frames,width=width,color=True,details=details)
                self.assertTrue(all(columns(line)<=width for line in text.splitlines()),(width,details))
                if width>=32:
                    plain=re.sub(r'\x1b\[[0-9;]*m','',text)
                    self.assertTrue(all(f'#{id} ' in plain for id in range(1,10)))

    def test_planning_card_and_resizing_keep_navigation_and_scrolling_visible(self):
        state=State(phase='planning',replans=1,started=100)
        planning=render([(110,state)],now=120,width=100,color=False)
        self.assertIn('Planning the first TODOs',planning)
        self.assertIn('0/0 DONE · 0%',planning);self.assertNotIn('100%',planning)
        self.assertIn('No workers delegated yet',planning);self.assertIn('Elapsed 20s',planning)
        text=render(self.deep_frames(),width=100,color=True,details=True)
        focused,position=viewport(render(self.deep_frames(),width=46,color=False),46,10)
        self.assertGreater(position,0);self.assertIn('#9',focused)
        self.assertRegex(focused,r'#9 ALG · FAIL · (?:.+ · )?30s · 1/3')
        screen,scroll=viewport(text,100,16,999)
        self.assertGreater(scroll,0);self.assertIn('Q Quit',screen)
        self.assertLessEqual(len(screen.splitlines()),16)
        resized,scroll=viewport(render(self.deep_frames(),width=32,details=True),32,7,scroll)
        self.assertLessEqual(len(resized.splitlines()),7);self.assertTrue(all(columns(row)<=32 for row in resized.splitlines()))
        self.assertIn('Q',resized.splitlines()[-1])
        enlarged,scroll=viewport(text,100,500,scroll)
        self.assertEqual(scroll,0);self.assertIn('#9',enlarged)
        for height in (1,3,6,12):
            screen,_=viewport(planning,100,height)
            self.assertLessEqual(len(screen.splitlines()),height)
            self.assertIn('Q',screen)

    def test_default_has_no_history_and_opt_in_captures_exact_checkpoints(self):
        s=self.state();s.save(self.path)
        self.assertFalse((self.session/'history').exists())
        (self.session/'history').mkdir();s.save(self.path);s.save(self.path)
        self.assertEqual(len(read_history(self.session)),1)
        running(s,1);s.save(self.path)
        s=apply_report(s,1,report(1));s.save(self.path)
        frames=read_history(self.session)
        self.assertEqual([f[1].tasks[1].status for f in frames],['PENDING','RUNNING','DONE'])
        self.assertEqual(frames[-1][1].render(),State.load(self.path).render())
        before={str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()}
        for index in (0,1,None):render(read_history(self.session),index,color=False)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()})

    def test_render_shows_real_durations_progress_dfs_relations_and_prompt_changes(self):
        s=self.state();s.started=100
        planning=copy.deepcopy(s);planning.phase='planning';planning.replans=2
        changed=apply_plan(planning,parse_plan('ADD | 1 | ALGORITHM | | Prepare\nFINISH'))
        active=copy.deepcopy(changed);running(active,3);active.actions+=1
        done=apply_report(copy.deepcopy(active),3,report(3,summary='Prepared current inputs'))
        frames=[(100,s),(105,planning),(110,changed),(120,active),(150,done)]
        live=render(frames[:-1],now=140,width=180)
        self.assertIn('Elapsed 40s',live)
        self.assertNotIn('DFS',live);self.assertNotIn('1>child:3',live)
        self.assertIn('#3 → #1 → #2',live)
        plain=re.sub(r'\x1b\[[0-9;]*m','',live)
        self.assertIn('20s',plain);self.assertIn('attempt 1/3',plain);self.assertIn('needs #1',plain)
        self.assertIn('\x1b['+CYAN+'m',live);self.assertIn('\x1b['+AMBER+'m',live)
        self.assertIn('LAST PLAN · ADD #3 · SPLIT #1',plain)
        expanded=render(frames[:-1],now=140,width=180,color=False,details=True)
        self.assertIn('ADD #3',expanded);self.assertIn('SPLIT #1',expanded)
        historical=render(frames,index=3,now=500,color=False,width=180)
        self.assertIn('HISTORY 4/5',historical);self.assertIn('Elapsed 20s',historical)
        self.assertIn('0s',historical);self.assertIn('attempt 1/3',historical);self.assertNotIn('\x1b',historical)
        final=render(frames,now=160,width=180)
        self.assertIn('1/3 DONE · 33%',final);self.assertIn('30s',final)
        self.assertNotIn('Prepared current inputs',final);self.assertIn('\x1b['+GREEN+'m',final)
        self.assertNotIn('Prepared current inputs',render(frames,now=160,width=180,color=False,details=True))
        self.assertEqual(active.tasks[1].status,'WAITING')  # Rendering never settles the original state.
        late=work_times([(120,active)],0,140)
        self.assertTrue(late[3].endswith('?'))

    def test_history_failure_never_rejects_an_accepted_worker_report(self):
        (self.session/'history').mkdir();self.state().save(self.path)
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):return report(task_id)
        h=Harness(self.session,Agent())
        with patch('visualize.record',side_effect=OSError('History disk unavailable')):
            self.assertEqual(h.run(),0)
        self.assertTrue(State.load(self.path).complete())
        self.assertEqual(read_history(self.session)[-1][1].phase,'complete')

    def test_optional_window_open_failure_cannot_prevent_normal_run(self):
        from harness import main
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):return report(task_id)
        for enabled in (False,True):
            with self.subTest(visualize=enabled):
                self.state().save(self.path)
                with patch('harness.gguf_metadata',return_value={'qwen35.context_length':65536,'qwen35.nextn_predict_layers':1}),\
                     patch('harness.backend_lock',return_value=nullcontext()),patch('harness.Server'),\
                     patch('harness.Qwen',return_value=Agent()),patch('harness.status') as messages,\
                     patch('visualize.open_view',side_effect=OSError('Cannot create terminal window')) as viewer,\
                     patch('harness.start_process',side_effect=AssertionError('CPU test must not launch inference')):
                    args=['run','--server','unused','--model','unused','--qwen','unused','--session',str(self.session)]
                    self.assertEqual(main(args+(['--visualize'] if enabled else [])),0)
                self.assertEqual(viewer.call_count,int(enabled));self.assertTrue(State.load(self.path).complete())
                warnings=[call.args for call in messages.call_args_list if call.args[:2]==('VIEW','WARN')]
                self.assertEqual(len(warnings),int(enabled))
                if enabled:self.assertIn('execution continues',warnings[0][2])

    def test_owner_exit_freezes_live_view_without_changing_stored_running_state(self):
        state=self.state();running(state,1);state.save(self.path)
        worker=start_process([sys.executable,'-c','import time; time.sleep(20)'])
        try:
            born=process_stamp(worker.pid);self.assertTrue(born)
            atomic(self.session/'view.log',f'OWNER | {worker.pid} | {born}\n')
            self.assertFalse(owner_ended(self.session))
            with patch('visualize.process_stamp',return_value='different-creation-time'):
                self.assertTrue(owner_ended(self.session))  # A recycled PID is not this run.
        finally:stop_process(worker)
        self.assertTrue(owner_ended(self.session))
        before=self.path.read_bytes();frames=read_history(self.session)
        text=render(frames,now=9999999999,width=120,color=False,ended=True,rate=45,details=True)
        self.assertIn('ENDED',text);self.assertIn('Run ended before report acceptance',text)
        self.assertNotIn('Working',text);self.assertNotIn('t/s',text)
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(State.load(self.path).tasks[1].status,'RUNNING')
        history=render(frames,index=0,width=120,color=False,ended=True)
        self.assertIn('HISTORY',history);self.assertNotIn('Run ended before report acceptance',history)

    def test_finished_view_automatically_follows_next_prompt_without_mixing_sessions(self):
        from visualize import main
        state=self.state()
        for id in (1,2):state=apply_report(running(state,id),id,report(id))
        state.phase='complete';state.save(self.path);before=self.path.read_bytes()
        other=self.session/'next-session';other.mkdir()
        next_state=apply_plan(State(),parse_plan('ADD | 0 | ALGORITHM | next.py | Second session only\nFINISH'))
        next_state.save(other/'state.log')
        with patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')):
            open_view(self.session)
        channel=view_channel(ROOT)
        self.assertEqual(following(channel),self.session.resolve())
        kernel=Mock();kernel.GetStdHandle.return_value=1;kernel.GetConsoleMode.return_value=1
        output=io.StringIO();keys=iter(['x','x','x','x','q']);count=[0]
        def key():
            value=next(keys)
            count[0]+=1
            if count[0]==2:
                self.assertTrue(viewer_active(channel))
                with patch('visualize.subprocess.run',side_effect=AssertionError('Reuse the existing viewer')):
                    open_view(other)
            elif count[0]==3:
                running(next_state,1);next_state.save(other/'state.log')
            return value
        with patch('visualize.ctypes.windll',Mock(kernel32=kernel)),patch('sys.stdout',output),\
             patch('msvcrt.kbhit',return_value=True),patch('msvcrt.getwch',side_effect=key),\
             patch('builtins.input',side_effect=AssertionError('No manual session switching')),patch('visualize.time.sleep') as sleep,\
             patch('visualize.owner_ended',return_value=True),\
             patch('visualize.shutil.get_terminal_size',return_value=os.terminal_size((120,40))):
            self.assertEqual(main([str(self.session),'--follow',str(channel)]),0)
        text=re.sub(r'\x1b\[[0-9;]*[mHKJ]','',output.getvalue())
        self.assertGreaterEqual(sleep.call_count,4);self.assertIn('ENDED',text)
        self.assertIn('next-session',text);self.assertIn('0/1 DONE',text)
        switched=text.rsplit('SESSION ·',1)[-1]
        self.assertIn('RUNNING',switched);self.assertNotIn('#2 TEST',switched)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(viewer_active(channel))  # Q releases only the viewer's environment lock.
        self.assertNotIn('A Attach',text)

    def test_terminal_and_project_isolation_and_rejected_follow_targets(self):
        self.state().save(self.path)
        channel=view_channel(ROOT)
        self.assertFalse(viewer_active(channel));self.assertFalse(channel.exists())
        with patch.dict(os.environ,WT_SESSION='another-terminal'):
            self.assertNotEqual(channel,view_channel(ROOT))
        self.assertNotEqual(channel,view_channel(self.session/'another-project'))
        with patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')):
            open_view(self.session)
        self.assertEqual(following(channel),self.session.resolve())
        with lock(channel.with_suffix('.lock')):
            self.assertTrue(viewer_active(channel))
            # Resume knows its previously registered project even when --project must be omitted.
            self.assertEqual(view_channel(self.session/'another-project',self.session),channel)
        self.assertFalse(viewer_active(channel))
        # A pointer is not permission to switch to a different environment or invalid TODO state.
        other=self.session/'unrelated';other.mkdir()
        self.state().save(other/'state.log');atomic(other/'view.log','ENV | unrelated-channel\n')
        atomic(channel,str(other))
        with self.assertRaisesRegex(ValueError,'environment'):following(channel)
        atomic(other/'view.log',f'ENV | {channel}\n');atomic(other/'state.log','incomplete write')
        with self.assertRaises(ValueError):following(channel)

    def test_completed_todos_are_distinct_from_live_gpu_acceptance_and_history(self):
        state=self.state()
        for id in (1,2):state=apply_report(running(state,id),id,report(id))
        state.phase='complete';state.save(self.path)
        frames=read_history(self.session)
        pending=render(frames,color=False,session_label='snapshot / longflow-example')
        self.assertIn('TODOs DONE',pending);self.assertIn('Acceptance pending',pending)
        for outcome in ('PASS','FAIL','BLOCKED'):
            atomic(self.session/'result.txt',outcome+' | nodes 2\nCOVERAGE | reopen | NOT EXERCISED\n')
            value=acceptance_result(self.session);self.assertEqual(value,outcome)
            live=render(frames,color=False,acceptance=value)
            self.assertIn('2/2 DONE',live);self.assertIn('GPU LONGFLOW · '+outcome,live)
            self.assertNotIn('GPU LONGFLOW',render(frames,index=0,color=False,acceptance=value))
        atomic(self.session/'result.txt','partial');self.assertEqual(acceptance_result(self.session),'')

    def test_progress_and_bar_require_successful_terminal_state(self):
        initial=State(phase='planning')
        state=apply_plan(State(),parse_plan('\n'.join(f'ADD | 0 | TEST | | Check {id}' for id in range(1,15))+'\nFINISH'))
        for id in range(1,14):state=apply_report(running(state,id),id,report(id))
        partial=copy.deepcopy(state)
        active=apply_report(running(state,14),14,report(14))
        complete=copy.deepcopy(active);complete.phase='complete';complete.validate()
        blocked=copy.deepcopy(complete);blocked.block('Acceptance failed after task reports')
        failed=copy.deepcopy(active);failed.closed=False
        failed=apply_report(running(failed,14),14,report(14,'FAILED',reason='A genuine release defect'))
        failed.block('Release defect remains unresolved')
        cases=[(initial,0,0,0),(partial,13,14,93),(active,14,14,99),
               (complete,14,14,100),(blocked,14,14,99),(failed,13,14,93)]
        for state,done,total,percent in cases:
            before=state.render()
            for width in (120,44,20):
                with self.subTest(phase=state.phase,done=done,width=width):
                    text=render([(state.started,state)],width=width,ended=True)
                    plain=re.sub(r'\x1b\[[0-9;]*m','',text)
                    if width>=44:self.assertIn(f'{done}/{total} DONE · {percent}%',plain)
                    bar=next(line for line in text.splitlines() if '━' in line)
                    fill=re.search(re.escape(f'\x1b[{GREEN}m')+'(━+)',bar)
                    self.assertEqual(len(fill[1]) if fill else 0,int(bar.count('━')*percent/100))
                    if width>=44:self.assertEqual('ENDED · OK ·' in plain,percent==100)
                    if percent==100 and width==120:self.assertIn(f'\x1b[{GREEN}mENDED · OK · TODOs DONE · accepted agent reports',text)
            self.assertEqual(state.render(),before)

    def test_persisted_longflow_acceptance_changes_99_to_100_without_changing_todos(self):
        (self.session/'history').mkdir()
        state=self.state()
        for id in (1,2):state=apply_report(running(state,id),id,report(id))
        state.save(self.path)  # All reports committed; final completion is still pending.
        state.phase='complete';state.save(self.path)
        frames=read_history(self.session);before=self.path.read_bytes()
        for outcome,percent in [('',99),('PASS',100),('FAIL',99),('BLOCKED',99)]:
            with self.subTest(outcome=outcome):
                atomic(self.session/'result.txt',outcome+' | coverage checked\n')
                text=render(frames,session_label='snapshot / longflow-example',acceptance=acceptance_result(self.session))
                plain=re.sub(r'\x1b\[[0-9;]*m','',text)
                self.assertIn(f'2/2 DONE · {percent}%',plain)
                self.assertEqual('ENDED · OK ·' in plain,outcome=='PASS')
                bar=next(line for line in text.splitlines() if '━' in line)
                self.assertEqual(bar.count(f'\x1b[{GREEN}m'),1)
                self.assertIn('━'*88,re.sub(r'\x1b\[[0-9;]*m','',bar))
                filled=re.search(re.escape(f'\x1b[{GREEN}m')+'(━+)',bar)[1]
                self.assertEqual(len(filled),int(88*percent/100))
                if outcome:self.assertIn('GPU LONGFLOW · '+outcome,plain)
                else:self.assertIn('Acceptance pending',plain)
        self.assertEqual(self.path.read_bytes(),before)

    def test_history_never_credits_earlier_release_with_later_gpu_acceptance(self):
        state=self.state()
        for id in (1,2):state=apply_report(running(state,id),id,report(id))
        state.phase='complete';frames=[(100,copy.deepcopy(state))]
        state=apply_plan(state,parse_plan('REOPEN | 1 | Follow-up changes the prerequisite\nFINISH'))
        frames.append((110,copy.deepcopy(state)))
        for id in (1,2):state=apply_report(running(state,id),id,report(id))
        state.phase='complete';frames.append((120,state))
        for index,percent in [(0,99),(1,0),(2,100),(None,100)]:
            with self.subTest(index=index):
                text=render(frames,index=index,color=False,session_label='snapshot / longflow-example',acceptance='PASS')
                self.assertIn(f'DONE · {percent}%',text)
                self.assertEqual('ENDED · OK ·' in text,percent==100)
                self.assertEqual('GPU LONGFLOW · PASS' in text,index is None)
        # Ordinary sessions already had an authoritative completion at that point.
        self.assertIn('2/2 DONE · 100%',render(frames,index=0,color=False))

    def test_successive_run_commands_reuse_only_a_live_environment_viewer(self):
        from harness import main
        class Agent:
            def call(_,role,prompt,workspace,task_id=None,**kwargs):return report(task_id)
        with patch('harness.gguf_metadata',return_value={'qwen35.context_length':65536,'qwen35.nextn_predict_layers':1}),\
             patch('harness.backend_lock',return_value=nullcontext()),patch('harness.Server'),\
             patch('harness.Qwen',return_value=Agent()),\
             patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as launch,\
             patch('harness.start_process',side_effect=AssertionError('No inference in CPU tests')):
            command=['run','--server','unused','--model','unused','--qwen','unused','--session',str(self.session)]
            self.state().save(self.path)
            self.assertEqual(main(command+['--visualize']),0)
            channel=view_channel(ROOT)
            with lock(channel.with_suffix('.lock')):
                self.state().save(self.path)
                self.assertEqual(main(command),0)  # No repeated --visualize is necessary.
            self.assertEqual(launch.call_count,1)
            (self.session/'view.log').unlink()
            self.state().save(self.path);self.assertEqual(main(command),0)
            self.assertFalse((self.session/'view.log').exists())  # Explicit Q is respected.

    def test_failure_restart_retains_history_and_no_summary_can_inject_terminal_controls(self):
        (self.session/'history').mkdir();s=self.state();running(s,1);s.save(self.path)
        restored=State.load(self.path);restored.recover();restored.save(self.path)
        text=render(read_history(self.session),color=False,width=180)
        self.assertIn('FAILED',text);self.assertIn('Interrupted',text);self.assertIn('attempt 1/3',text)
        self.assertEqual(restored.tasks[2].needs,[1]);self.assertEqual(restored.eligible(),None)
        restored.tasks[1].summary='Bad \x1b[2J text';restored.save(self.path)
        self.assertNotIn('\x1b',render(read_history(self.session),color=False))

    @unittest.skipUnless(os.name=='nt','Windows separate console flags')
    def test_separate_console_and_viewer_exit_never_stop_the_worker(self):
        self.state().save(self.path)
        with patch('visualize.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as launch:
            open_view(self.session)
        command=launch.call_args.args[0]
        script=base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn(str(self.session.resolve()),script);self.assertIn('visualize.py',script)
        self.assertIn('FindWindowSW',script);self.assertIn('.Document.Application.ShellExecute',script)
        self.assertEqual(launch.call_args.kwargs['creationflags'],subprocess.CREATE_NO_WINDOW)
        before={str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()}
        worker=start_process([sys.executable,'-c','import time; time.sleep(20)'])
        try:
            reader=subprocess.run([sys.executable,'-B',str(ROOT/'visualize.py'),str(self.session),'--once'],
                                  capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(reader.returncode,0,reader.stderr)
            self.assertIn('BACKTRACKIFY · LIVE TREE',reader.stdout);self.assertIsNone(worker.poll())
        finally:stop_process(worker)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.session.rglob('*') if p.is_file()})

    @unittest.skipUnless(os.name=='nt','Windows console keyboard')
    def test_keyboard_browses_history_and_returns_live_without_writing_state(self):
        from visualize import main
        s=self.state();s.save(self.path)
        active=copy.deepcopy(s);running(active,1)
        active.tasks[1].kind='loop';active.tasks[1].reason='Repeated model output in previous attempt'
        frames=[(100,s),(120,active)]
        kernel=Mock();kernel.GetStdHandle.return_value=1;kernel.GetConsoleMode.return_value=1
        output=io.StringIO()
        # First redraw, then Left, Right, Left, L and Q; no real window is opened.
        keys=['x','\xe0','K','\xe0','M','\xe0','K','\r','\xe0','P','l','d','q']
        sizes=iter(os.terminal_size(size) for size in [(100,24),(48,10),(32,6),(100,24)])
        before=self.path.read_bytes()
        with patch('visualize.ctypes.windll',Mock(kernel32=kernel)),patch('sys.stdout',output),\
             patch('msvcrt.kbhit',return_value=True),patch('msvcrt.getwch',side_effect=keys),\
             patch('visualize.read_history',return_value=frames),patch('visualize.time.sleep'),\
             patch('visualize.shutil.get_terminal_size',side_effect=lambda *_:next(sizes,os.terminal_size((100,24)))),\
             patch('visualize.atomic',side_effect=AssertionError('Viewer must not write')):
            self.assertEqual(main([str(self.session)]),0)
        text=output.getvalue()
        self.assertIn('HISTORY 1/2',text);self.assertIn('LIVE · PLAN 0',text)
        self.assertGreater(text.count('LIVE · PLAN 0'),1)
        self.assertIn('Repeated model output in previous attempt',text);self.assertIn('\x1b[?25h',text)
        self.assertIn('D/Enter Less',text)
        self.assertEqual(kernel.SetConsoleMode.call_count,2)
        self.assertEqual(self.path.read_bytes(),before)
