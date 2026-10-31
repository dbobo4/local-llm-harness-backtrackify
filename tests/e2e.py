"""Genuine quantized-model acceptance; called only inside a Desktop snapshot."""
import os
from pathlib import Path
import uuid

from harness import (ROOT, PROFILES, Harness, Qwen, Refused, State, Task, apply_report, atomic,
                     gguf_metadata, status)
from chat import Conversation


def require_snapshot():
    base = os.environ.get('BACKTRACKIFY_TEST_ROOT')
    if not base or Path(base).name != 'Backtrackify-TEST' or Path(base).resolve() not in ROOT.parents:
        raise Refused('Developer GPU E2E requires a fresh Desktop\\Backtrackify-TEST snapshot. Run powershell -File test.ps1 -Gpu -Install.')


def check(condition, detail):
    if not condition:
        raise Refused(detail)


def check_probe_report(report):
    task=Task(1,0,'ALGORITHM','probe.txt','Reasoning cutoff probe',status='RUNNING',attempts=1)
    state=apply_report(State(tasks={1:task},next_id=2,phase='ready'),1,report)
    check(state.tasks[1].status=='DONE' and len(state.tasks[1].result)>20,
          'Reasoning probe omitted a useful structured final report')


def smoke(args, server):
    require_snapshot()
    evidence = []
    def passed(name, detail):
        evidence.append(f'PASS | {name} | {detail}')
        atomic(ROOT / 'sessions' / 'acceptance.txt', '\n'.join(evidence) + '\n')
        status('E2E', 'PASS', name + ' | ' + detail)
    try:
        meta = gguf_metadata(args.model)
        check(args.context <= meta['qwen35.context_length'], 'Configured context exceeds GGUF metadata')
        passed('model-load', f'{args.profile} | qwen35 | context {args.context}')
        session = ROOT / 'sessions' / ('e2e-' + uuid.uuid4().hex[:8])
        workspace = session / 'workspace'
        workspace.mkdir(parents=True)
        goal = ('Implement calc.py with add(a,b) returning arithmetic addition. Preserve inputs. '
                'Independent standard-library unittest tests in tests/test_calc.py must check positive, negative and zero values. '
                'Begin by verifying the existing calc.py before repairs. Report FAILED for defects. Do not weaken tests. Plan repairs and fresh TEST verification when needed.')
        atomic(session / 'goal.txt', goal)
        atomic(session / 'memory.txt', 'Constraint: standard library only. Preserve arithmetic addition.\n')
        State(max_depth=PROFILES[args.profile][3]).save(session / 'state.log')
        qwen = Qwen(args.qwen,args.profile,args.context,server.url,session,getattr(args,'agent_seconds',None))
        h = Harness(session,qwen)
        atomic(workspace/'calc.py','def add(a, b):\n    return a - b\n')
        check(h.run()==0,'Native delegated workflow did not complete')
        check(any('report FAILED' in e for e in h.state.events),'No worker failure/backtracking observed')
        check(h.state.complete(),'Accepted reports did not complete the TODO')
        count=len(qwen.session_ids)
        check(Harness(session,qwen).run()==0 and len(qwen.session_ids)==count,'Restart replayed completed work')
        passed('planning/backtracking/restart','Model plan and structured worker reports; no replay or harness project commands')

        # Force a tiny native reasoning budget, then demand an actual file-tool call.
        probe = session/'reasoning-probe';probe.mkdir()
        before_log = server.log.stat().st_size
        report = qwen.call('ALGORITHM',
            f'Reason carefully through fifty steps about a robust parser before acting. Then use write_file to create {probe / "probe.txt"} '
            'with the exact text reasoning cutoff still allows tools. Finish through structured_output.report with task_id 1, status DONE, and a useful one-sentence summary.',
            probe,thinking_override=32,task_id=1)
        check((probe/'probe.txt').is_file() and 'reasoning cutoff' in (probe/'probe.txt').read_text(encoding='utf-8'),'Reasoning probe did not execute the native file tool')
        check_probe_report(report)
        with server.log.open('rb') as log:
            log.seek(before_log);trace=log.read().decode('utf-8',errors='replace')
        check('budget exhausted, forcing end sequence' in trace or
              ('budget exhausted, waiting for UTF-8 completion' in trace and 'UTF-8 complete, now forcing end sequence' in trace),
              'Native sampler budget exhaustion not observed; cannot claim cutoff PASS')
        check(any(c['thinking']==32 for c in qwen.calls),'Thinking override did not reach the native wire')
        passed('reasoning cutoff','Native sampler exhaustion observed at budget 32; actual file tool and useful final output followed')

        # Task-boundary rollover uses distinct native Qwen session IDs and durable goal/state.
        check(len(qwen.session_ids)==len(set(qwen.session_ids)) and len(qwen.session_ids)>=4,'Qwen did not use independent fresh contexts')
        passed('task-boundary rollover',f'{len(qwen.session_ids)} independent native contexts; persisted goal, plan and memory')
        chat=Conversation(session/'chat.txt',server.url,args.context)
        chat.goal='PROJECT_ORCHID: implement addition. Constraint: standard library only.'
        chat.turns=[dict(role='user' if i%2==0 else 'assistant',content=f'PROJECT_ORCHID finding {i}: addition should preserve negative inputs.') for i in range(8)]
        recent=chat.turns[-2:].copy();chat.compact(3072)
        restored=Conversation(session/'chat.txt',server.url,args.context)
        check(restored.goal==chat.goal and restored.turns==recent and restored.rollovers==1,'Chat rollover lost original goal or recent turns')
        answer=restored.send('What is the project marker? Include the exact marker.')
        check('PROJECT_ORCHID' in answer['turns'][-1]['content'],'Chat summary/rollover lost the project marker')
        passed('chat compaction','Actual model summary; original goal/recent turns persist after reload and continuation')
        return 0
    except (OSError, ValueError) as e:
        evidence.append('FAIL | ' + str(e))
        atomic(ROOT/'sessions'/'acceptance.txt','\n'.join(evidence)+'\n')
        status('E2E','FAIL',str(e))
        return 1
