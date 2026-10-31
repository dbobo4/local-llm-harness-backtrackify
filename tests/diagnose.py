"""Replay one untouched failed worker call in a fresh snapshot; no tuning or project checks."""
import copy
import json
from pathlib import Path
import re
import time
import uuid
from harness import Qwen, ROOT, State, Refused, atomic, clone, files, generation, apply_report, status
from tests.e2e import require_snapshot, check

def native_rows(path):
    rows=[]
    if not path.exists(): return rows
    for line in path.read_text(encoding='utf-8',errors='replace').splitlines():
        try: value=json.loads(line)
        except ValueError: continue
        if isinstance(value,dict): rows.append(value)
    return rows


def select_source(snapshot):
    """Replay an untouched delegated worker call, never guess its pre-edit files."""
    snapshot=snapshot.resolve()
    check(snapshot.parent.name=='Backtrackify-TEST','Source must be a Backtrackify-TEST snapshot')
    for session in sorted((snapshot/'sessions').glob('longflow-*')):
        state=State.load(session/'state.log')
        for turn in sorted((session/'turns').iterdir()):
            rows=native_rows(turn/'qwen.log')
            result=next((r for r in reversed(rows) if r.get('type')=='result'),{})
            if result.get('is_error') is not True: continue
            transcript=next((p for p in turn.rglob('*.jsonl') if 'chats' in p.parts),None)
            if not transcript: continue
            user=next((r for r in native_rows(transcript) if r.get('type')=='user'),{})
            prompt='\n'.join(p.get('text','') for p in user.get('message',{}).get('parts',[]))
            match=re.search(r'Assigned task (\d+): ',prompt)
            if not match: continue
            id=int(match[1]);check(id in state.tasks,'Unknown delegated task')
            role=state.tasks[id].role
            parts=[p for row in rows for p in row.get('message',{}).get('content',[]) if isinstance(p,dict)]
            check(not any(p.get('type')=='tool_use' and p.get('name') not in {'read_file','glob','grep_search'} for p in parts),
                  'Source call had potential side effects; exact pre-call files are unavailable')
            review=Path(user['cwd']).resolve()
            check(review.parent==session/'reviews','Source escaped its snapshot')
            check(review.is_dir(),'Source review no longer exists')
            check(all(t.status in {'DONE','DROPPED'} for t in state.tasks.values() if t.parent==id),
                  'Cannot replay an old parent call after its TODO was split')
            config=json.loads((turn/'settings.json').read_text(encoding='utf-8'))['modelProviders']['openai'][0]['generationConfig']
            return dict(snapshot=snapshot,session=session,turn=turn,workspace=review,prompt=prompt,task_id=id,role=role,
                        system=(snapshot/'agents'/f'{role.lower()}.md').read_text(encoding='utf-8'),config=config,state=state)
    raise Refused('No safely replayable untouched failed worker call found')



def sse_chunks(path):
    chunks=[]
    for line in path.read_bytes().splitlines():
        if not line.startswith(b'data:'): continue
        try: value=json.loads(line[5:].strip())
        except ValueError: continue
        if isinstance(value,dict): chunks.append(value)
    return chunks



def diagnose(args,server):
    require_snapshot()
    source=select_source(args.source_snapshot)
    thinking,output=generation(args.profile,source['role'],args.context)
    config=source['config']
    check(config['contextWindowSize']==args.context and
          config['extra_body']['thinking_budget_tokens']==thinking and config.get('samplingParams',{}).get('max_tokens',config['extra_body'].get('max_tokens'))==output,
          'Replay requires the original finite profile and context')
    session=ROOT/'sessions'/('diagnose-'+uuid.uuid4().hex[:8]);workspace=session/'workspace'
    original=clone(source['workspace'],workspace)
    prompt=source['prompt'].replace(str(source['workspace']),str(workspace)).replace(source['workspace'].as_posix(),workspace.as_posix())
    check(str(source['snapshot']) not in prompt and source['snapshot'].as_posix() not in prompt,'Prompt still references source snapshot')
    atomic(session/'source.txt',str(source['turn'])+'\n')
    qwen=Qwen(args.qwen,args.profile,args.context,server.url,session,getattr(args,'agent_seconds',None))
    started=time.monotonic()
    try:
        report=qwen.call(source['role'],prompt,workspace,task_id=source['task_id'],system_prompt=source['system'],trace=True)
        state=copy.deepcopy(source['state']);state.phase='ready';state.reason=''
        task=state.tasks[source['task_id']];task.status='RUNNING';task.attempts=max(1,task.attempts)
        apply_report(state,task.id,report)
        result=report['status']+' | '+report['summary']+' | '+report.get('reason','')
        code=0
    except (OSError,ValueError) as e:
        result='BLOCKED | '+getattr(e,'kind',type(e).__name__)+' | '+str(e)
        code=2
    check(files(source['workspace'])==original,'Source snapshot changed during replay')
    atomic(session/'result.txt',result+f' | seconds {time.monotonic()-started:.1f}\n')
    status('DIAG','MEASURED',result+' | '+str(session))
    return code
