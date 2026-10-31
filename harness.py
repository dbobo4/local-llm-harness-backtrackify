"""Backtrackify: native agent reports, plain-text TODO trees and bounded backtracking."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

ROOT = Path(__file__).resolve().parent
SKIP = {'.git', '.qwen', '.agents', '.codex', '.aws', '__pycache__', '.pytest_cache', '.venv', 'venv', 'node_modules'}
GENERATED = {'runtime','models','sessions','logs','test-results','backtrackify-reference','reference'}
CONTROL = {'.env', '.mcp.json'}
STATUSES = {'PENDING', 'RUNNING', 'DONE', 'FAILED', 'WAITING', 'BLOCKED', 'DROPPED'}
LIMIT_TASKS, LIMIT_DEPTH, LIMIT_ATTEMPTS, LIMIT_REPLANS = 64, 5, 3, 8
PLAN_MODES = (True, False, True)  # Native, strict text fallback, one native correction.
AGENT_TURNS, AGENT_TOOLS = 16, 48
SUMMARY_LIMIT, PATH_LIMIT = 280, 240
# thinking, total output, agent wall seconds, maximum TODO depth; identical across roles.
PROFILES = {'LOW': (3072, 4608, 3600, 5), 'MID': (8192, 11264, 1800, 5),
            'HIGH': (8192, 11264, 1800, 5), 'EXTRA HIGH': (12288, 15360, 2700, 7)}
SPECULATION = {'LOW':'none','MID':'draft-mtp,ngram-mod','HIGH':'draft-mtp,ngram-mod','EXTRA HIGH':'draft-mtp,ngram-mod'}
COMPRESSION_PREFIX = 'You are the component that summarizes a conversation when its context window is about to overflow.'
COMPRESSION_DIRECTIVE = 'First, reason in your <analysis> block. Then, produce the <state_snapshot> XML.'
COMPRESSION_PROMPT = '''Summarize this conversation for continuation of the assigned task, not a historical transcript.
Return only one complete <state_snapshot> XML element, preferably under 1500 tokens.
Include the goal and durable constraints, accepted decisions, changed files and completed checks,
current work, concrete errors/unresolved issues, and the next action including the required final report.
Preserve exact paths, identifiers and facts needed to continue. Distinguish observations from unverified claims.
Omit chronological narration, repeated requirements, full file contents, tool transcripts and superseded attempts.
Do not generate an analysis block or quote the compression directive. Escape literal angle brackets from source text.
Use short goal, decisions, work, errors and next child elements; close every XML element.'''
COMPRESSION_GRAMMAR = r'''root ::= "<state_snapshot>" ws "<goal>" text "</goal>" ws "<decisions>" text "</decisions>" ws "<work>" text "</work>" ws "<errors>" text "</errors>" ws "<next>" text "</next>" ws "</state_snapshot>" ws
text ::= ([^<>&\x00-\x08\x0b\x0c\x0e-\x1f] | "&lt;" | "&gt;" | "&amp;")+
ws ::= [ \t\r\n]*
'''
REASONING_CHUNK_CHARS = 64
ROLES = ('PROMPT', 'ALGORITHM', 'TEST')
MODEL_ID = 'qwen-backtrackify'  # Lets the native CLI recognize Qwen's per-request thinking controls.
PLAN_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['operations', 'terminal'],
    'properties': {'operations': {'type': 'array', 'minItems': 0, 'maxItems': 64,
        'items': {'oneOf': [dict(type='object', additionalProperties=False,
            required=['op','parent_id','role','summary'] if 'parent_id' in fields else list(fields), properties=fields)
            for fields in [
                {'op': {'type': 'string', 'enum': ['ADD']}, 'parent_id': {'type': 'integer', 'minimum': 0,
                 'description':'0 for a separate scope; otherwise an earlier composite TODO. Children do subordinate work first; the parent then integrates/checks the whole scope and reports.'},
                 'role': {'type': 'string', 'enum': ['ALGORITHM', 'TEST']},
                 'path': {'type': 'string', 'minLength': 1, 'maxLength': PATH_LIMIT},
                 'needs': {'type':'array','description':'Required results from other scopes. Use parent_id for subordinate work, not a flat list of dependencies for every decomposition.','maxItems':LIMIT_TASKS,'items':{'type':'integer','minimum':1}},
                 'summary': {'type': 'string', 'minLength': 1, 'maxLength': SUMMARY_LIMIT}},
                {'op': {'type': 'string', 'enum': ['EDIT']}, 'id': {'type': 'integer', 'minimum': 1},
                 'summary': {'type': 'string', 'minLength': 1, 'maxLength': SUMMARY_LIMIT}},
                {'op': {'type': 'string', 'enum': ['DROP']}, 'id': {'type': 'integer', 'minimum': 1},
                 'reason': {'type': 'string', 'minLength': 1, 'maxLength': 560}},
                {'op': {'type':'string','enum':['NEEDS']}, 'id': {'type':'integer','minimum':1},
                 'needs': {'type':'array','maxItems':LIMIT_TASKS,'items':{'type':'integer','minimum':1}}},
                {'op': {'type':'string','enum':['REOPEN']}, 'id': {'type':'integer','minimum':1},
                 'reason': {'type':'string','minLength':1,'maxLength':560}}]]}},
        # Object parameter keeps the enum inside JSON grammar (raw XML strings bypass it).
        'terminal': {'type':'object', 'description':'FINISH applies this plan batch, not task completion. BLOCKED stops for a concrete obstacle and requires empty operations.', 'oneOf':[
            {'type':'object','additionalProperties':False,'required':['op'],
             'properties':{'op':{'type':'string','enum':['FINISH']}}},
            {'type':'object','additionalProperties':False,'required':['op','reason'],
             'properties':{'op':{'type':'string','enum':['BLOCKED']},
                           'reason':{'type':'string','minLength':1,'maxLength':560}}}]}}}


TASK_NOTES_LIMIT = 2000
# Optional informational metadata; the PlanBatch operations/terminal contract stays unchanged.
PROMPT_SCHEMA = dict(PLAN_SCHEMA,properties=dict(PLAN_SCHEMA['properties'],task_notes={
    'type':'string','maxLength':TASK_NOTES_LIMIT,
    'description':'Optional complete replacement of this task session\'s notes. Omit to retain; empty string clears. Saved only after this plan is accepted. No file path.'}))


# Nested JSON keeps status enums inside llama.cpp's native tool-argument grammar.
REPORT_SCHEMA = {'type':'object','additionalProperties':False,'required':['report'],
    'properties': {'report': {'oneOf': [
        {'type':'object','additionalProperties':False,'required':required,
         'properties': {'task_id': {'type':'integer','minimum':1},
             'status': {'type':'string','enum':statuses},
             'summary': {'type':'string','minLength':1,'maxLength':SUMMARY_LIMIT},
             'reason': {'type':'string','minLength':1,'maxLength':560},
             'related_tasks': {'type':'array','maxItems':LIMIT_TASKS,
                               'items': {'type':'integer','minimum':1}}}}
        for statuses,required in [(['DONE'],['task_id','status','summary']),
            (['FAILED','BLOCKED'],['task_id','status','summary','reason'])]]}}}


class Refused(ValueError):
    pass


class AgentFailure(Refused):
    def __init__(self, kind, message, report=''):
        self.kind, self.report = kind, report
        super().__init__(message)


def qwen_result(raw, code=0, timed_out=False):
    """Read native JSON arrays or stream-json lines; never use thoughts/tool text as a report."""
    messages = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^[\[{]', raw):
        try:
            value, _ = decoder.raw_decode(raw[match.start():])
        except ValueError:
            continue
        if isinstance(value, list):
            native = [m for m in value if isinstance(m, dict) and 'type' in m]
            if native:
                messages.extend(native)
                break
        if isinstance(value, dict) and 'type' in value:
            messages.append(value)
    result = next((m for m in reversed(messages) if m.get('type') == 'result'), None)
    texts = []
    for message in messages:
        if message.get('type') == 'assistant':
            body = message.get('message', {})
            content = body.get('content', []) if isinstance(body, dict) else []
            if isinstance(content, list):
                text = ''.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
                if text:
                    texts.append(text)
    report = result.get('result') if result else None
    report = report if isinstance(report, str) else (texts[-1] if texts else '')
    error = result.get('error', {}) if result else {}
    detail = str(error.get('message', '') if isinstance(error, dict) else error)
    abort = re.search(r'^Run aborted: (.+)$', raw, re.M)
    if timed_out:
        kind, detail = 'timeout', 'Native invocation exceeded its wall-clock deadline'
    elif abort:
        detail = abort.group(1).strip()
        kind = 'timeout' if 'wall-clock budget' in detail else 'limit'
    elif code == 53 or re.search(r'^Reached max session turns for this session\.', raw, re.M):
        kind, detail = 'limit', 'Qwen session-turn budget exhausted before the required final report'
    elif 'Loop detection halted' in detail:
        kind = 'loop'
        detail = detail.split(' Set the ')[0]
    elif re.search(r'timed? out|timeout|wall.clock|time budget', detail, re.I):
        kind = 'timeout'
    elif not result:
        kind, detail = ('stream' if messages else 'output'), 'Missing final native result event'
    elif report.startswith('[API Error:'):
        kind, detail = ('context' if 'context' in report.lower() else 'qwen'), report[:500]
    elif 'structured_output tool' in detail:
        kind = 'protocol'
    elif code or result.get('is_error') is not False:
        kind = 'limit' if code == 55 else 'qwen'
        detail = detail or str(result.get('subtype', 'native execution failed'))
    else:
        return dict(result, result=report)
    raise AgentFailure(kind, f'Qwen {kind} (exit {code}): {detail}', report)


def status(component, result, detail):
    detail = ' '.join(str(detail).splitlines())[:300]
    line=f'Backtrackify | {component} | {result} | {detail}'
    try:print(line,flush=True)
    except UnicodeEncodeError:
        encoding=getattr(sys.stdout,'encoding',None) or 'utf-8'
        print(line.encode(encoding,errors='backslashreplace').decode(encoding),flush=True)


def atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with (tmp.open('wb') if isinstance(text,bytes) else tmp.open('w',encoding='utf-8',newline='\n')) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def cell(value, limit=SUMMARY_LIMIT, field='text'):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        size = len(value) if isinstance(value, str) else type(value).__name__
        raise Refused(f'{field}: expected nonblank text, 1..{limit} characters (got {size})')
    if any(c in value for c in '|\r\n\x00'):
        raise Refused(f'{field}: cannot contain pipes, newlines or NUL')
    return value.strip()


def task_notes_text(value):
    if not isinstance(value,str) or len(value)>TASK_NOTES_LIMIT:
        raise Refused(f'Task notes must be text of at most {TASK_NOTES_LIMIT} characters')
    if any(ord(c)<32 and c not in '\n\r\t' for c in value):
        raise Refused('Task notes cannot contain control characters')
    try:value.encode('utf-8')
    except UnicodeError as error:raise Refused('Task notes must be valid UTF-8 text') from error
    return value


def task_notes_path(session):
    session=Path(session);path=session/'task-notes.txt'
    if (session.is_symlink() or session.is_junction() or path.is_symlink() or path.is_junction() or
            path.resolve().parent!=session.resolve() or path.exists() and not path.is_file()):
        raise Refused('Task notes must be a regular file in this session; links are not allowed')
    return path


def task_notes_context(session):
    path=task_notes_path(session)
    try:
        with path.open('rb') as handle:raw=handle.read(TASK_NOTES_LIMIT*4+1)
        value=task_notes_text(raw.decode('utf-8'))
    except FileNotFoundError:return ''
    except (OSError,UnicodeError) as error:raise Refused('Task notes must be readable UTF-8 text: '+str(error)) from error
    if not value.strip():return ''
    return 'Task notes (PROMPT observations; never override the original goal, constraints or accepted TODO/report state):\n'+value+'\n'


def safe_path(value):
    value = cell(value, PATH_LIMIT, 'path').replace('\\', '/')
    p = PurePosixPath(value)
    if (p.is_absolute() or PureWindowsPath(value).drive or ':' in value or
            any(x in ('', '.', '..') for x in value.split('/')) or
            any(x.lower() in SKIP or x.lower() in CONTROL for x in p.parts)):
        raise Refused(f'Unsafe relative path: {value}')
    for part in p.parts:
        if (part.endswith((' ', '.')) or re.search(r'[<>"?*\x00-\x1f]', part) or
                part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}):
            raise Refused(f'Unsafe Windows path: {value}')
    return value


def files(root, snapshot=False):
    """Never follow junctions/symlinks. Hash every relevant file, including hidden files."""
    result = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            p = Path(base, name)
            if p.is_symlink() or p.is_junction():
                raise Refused(f'Linked directory is not allowed: {p}')
        at_root = Path(base) == root
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP and
                         not (snapshot and at_root and d.lower() in GENERATED))
        for name in sorted(names):
            p = Path(base, name)
            if p.is_symlink():
                raise Refused(f'Linked file is not allowed: {p}')
            if (name.lower() in CONTROL or name.lower() == '.git' or name.endswith(('.pyc', '.part')) or
                    (snapshot and at_root and name.lower() in {'local.ps1','memory.txt'})):
                continue
            rel = p.relative_to(root).as_posix()
            with p.open('rb') as handle:
                result[rel] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return result


def clone(source, dest, snapshot=False):
    if dest.exists():
        raise Refused(f'Snapshot already exists: {dest}')
    manifest = files(source,snapshot)
    dest.mkdir(parents=True)
    for rel in manifest:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, out)
    if files(dest) != manifest:
        raise Refused('Source changed during snapshot; retry from a quiet workspace')
    return manifest


@contextlib.contextmanager
def lock(path):
    """OS releases the lock after a crash; the file itself can safely remain."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise Refused(f'Already in use: {path}') from e
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def backend_lock():
    # A named Windows mutex also coordinates disposable snapshots sharing a GPU.
    handle = None
    if os.name == 'nt':
        import ctypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        api.CreateMutexW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = api.ReleaseMutex.argtypes = [ctypes.c_void_p]
        handle = api.CreateMutexW(None, True, 'Local\\Backtrackify-GPU-' + os.environ.get('CUDA_VISIBLE_DEVICES', 'default'))
        if not handle:
            raise Refused('Cannot acquire Windows GPU ownership mutex')
        if ctypes.get_last_error() == 183:
            api.CloseHandle(handle)
            raise Refused('Another Backtrackify session/snapshot owns this GPU; wait for it to finish')
    try:
        with lock(ROOT / 'runtime' / 'backend.lock'):
            yield
    finally:
        if handle:
            api.ReleaseMutex(handle)
            api.CloseHandle(handle)


@dataclass
class Task:
    id: int
    parent: int
    role: str
    path: str
    summary: str
    status: str = 'PENDING'
    attempts: int = 0
    result: str = ''
    reason: str = ''
    related: list[int] = field(default_factory=list)
    kind: str = ''
    needs: list[int] = field(default_factory=list)


@dataclass
class State:
    tasks: dict[int, Task] = field(default_factory=dict)
    next_id: int = 1
    closed: bool = False
    replans: int = 0
    phase: str = 'new'
    events: list[str] = field(default_factory=list)
    actions: int = 0
    started: float = field(default_factory=time.time)
    max_depth: int = LIMIT_DEPTH
    reason: str = ''

    def event(self, detail):
        self.events.append(time.strftime('%Y-%m-%d %H:%M:%S') + ' | ' + detail.replace('\n', ' ')[:900])
        while len(self.events)>40:
            # An interrupted round's budget must outlive ordinary diagnostic rollover.
            drop=next((i for i,e in enumerate(self.events) if self.phase!='planning' or
                       not e.partition(' | ')[2].startswith(f'Planning attempt {self.replans}.')),0)
            self.events.pop(drop)

    def validate(self):
        if type(self.max_depth) is not int or self.max_depth not in {p[3] for p in PROFILES.values()}:
            raise Refused('Invalid static TODO depth limit')
        if len(self.tasks) > LIMIT_TASKS or self.next_id != len(self.tasks) + 1 or set(self.tasks) != set(range(1,self.next_id)):
            raise Refused('Too many tasks or reused TODO ID')
        if self.phase not in {'new', 'planning', 'ready', 'blocked', 'complete'}:
            raise Refused('Invalid phase')
        if not math.isfinite(self.started) or self.started <= 0:
            raise Refused('Invalid start time')
        if any(t.status == 'RUNNING' for t in self.tasks.values()) and self.phase != 'ready':
            raise Refused('Running agent requires the execution phase')
        if sum(t.status == 'RUNNING' for t in self.tasks.values()) > 1:
            raise Refused('Agent tasks must run sequentially')
        for key, t in self.tasks.items():
            if key != t.id or type(t.id) is not int or t.id < 1 or t.role not in ROLES[1:] or t.status not in STATUSES:
                raise Refused('Invalid task identity, role or status')
            if type(t.attempts) is not int or not 0 <= t.attempts <= LIMIT_ATTEMPTS:
                raise Refused('Invalid attempt counter')
            if t.status == 'RUNNING' and t.attempts == 0:
                raise Refused('Running task has no persisted attempt')
            if not isinstance(t.path,str): raise Refused('Invalid optional artifact path')
            if t.path: safe_path(t.path)
            cell(t.summary, field='summary')
            for name, value, limit in [('result',t.result,SUMMARY_LIMIT),('reason',t.reason,560),('kind',t.kind,40)]:
                if value: cell(value,limit,name)
                elif not isinstance(value,str): raise Refused('Invalid task '+name)
            if t.status == 'DONE' and (not t.result or t.attempts == 0):
                raise Refused("DONE requires the task's own accepted worker result")
            if t.status in {'FAILED','BLOCKED','DROPPED'} and (not t.reason or not t.kind):
                raise Refused('Failed/blocked/dropped task requires its concrete cause')
            if (not isinstance(t.related,list) or any(type(i) is not int or i not in self.tasks or i == t.id for i in t.related)
                    or len(t.related) != len(set(t.related))):
                raise Refused('Invalid related TODO IDs')
            if not isinstance(t.needs,list): raise Refused(f'Task {t.id}: needs must be an ID list')
            seen_needs = set()
            for id in t.needs:
                if type(id) is not int or id not in self.tasks: raise Refused(f'Task {t.id}: unknown needs ID {id!r}')
                if id == t.id: raise Refused(f'Task {t.id} needs itself')
                if id in seen_needs: raise Refused(f'Task {t.id}: duplicate needs ID {id}')
                if t.status != 'DROPPED' and self.tasks[id].status == 'DROPPED':
                    raise Refused(f'Task {t.id}: dropped needs ID {id}')
                seen_needs.add(id)
            if t.status in {'DONE','RUNNING'} and any(self.tasks[i].status != 'DONE' for i in t.needs):
                raise Refused('Accepted/running task has unfinished needs')
            if type(t.parent) is not int or t.parent < 0: raise Refused('Invalid parent ID')
            seen = {t.id}
            parent = t.parent
            while parent:
                if type(parent) is not int or parent not in self.tasks or parent in seen:
                    raise Refused('Missing parent or circular TODO tree')
                seen.add(parent)
                if len(seen) > self.max_depth: raise Refused('TODO depth limit exceeded')
                parent = self.tasks[parent].parent
            children = [c for c in self.tasks.values() if c.parent == t.id and c.status != 'DROPPED']
            if t.status == 'WAITING' and not any(c.status != 'DONE' for c in children):
                raise Refused('Waiting task requires unfinished children')
            if t.status == 'DROPPED' and children: raise Refused('Dropped parent has live children')
            if t.status != 'WAITING' and any(c.status != 'DONE' for c in children):
                raise Refused('Parent status conflicts with unfinished children')
        visiting,visited = [],set()
        def visit(id):
            if id in visiting:
                cycle = visiting[visiting.index(id):]+[id]
                raise Refused('Circular children/needs prerequisites: '+' -> '.join(map(str,cycle)))
            if id in visited: return
            visiting.append(id)
            for prerequisite in self.prerequisites(id): visit(prerequisite)
            visiting.pop();visited.add(id)
        for t in self.tasks.values():
            if t.status != 'DROPPED': visit(t.id)
        if not 0 <= self.replans <= LIMIT_REPLANS or not 0 <= self.actions <= 64:
            raise Refused('Run limits exceeded')
        if type(self.closed) is not bool or type(self.actions) is not int or type(self.replans) is not int:
            raise Refused('Invalid state counters')
        if self.phase == 'blocked': cell(self.reason,560,'stop reason')
        elif self.reason: raise Refused('Unexpected stop reason outside blocked state')
        if self.phase == 'complete' and not self.complete(): raise Refused('Completion with unfinished TODOs')

    def render(self):
        rows = ['Backtrackify state 4',
                f'META | {self.next_id} | {int(self.closed)} | {self.replans} | {self.phase} | {self.actions} | {self.started:.3f} | {self.max_depth}',
                '# id | parent | role | status | attempts | needs IDs | path | summary',
                '# RESULT | id | kind | related IDs | result | reason']
        if self.reason: rows.append('STOP | '+self.reason)
        for t in sorted(self.tasks.values(),key=lambda t:t.id):
            rows.append(f'{t.id} | {t.parent} | {t.role} | {t.status} | {t.attempts} | {",".join(map(str,sorted(t.needs)))} | {t.path} | {t.summary}')
            if t.result or t.reason or t.related or t.kind:
                rows.append(f'RESULT | {t.id} | {t.kind} | {",".join(map(str,t.related))} | {t.result} | {t.reason}')
        return '\n'.join(rows + ['EVENT | '+e for e in self.events]) + '\n'

    def save(self, path):
        self.validate()
        atomic(path,self.render())
        if path.name == 'state.log' and (path.parent/'history').is_dir():
            from visualize import record
            try: record(path)
            except (OSError,ValueError) as e:
                with contextlib.suppress(OSError):status('VIEW', 'WARN', f'History capture failed; state is saved: {e}')

    @classmethod
    def load(cls, path):
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) < 2 or lines[0] != 'Backtrackify state 4':
            raise Refused('Unknown state format; start a fresh orchestration session')
        m = [x.strip() for x in lines[1].split('|')]
        if len(m) != 8 or m[0] != 'META' or m[2] not in {'0','1'}: raise Refused('Corrupt state metadata')
        s = cls(next_id=int(m[1]),closed=m[2]=='1',replans=int(m[3]),phase=m[4],
                actions=int(m[5]),started=float(m[6]),max_depth=int(m[7]))
        results = {}
        for line in lines[2:]:
            if line.startswith('#') or not line.strip(): continue
            if line.startswith('EVENT | '):
                s.events.append(line[8:]); continue
            v = [x.strip() for x in line.split('|')]
            if v[0] == 'STOP':
                if len(v) != 2 or s.reason: raise Refused('Corrupt or duplicate stop reason')
                s.reason = v[1]
            elif v[0] == 'RESULT':
                if len(v) != 6 or int(v[1]) in results: raise Refused('Corrupt or duplicate result row')
                results[int(v[1])] = v
            else:
                if len(v) != 8 or int(v[0]) in s.tasks: raise Refused('Corrupt or duplicate TODO row')
                t = Task(int(v[0]),int(v[1]),v[2],v[6],v[7],v[3],int(v[4]),needs=[int(i) for i in v[5].split(',')] if v[5] else [])
                s.tasks[t.id] = t
        for id, v in results.items():
            if id not in s.tasks: raise Refused('Result references an unknown task')
            t = s.tasks[id]
            t.kind,t.related,t.result,t.reason = v[2],[int(i) for i in v[3].split(',')] if v[3] else [],v[4],v[5]
        s.validate()
        return s

    def block(self, reason):
        self.phase,self.reason = 'blocked',cell(reason,560,'stop reason')
        self.event('Stopped: '+self.reason)

    def settle(self):
        """Children unblock their parent; only its own report can complete it."""
        for t in sorted(self.tasks.values(),key=lambda t:t.id):
            children = [c for c in self.tasks.values() if c.parent == t.id and c.status != 'DROPPED']
            if any(c.status != 'DONE' for c in children):
                if t.status != 'DROPPED': t.status = 'WAITING'
                if t.attempts >= LIMIT_ATTEMPTS and self.phase != 'blocked':
                    self.block(f'Task {t.id}: no attempts remain for its own final report after children; cannot execute this split')
            elif t.status == 'WAITING' or (t.status == 'PENDING' and t.attempts >= LIMIT_ATTEMPTS):
                if t.attempts < LIMIT_ATTEMPTS:
                    t.status = 'PENDING'
                else:
                    t.status,t.kind = 'BLOCKED','limit'
                    t.reason = ("No attempts remain for this task's own final report; previous cause: "+t.reason)[:560]
                    if self.phase != 'blocked': self.block(f'Task {t.id}: '+t.reason[:530])

    def recover(self):
        for t in self.tasks.values():
            if t.status == 'RUNNING':
                t.status,t.kind = 'FAILED','interrupted'
                t.reason = ('Interrupted before report acceptance; inspect retained review files' +
                            ('; prior unresolved cause: '+t.reason if t.reason else ''))[:560]
                self.event(f'Task {t.id} interrupted; attempt consumed')
        self.settle()
        if self.phase == 'planning':
            self.event('Interrupted planning; resume the persisted attempt budget, only committed operations remain')

    def allowed_add_parents(self):
        parents = [0]
        for t in sorted(self.tasks.values(),key=lambda t:t.id):
            if t.status not in {'PENDING','FAILED','WAITING','BLOCKED'} or t.attempts >= LIMIT_ATTEMPTS: continue
            depth,parent = 1,t.parent
            while parent and depth < self.max_depth:
                depth,parent = depth+1,self.tasks[parent].parent
            if depth < self.max_depth: parents.append(t.id)
        return parents

    def failure(self, task):
        return f'[{task.kind}] {task.reason}' if task.kind else task.reason

    def needs_split(self, task):
        return task.status in {'FAILED','BLOCKED'} and (task.kind in {'context','limit','timeout'} or task.attempts >= LIMIT_ATTEMPTS)

    def can_retry_parent(self, task):
        children=[t for t in self.tasks.values() if t.parent==task.id and t.status!='DROPPED']
        return (task.status in {'FAILED','BLOCKED'} and task.attempts<LIMIT_ATTEMPTS and
                bool(children) and all(t.status=='DONE' for t in children))

    def prerequisites(self, id):
        task = self.tasks[id]
        return sorted(task.needs) + sorted(t.id for t in self.tasks.values()
            if t.parent == id and t.status != 'DROPPED' and t.id not in task.needs)

    def invalidate(self, changed):
        """Propagate through dependents and ancestors; keep old reports and attempt counts."""
        affected = set(changed)
        while True:
            added = {t.id for t in self.tasks.values() if t.status != 'DROPPED' and t.id not in affected and
                     affected.intersection(self.prerequisites(t.id))}
            if not added: break
            affected.update(added)
        for id in sorted(affected):
            task = self.tasks[id]
            if task.status == 'DONE':
                cause = next(i for i in self.prerequisites(id) if i in affected)
                task.status,task.kind = 'PENDING','invalidated'
                task.reason = f'Prerequisite {cause} changed; repeat this task\'s own accepted result'
                self.event(f'Task {id} invalidated by prerequisite {cause}')

    def frontier(self):
        """Needs first, then children, then own work; never skip an unresolved result."""
        self.settle()
        if self.phase == 'blocked': return None,''
        def visit(task,route):
            if task.status in {'DONE','DROPPED'}: return None
            for id in self.prerequisites(task.id):
                result = visit(self.tasks[id],route+('>needs:' if id in task.needs else '>child:')+str(id))
                if result: return result
            return task,route
        for task in sorted(self.tasks.values(),key=lambda t:t.id):
            if task.parent == 0:
                result = visit(task,str(task.id))
                if result: return result
        return None,''

    def eligible(self):
        task,_ = self.frontier()
        return task if task and task.status == 'PENDING' and task.attempts < LIMIT_ATTEMPTS else None

    def complete(self):
        active = [t for t in self.tasks.values() if t.status != 'DROPPED']
        return (self.closed and self.phase != 'blocked' and bool(active) and
                all(t.status == 'DONE' and t.result and t.attempts > 0 and
                    all(i in self.tasks and self.tasks[i].status == 'DONE' for i in t.needs) for t in active))


def parse_plan(text):
    ops = []
    for line in text.strip().splitlines():
        v = [x.strip() for x in line.split('|')]
        if v[0] == 'ADD' and len(v) in {5,6}:
            ops.append(dict(op='ADD', parent_id=int(v[1]), role=v[2], summary=v[4], **({'path':v[3]} if v[3] else {})))
            if len(v) == 6: ops[-1]['needs'] = [int(i.strip()) for i in v[5].split(',')] if v[5] else []
        elif v[0] == 'NEEDS' and len(v) == 3:
            ops.append(dict(op='NEEDS',id=int(v[1]),needs=[int(i.strip()) for i in v[2].split(',')] if v[2] else []))
        elif v[0] == 'REOPEN' and len(v) == 3:
            ops.append(dict(op='REOPEN',id=int(v[1]),reason=v[2]))
        elif v[0] == 'EDIT' and len(v) == 3:
            ops.append(dict(op='EDIT', id=int(v[1]), summary=v[2]))
        elif v[0] == 'DROP' and len(v) == 3:
            ops.append(dict(op='DROP', id=int(v[1]), reason=v[2]))
        elif v[0] == 'FINISH' and len(v) == 1:
            ops.append(dict(op='FINISH'))
        elif v[0] == 'BLOCKED' and len(v) == 2:
            ops.append(dict(op='BLOCKED',reason=v[1]))
        else:
            raise Refused(f'Invalid plan line: {line[:120]}')
    return ops


def apply_plan(state, operations):
    """Validate the entire batch on a copy; caller checkpoints only the returned state."""
    if not isinstance(operations, list) or not 1 <= len(operations) <= 65:
        raise Refused('Plan batch must contain 1..65 operations')
    state.validate()
    if any(t.status == 'RUNNING' for t in state.tasks.values()): raise Refused('Cannot plan while a worker is running')
    s = copy.deepcopy(state)
    continuations=set()
    s.reason = ''
    s.closed = False
    s.phase = 'ready'
    terminal = False
    fields = {'ADD': {'op', 'parent_id', 'role', 'summary'},
              'EDIT': {'op', 'id', 'summary'}, 'DROP': {'op', 'id', 'reason'},
              'NEEDS': {'op','id','needs'}, 'REOPEN': {'op','id','reason'}, 'FINISH': {'op'}, 'BLOCKED': {'op','reason'}}
    for op in operations:
        if (terminal or not isinstance(op, dict) or not isinstance(op.get('op'), str) or
                op['op'] not in fields or not fields[op['op']] <= op.keys() or
                op.keys() - (fields[op['op']] | ({'path','needs'} if op['op'] == 'ADD' else set()))):
            raise Refused('Invalid operation fields, or operations after terminal marker')
        kind = op['op']
        for k in ('id', 'parent_id'):
            if k in op and (type(op[k]) is not int or op[k] < (0 if k == 'parent_id' else 1)):
                raise Refused('IDs must be nonnegative integers; task IDs start at 1')
        if 'needs' in op and (not isinstance(op['needs'],list) or
                any(type(i) is not int or i < 1 for i in op['needs']) or len(op['needs']) != len(set(op['needs']))):
            raise Refused('needs must be a list of distinct positive TODO IDs')
        if kind == 'ADD':
            if op['role'] not in ('ALGORITHM', 'TEST'):
                raise Refused('ADD role must be ALGORITHM or TEST')
            parent = op['parent_id']
            allowed = s.allowed_add_parents()
            if parent not in allowed:
                current = s.tasks[parent].status if parent in s.tasks else 'unknown'
                reason = ('out of worker attempts; its own final report is still required' if parent in s.tasks and s.tasks[parent].attempts >= LIMIT_ATTEMPTS
                          else 'at the depth limit' if current in {'PENDING', 'FAILED', 'WAITING'} else current)
                raise Refused(f'ADD parent {parent} is {reason}; allowed existing parents: ' + ', '.join(map(str, allowed)))
            if parent:
                s.tasks[parent].status = 'WAITING'
            t = Task(s.next_id, parent, op['role'], safe_path(op['path']) if 'path' in op else '', cell(op['summary'], field='summary'))
            t.needs = sorted(op.get('needs',[]))
            s.tasks[t.id] = t
            s.next_id += 1
            if parent: s.invalidate({parent})
        elif kind in {'NEEDS','REOPEN'}:
            t = s.tasks.get(op['id'])
            if not t or t.status in {'RUNNING','DROPPED'}: raise Refused('Unknown or unmodifiable task')
            if kind == 'REOPEN':
                if t.status != 'DONE': raise Refused('REOPEN requires a DONE task')
                t.status,t.kind,t.reason = 'PENDING','reopened',cell(op['reason'],560,'reopen reason')
                s.event(f'Task {t.id} reopened: {t.reason}')
                s.invalidate({t.id})
            else:
                if t.status == 'DONE': raise Refused('REOPEN a DONE task before changing its needs')
                needs = sorted(op['needs'])
                if t.needs != needs:
                    t.needs = needs
                    s.invalidate({t.id})
        elif kind in {'EDIT', 'DROP'}:
            t = s.tasks.get(op['id'])
            continuing=bool(t and kind=='EDIT' and s.can_retry_parent(t))
            children=[c.id for c in s.tasks.values() if t and c.parent==t.id and c.status!='DROPPED']
            if (not t or t.status not in {'PENDING', 'FAILED', 'BLOCKED'} or
                    children and not continuing):
                detail=('EDIT must precede ADD; split parents retain their original scope. '
                        'Only FAILED/BLOCKED parents with DONE children and attempts remaining allow unchanged-scope continuation.' if children else
                        'Only unfinished leaf tasks may be edited; DONE requires REOPEN with a reason.')
                raise Refused(f'{kind} task {op["id"]}: status {t.status if t else "unknown"}; live children {children}. '+detail)
            if kind == 'EDIT':
                if t.attempts >= LIMIT_ATTEMPTS:
                    raise Refused('Retry limit exhausted; split the failed task or BLOCKED')
                if continuing:
                    if op['summary']!=t.summary:raise Refused('Parent continuation must preserve its exact original scope')
                    continuations.add(t.id)
                t.summary, t.status = cell(op['summary'], field='summary'), 'PENDING'
            else:
                if t.attempts or t.result or t.reason or any(c.parent == t.id and c.status != 'DROPPED' for c in s.tasks.values()):
                    raise Refused('DROP requires an unstarted task without unresolved history or active children')
                t.status,t.kind,t.reason = 'DROPPED','drop',cell(op['reason'],560,'drop reason')
        else:
            terminal = True
            s.closed = kind == 'FINISH'
            if kind == 'BLOCKED':
                if len(operations) != 1:
                    raise Refused('BLOCKED requires empty operations: use FINISH to apply plan changes, or BLOCKED alone for a concrete unresolved obstacle')
                s.block(op['reason'])
    if not terminal:
        raise Refused('Batch must end with FINISH or BLOCKED')
    s.settle()
    if s.closed and s.phase=='blocked' and state.phase!='blocked':raise Refused(s.reason)
    if s.closed and not any(t.status != 'DROPPED' for t in s.tasks.values()):
        raise Refused('A finished plan requires at least one task')
    if s.closed and any(t.status in {'FAILED','BLOCKED'} for t in s.tasks.values()):
        ids=', '.join(str(t.id) for t in s.tasks.values() if t.status in {'FAILED','BLOCKED'})
        raise Refused(f'Unresolved FAILED/BLOCKED tasks: {ids}. FINISH never retries them; use a permitted EDIT or repair children')
    if s.phase != 'blocked':
        for old in state.tasks.values():
            if not state.needs_split(old):
                continue
            children = [t for t in s.tasks.values() if t.parent == old.id and t.status != 'DROPPED']
            if s.tasks[old.id].status == 'PENDING' and old.id not in continuations:
                raise Refused(f'Task {old.id} needs smaller children after its execution limit, or BLOCKED')
            if any(t.role == old.role and t.summary.casefold() == old.summary.casefold() for t in children):
                raise Refused(f'Task {old.id}: a child repeats the entire failed task; narrow its scope')
    s.validate()
    s.event(f'Atomic plan batch applied: {len(operations)} operations')
    return s


def apply_report(state, task_id, report):
    """Accept one worker result atomically; its contents are not a correctness proof."""
    state.validate()
    fields = {'task_id','status','summary','reason','related_tasks'}
    if (type(task_id) is not int or not isinstance(report,dict) or not {'task_id','status','summary'} <= report.keys()
            or report.keys() - fields or type(report['task_id']) is not int or report['task_id'] != task_id):
        raise Refused('Invalid report fields or delegated task ID')
    task = state.tasks.get(task_id)
    if not task or task.status != 'RUNNING': raise Refused('Report requires the currently running task')
    status = report['status']
    if not isinstance(status,str) or status not in {'DONE','FAILED','BLOCKED'}: raise Refused('Invalid report status')
    summary = cell(report['summary'],SUMMARY_LIMIT,'summary')
    reason = cell(report['reason'],560,'reason') if 'reason' in report else ''
    if status != 'DONE' and not reason: raise Refused('FAILED/BLOCKED requires a concrete reason')
    related = report.get('related_tasks',[])
    if (not isinstance(related,list) or len(related) != len(set(i for i in related if type(i) is int))
            or any(type(i) is not int or i == task_id or i not in state.tasks or
                   state.tasks[i].status in {'DROPPED','RUNNING'} for i in related)):
        raise Refused('Invalid related TODO IDs')
    s = copy.deepcopy(state)
    t = s.tasks[task_id]
    t.status,t.result,t.reason,t.related,t.kind = status,summary,reason,list(related),'agent' if status != 'DONE' else ''
    if status != 'DONE':
        s.closed = False
    s.settle()
    if s.phase != 'blocked': s.phase = 'ready'
    s.event(f'Task {task_id} report {status}: {summary}' + (f'; {reason}' if reason else ''))
    s.validate()
    return s


class WindowsJob:
    """A non-inherited kernel handle; closing its last handle kills only its members."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [('times', ctypes.c_int64 * 2), ('flags', w.DWORD),
                        ('working_set', ctypes.c_size_t * 2), ('processes', w.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', Limits), ('io', ctypes.c_uint64 * 6), ('memory', ctypes.c_size_t * 4)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, handle):
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


_owner_job = None


def start_process(command, **kwargs):
    """Contain descendants across normal cleanup and abrupt Python/console termination."""
    global _owner_job
    job = None
    if os.name == 'nt':
        if _owner_job is None:
            owner = WindowsJob()
            try:
                owner.add(-1)  # GetCurrentProcess pseudo-handle, before spawning any children.
            except BaseException:
                owner.close()
                raise
            # Intentionally held until OS process teardown, including TerminateProcess.
            _owner_job = owner
        job = WindowsJob()
    p = None
    try:
        p = subprocess.Popen(command, **kwargs)
        if job:
            job.add(int(p._handle))
        p._backtrackify_job = job
        return p
    except BaseException:
        if job:
            job.close()
        if p is not None:
            p.kill()  # Retained handle, never a rediscovered PID.
            p.wait()
        raise


def windows_servers():
    """Read-only check across ports/assets; names are conflict warnings, never kill targets."""
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD), ('name', w.WCHAR * 260)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    api.CreateToolhelp32Snapshot.restype = w.HANDLE
    api.Process32FirstW.argtypes = api.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry(size=ctypes.sizeof(Entry))
        result = api.Process32FirstW(handle, ctypes.byref(entry))
        found = []
        while result:
            if entry.name.casefold() == 'llama-server.exe':
                found.append(entry.pid)
            result = api.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES; fail closed on enumeration errors.
            raise ctypes.WinError(ctypes.get_last_error())
        return found
    finally:
        api.CloseHandle(handle)


def execute(command, cwd, timeout, output, env=None):
    """Kill only the process tree we created, even if its root exits before its children."""
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with output.open('w', encoding='utf-8') as log:
        p = start_process(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                           creationflags=flags, start_new_session=os.name != 'nt')
        try:
            return p.wait(timeout=timeout)
        finally:
            stop_process(p)


def stop_process(p):
    job = getattr(p, '_backtrackify_job', None)
    try:
        if p.poll() is None:
            if os.name == 'nt':
                p.kill()  # Retained handle; no PID lookup or global process-name termination.
            else:
                import signal
                os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    finally:
        if job:
            job.close()  # Includes descendants of an already exited root; unrelated jobs are untouched.


def merge(workspace, review, original, scope=None, commit=None, snapshot=False):
    """Validate the whole candidate before promotion; hashes guard concurrent edits, not quality."""
    if files(workspace,snapshot) != original: raise AgentFailure('workspace','Workspace changed during delegated work')
    if scope == '':  # No artifact path: retain the review without promoting edits.
        if commit:commit()
        return
    after = files(review)
    delta = sorted(p for p in original.keys() | after.keys() if original.get(p) != after.get(p))
    for rel in delta:
        safe_path(rel)
        if scope is not None and rel != scope and not rel.startswith(scope + '/'):
            raise AgentFailure('workspace',f'TEST may promote only its assigned artifact path: {scope}; unexpected file: {rel}')
    backup = {rel:(workspace/rel).read_bytes() if rel in original else None for rel in delta}
    applied = []
    try:
        for rel in delta:
            applied.append(rel)
            if rel in after: atomic(workspace/rel,(review/rel).read_bytes())
            else: (workspace/rel).unlink()
        if commit:commit()
    except BaseException:
        for rel in reversed(applied):
            if backup[rel] is None: (workspace/rel).unlink(missing_ok=True)
            else: atomic(workspace/rel,backup[rel])
        raise


def request(url, data=None, timeout=60):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def generation(profile, role, context):
    thinking, output = PROFILES[profile][:2]
    if output + 2048 > context:
        raise Refused(f'{role}: context {context} leaves insufficient prompt/tool headroom; split work or restore context')
    return thinking, output


class ReasoningBuffer:
    """Group reasoning deltas; preserve text and flush before every other event."""
    def __init__(self, minimum):
        self.minimum, self.pending, self.saved, self.text = minimum, b'', None, ''
        self.identity = None

    def flush(self):
        if self.saved is None:
            return b''
        self.saved['choices'][0]['delta']['reasoning_content'] = self.text
        data = ('data: ' + json.dumps(self.saved) + '\n\n').encode()
        self.saved, self.text, self.identity = None, '', None
        return data

    def feed(self, data, final=False):
        self.pending += data
        output = b''
        while match := re.search(br'\r?\n\r?\n', self.pending):
            frame, self.pending = self.pending[:match.end()], self.pending[match.end():]
            try:
                lines = [line for line in frame.splitlines() if line]
                chunk = json.loads(b'\n'.join(line[5:].strip() for line in lines if line.startswith(b'data:')))
                choices = chunk['choices']
                delta = choices[0]['delta']
                thought = delta.get('reasoning_content')
                pure = (len(lines) == 1 and lines[0].startswith(b'data:') and
                        set(chunk) <= {'id','object','created','model','system_fingerprint','choices','usage'} and
                        len(choices) == 1 and set(choices[0]) <= {'index','delta','finish_reason'} and
                        isinstance(thought, str) and thought and choices[0].get('finish_reason') is None and not chunk.get('usage') and
                        set(delta) <= {'role', 'content', 'reasoning_content'} and not delta.get('content'))
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                pure = False
            if pure:
                identity = ({k:v for k,v in chunk.items() if k!='choices'},
                            {k:v for k,v in choices[0].items() if k!='delta'},
                            {k:v for k,v in delta.items() if k!='reasoning_content'})
                if self.saved and self.identity != identity:
                    output += self.flush()
                self.saved = self.saved or chunk
                self.identity = identity
                self.text += thought
                if len(self.text) >= self.minimum:
                    output += self.flush()
            else:
                output += self.flush() + frame
        if final:
            output += self.flush() + self.pending
            self.pending = b''
        return output


def compression_request(body, output):
    """Adapt only the pinned CLI's native cold compression request; keep its history intact."""
    messages=body.get('messages',[])
    directive=messages[-1].get('content') if messages else None
    if isinstance(directive,list) and len(directive)==1 and directive[0].get('type')=='text':directive=directive[0].get('text')
    if (body.get('tools') or len(messages)<2 or messages[0].get('role')!='system' or
            not isinstance(messages[0].get('content'),str) or not messages[0]['content'].startswith(COMPRESSION_PREFIX) or
            messages[-1].get('role')!='user' or directive!=COMPRESSION_DIRECTIVE):return False
    if type(body.get('max_tokens')) is not int or body['max_tokens']<=0:raise Refused('Invalid native compaction output allowance')
    messages[0]['content']=COMPRESSION_PROMPT
    messages[-1]['content']='Return the complete continuation snapshot only.'
    body['max_tokens']=min(body['max_tokens'],1024 if output<=4608 else 3072)
    body['thinking_budget_tokens']=0
    body.setdefault('chat_template_kwargs',{})['enable_thinking']=False
    body['grammar']=COMPRESSION_GRAMMAR
    return True


def compression_summary(data, stream):
    if stream:
        rows=[json.loads(line[5:]) for line in data.splitlines() if line.startswith(b'data:') and line[5:].strip()!=b'[DONE]']
        text=''.join(c.get('delta',{}).get('content') or '' for row in rows for c in row.get('choices',[]))
    else:
        rows=[json.loads(data)]
        text=''.join(c.get('message',{}).get('content') or '' for c in rows[0].get('choices',[]))
    reasons=[c.get('finish_reason') for row in rows for c in row.get('choices',[]) if c.get('finish_reason')]
    if not reasons or reasons[-1]!='stop' or '<analysis>' in text:
        raise Refused('Native compaction did not produce a complete final snapshot')
    try:root=ET.fromstring(text.strip())
    except ET.ParseError as e:raise Refused('Native compaction returned malformed/incomplete snapshot XML') from e
    if root.tag!='state_snapshot' or not ''.join(root.itertext()).strip():
        raise Refused('Native compaction returned an empty or invalid snapshot')
    return text


class InferenceGate:
    """Check each *new* native request; never shrink an active generation budget."""
    def __init__(self, backend, context, thinking, output, log, trace=False, reasoning_chunk_chars=REASONING_CHUNK_CHARS, wall_time=PROFILES['MID'][2], vision=False):
        self.backend, self.context, self.thinking, self.output = backend, context, thinking, output
        self.log, self.calls = log, []
        self.trace, self.reasoning_chunk_chars = trace, reasoning_chunk_chars
        self.failure = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def note(self, kind, detail):
                with owner.log.open('a', encoding='utf-8') as log:
                    log.write(kind + ' | ' + str(detail) + '\n')

            def do_POST(self):
                started = False
                buffer = None
                try:
                    if self.path != '/v1/chat/completions':
                        raise Refused('Only native chat completions are permitted')
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size <= 8_000_000:
                        raise Refused('Oversized inference request')
                    body = json.loads(self.rfile.read(size))
                    compact=compression_request(body,owner.output)
                    if not compact and (body.get('thinking_budget_tokens') != owner.thinking or body.get('max_tokens') != owner.output):
                        raise Refused('Native thinking/output budget differs from the configured finite allowance')
                    from vision import has_images,checked_body
                    if has_images(body.get('messages',[])):
                        if not vision:raise Refused('This worker is text-only; image input is reserved for interactive Qwen and PROMPT')
                        body=checked_body(body)
                        tokens=request(owner.backend+'/v1/chat/completions/input_tokens',body,timeout=30)['input_tokens']
                        if type(tokens) is not int or tokens<0:raise Refused('Invalid multimodal token count')
                        template='[Native multimodal input; image bytes omitted from template log]'
                    else:
                        template = request(owner.backend + '/apply-template', body, timeout=30)['prompt']
                        tokens = len(request(owner.backend + '/tokenize', {'content': template, 'add_special': True}, timeout=30)['tokens'])
                    output,thinking=body['max_tokens'],body['thinking_budget_tokens']
                    if tokens + output + 256 > owner.context:
                        raise Refused(f'Context headroom exhausted: prompt {tokens} + generation {output} + safety 256 > {owner.context}. Split work at a task boundary.')
                    record = dict(thinking=thinking, output=output, prompt=tokens, complete=False, compression=compact)
                    owner.failure = None
                    owner.calls.append(record)
                    wire = owner.log.parent / f'wire-{len(owner.calls)}'
                    if owner.trace:
                        atomic(wire.with_suffix('.request.json'), json.dumps(body, indent=2))
                        atomic(wire.with_suffix('.template.txt'), template)
                    with owner.log.open('a', encoding='utf-8') as log:
                        log.write(f'PROMPT {tokens} | THINKING {thinking} | OUTPUT {output} | CONTEXT {owner.context}'+(' | COMPRESSION' if compact else '')+'\n')
                    req = urllib.request.Request(owner.backend + self.path, json.dumps(body).encode(), {'Content-Type':'application/json'})
                    with urllib.request.urlopen(req, timeout=wall_time + 30) as response, contextlib.ExitStack() as traces:
                        raw = traces.enter_context(wire.with_suffix('.backend.sse').open('wb')) if owner.trace else None
                        sent = traces.enter_context(wire.with_suffix('.client.sse').open('wb')) if owner.trace else None
                        if compact:
                            data=response.read()
                            if raw:raw.write(data)
                            record['complete']=True
                            try:summary=compression_summary(data,body.get('stream',False))
                            except Refused as e:
                                record['summary_error']=str(e)
                                wire.with_suffix('.compression-error.sse').write_bytes(data)
                                self.note('COMPRESSION_REJECTED',e);raise
                            self.note('COMPRESSION_ACCEPTED',f'{len(summary)} characters; complete snapshot')
                            self.send_response(response.status)
                            self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                            self.end_headers();started=True
                            if sent:sent.write(data)
                            self.wfile.write(data);self.wfile.flush()
                            return
                        buffer = ReasoningBuffer(owner.reasoning_chunk_chars) if owner.reasoning_chunk_chars and body.get('stream') else None
                        try:
                            self.send_response(response.status)
                            self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                            started = True
                            self.end_headers()
                        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as e:
                            self.note('CLIENT_DISCONNECTED', e)
                            return
                        pending = b''
                        record['complete'] = not body.get('stream', False)
                        while data := response.read1(65536):
                            if raw:
                                raw.write(data); raw.flush()
                            if body.get('stream'):
                                pending += data
                                while b'\n' in pending:
                                    line, pending = pending.split(b'\n', 1)
                                    if not line.startswith(b'data:'):
                                        continue
                                    payload = line[5:].strip()
                                    if payload == b'[DONE]':
                                        record['complete'] = True
                                    else:
                                        try:
                                            chunk = json.loads(payload)
                                            reasons = [c.get('finish_reason') for c in chunk.get('choices', [])]
                                            record['complete'] |= any(reason in ('stop', 'tool_calls', 'function_call', 'length', 'content_filter') for reason in reasons)
                                            if 'length' in reasons and not record.get('limited'):
                                                self.note('LENGTH', f'request {len(owner.calls)}')
                                            record['limited'] = record.get('limited', False) or 'length' in reasons
                                        except (ValueError, AttributeError, TypeError):
                                            record['invalid'] = True
                                            self.note('STREAM_INVALID', 'Invalid native SSE event')
                            try:
                                forwarded = buffer.feed(data) if buffer else data
                                if sent:
                                    sent.write(forwarded); sent.flush()
                                self.wfile.write(forwarded)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as e:
                                self.note('CLIENT_DISCONNECTED', e)
                                return
                        if buffer:
                            forwarded = buffer.feed(b'', final=True)
                            if sent:
                                sent.write(forwarded); sent.flush()
                            try:
                                self.wfile.write(forwarded)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as e:
                                self.note('CLIENT_DISCONNECTED', e)
                                return
                        if not record['complete']:
                            self.note('STREAM_INCOMPLETE', 'Backend closed without a terminal completion event')
                except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
                    detail = str(e)
                    if isinstance(e, urllib.error.HTTPError):
                        detail += ' | ' + e.read(2000).decode('utf-8', errors='replace')
                        e.close()
                    owner.failure = ('context' if detail.startswith(('Context headroom exhausted:','Native compaction')) else 'backend', detail)
                    self.note('UPSTREAM_ERROR' if started else 'REFUSED', detail)
                    if started and buffer:
                        tail = buffer.feed(b'', final=True)
                        if owner.trace:
                            with wire.with_suffix('.client.sse').open('ab') as trace:
                                trace.write(tail)
                        try:
                            self.wfile.write(tail)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as disconnected:
                            self.note('CLIENT_DISCONNECTED', disconnected)
                    if not started:
                        try:
                            self.send_response(400)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            error={'message':str(e),'type':'context_or_budget'}
                            if detail.startswith('Context headroom exhausted:'):
                                # Pinned Qwen handles this with its bounded native reactive compaction.
                                error.update(message=str(e)+f' (context_length_exceeded: {tokens} tokens > {owner.context-output-256} prompt limit)',
                                             type='invalid_request_error',code='context_length_exceeded')
                            self.wfile.write(json.dumps({'error':error}).encode())
                        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as disconnected:
                            self.note('CLIENT_DISCONNECTED', disconnected)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class Qwen:
    def __init__(self, cli, profile, context, base_url, session, agent_seconds=None, vision=False):
        self.cli, self.profile, self.context = str(cli), profile, context
        self.base_url, self.session = base_url, Path(session)
        self.vision=vision
        self.agent_seconds=PROFILES[profile][2] if agent_seconds is None else agent_seconds
        if type(self.agent_seconds) is not int or self.agent_seconds<=0:raise Refused('Agent timeout must be positive seconds')
        self.counter = 0
        self.calls = []
        self.session_ids = []

    def call(self, role, prompt, workspace, structured=False, thinking_override=None,
             system_prompt=None, seed=None, trace=False, reasoning_chunk_chars=REASONING_CHUNK_CHARS,
             interactive=False, cli_args=(), task_id=None):
        if role not in ROLES: raise Refused('Unknown agent role')
        if role != 'PROMPT' and not interactive and (type(task_id) is not int or task_id < 1):
            raise Refused('Worker calls require the delegated task ID')
        self.counter += 1
        invocation = self.session / 'turns' / f'{int(time.time())}-{self.counter}-{uuid.uuid4().hex[:6]}'
        invocation.mkdir(parents=True)
        scratch = invocation / 'tmp'
        scratch.mkdir()
        atomic(invocation / 'system-settings.json', '{}\n')
        thinking, output = generation(self.profile, role, self.context)
        wall_time = self.agent_seconds
        if thinking_override is not None:
            thinking = thinking_override
        if type(thinking) is not int or not 0 <= thinking <= output - 1536:
            raise Refused('Thinking must be finite and leave the final/tool-call reserve intact')
        settings = {'security': {'auth': {'selectedType': 'openai'}},
            'model': {'name': MODEL_ID, 'maxSessionTurns': AGENT_TURNS, 'maxToolCallsPerTurn': 24,
                      'skipLoopDetection': False, 'maxWallTimeSeconds': wall_time,
                      'chatCompression': {'maxRecentFilesToRetain': 0,'maxRecentImagesToRetain':0}},
            # At 32K Qwen derives hard = auto + 3000; align it with the gate's input ceiling.
            'context': {'autoCompactThreshold': max(0.01, min(0.85, (self.context - output - 256 - 3000) / self.context))},
            'modelProviders': {'openai': [{'id': MODEL_ID, 'name': 'Backtrackify local',
                'baseUrl': self.base_url + '/v1', 'envKey': 'BACKTRACKIFY_API_KEY',
                'generationConfig': {'contextWindowSize': self.context, 'samplingParams': {'max_tokens': output},
                    'timeout': (wall_time + 30) * 1000, 'maxRetries': 0, 'extra_body': {'thinking_budget_tokens': thinking,
                    'max_tokens': output, 'cache_prompt': True}}}]},
            'disableAllHooks': True, 'telemetry': {'enabled': False},
            'memory': {'enableManagedAutoMemory': False, 'enableManagedAutoDream': False,
                       'enableAutoSkill': False, 'enableTeamMemory': False},
            'tools': {'shell': {'defaultTimeout': 60000}},
            'general': {'enableAutoUpdate': False}}
        if seed is not None:
            settings['modelProviders']['openai'][0]['generationConfig']['extra_body']['seed'] = seed
        allow_images=self.vision and (interactive or role=='PROMPT')
        settings['modelProviders']['openai'][0]['generationConfig']['modalities']={'image':allow_images}
        image_directory=None
        if role=='PROMPT' and not interactive and allow_images:
            from vision import task_images,image_path,at_reference
            ids=task_images(self.session)
            if ids:
                image_directory=self.session/'images'
                state=self.session/'state.log'
                if not state.exists() or State.load(state).replans<=1:
                    refs=[]
                    for id in ids:refs.append(at_reference(image_path(self.session,id),workspace))
                    prompt=' '.join(refs)+'\n'+prompt
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(('QWEN_', 'OPENAI_', 'GEMINI_', 'ANTHROPIC_')):
                env.pop(key)
        env.update(QWEN_HOME=str(invocation), QWEN_RUNTIME_DIR=str(invocation / 'native'),
                   QWEN_CODE_SYSTEM_SETTINGS_PATH=str(invocation / 'system-settings.json'),
                   QWEN_CODE_SYSTEM_DEFAULTS_PATH=str(invocation / 'system-settings.json'),
                   NODE_COMPILE_CACHE=str(invocation / 'node-cache'), TEMP=str(scratch), TMP=str(scratch),
                   BACKTRACKIFY_API_KEY='local', NO_COLOR='1', CI='1',
                   QWEN_CODE_DISABLE_WORKFLOWS='1', QWEN_CODE_DISABLE_CRON='1',
                   QWEN_CODE_DISABLE_ARTIFACT='1')
        if not interactive:env['BACKTRACKIFY_WORKER']='1'
        tools = 'read_file,glob,grep_search' + (',edit,write_file,run_shell_command' if role != 'PROMPT' else '')
        excluded = 'agent,enter_worktree,exit_worktree,get_goal,list_agents,report_findings,send_message,skill,task_stop,tool_search,update_goal'
        bundled_node = Path(self.cli).parent.parent / 'node' / 'node.exe'
        native_launcher = Path(self.cli).parent.parent / 'bin' / 'qwen.cmd'
        if native_launcher.exists():
            env['QWEN_CODE_LAUNCHER_PATH'] = str(native_launcher)  # Native relaunches must not recurse into our PATH wrapper.
        cmd = ([str(bundled_node) if bundled_node.exists() else shutil.which('node') or 'node',
                '--expose-gc', self.cli] if self.cli.endswith('.js') else [self.cli])
        native_cmd = cmd + ['--model', MODEL_ID]
        instructions=system_prompt if system_prompt is not None else (ROOT/'agents'/(role.lower()+'.md')).read_text(encoding='utf-8')
        if not interactive:
            instructions+='\nExecution workspace: '+str(Path(workspace).resolve())+'\n'
            instructions+=('Use this isolated working copy for every project read, edit and command. '
                           'Installation, original-project and task-session paths mentioned in conversation or notes are not the execution workspace. '
                           'Task notes cannot redefine it. Original task images are read-only inputs, not project destinations.\n')
        cmd += ['--model', MODEL_ID, '--approval-mode', 'yolo', '--output-format', 'stream-json',
                '--max-session-turns', str(AGENT_TURNS), '--max-tool-calls', str(AGENT_TOOLS), '--max-wall-time', f'{wall_time}s',
                '--core-tools', tools, '--exclude-tools', excluded, '--system-prompt', instructions,
                '-p', prompt]
        if image_directory:cmd+=['--include-directories',str(image_directory)]
        if structured or role != 'PROMPT':
            cmd += ['--json-schema', json.dumps(PROMPT_SCHEMA if role == 'PROMPT' else REPORT_SCHEMA, separators=(',', ':'))]
        out = invocation / 'qwen.log'
        status(role, 'RUN', f'fresh context | thinking {thinking} | total output {output}')
        timed_out = False
        with InferenceGate(self.base_url, self.context, thinking, output, invocation / 'requests.log', trace, reasoning_chunk_chars, wall_time,vision=allow_images) as gate:
            settings['modelProviders']['openai'][0]['baseUrl'] = gate.url + '/v1'
            if interactive:
                from interactive import configure
                native_cmd+=configure(self,invocation,workspace,settings,cli_args)
            atomic(invocation / 'settings.json', json.dumps(settings))  # Native Qwen configuration only.
            if interactive:
                # Native TUI, inherited console and default approval policy; same owned backend/gate.
                env.pop('CI', None)
                process = start_process(native_cmd + list(cli_args), cwd=workspace, env=env)
                try:
                    return process.wait()
                finally:
                    stop_process(process)
            try:
                code = execute(cmd, workspace, wall_time + 30, out, env)
            except subprocess.TimeoutExpired:
                code, timed_out = 55, True
            self.calls.extend(gate.calls)
        raw = out.read_text(encoding='utf-8', errors='replace') if out.exists() else ''
        try:
            result = qwen_result(raw, code, timed_out)
            if gate.failure:
                raise AgentFailure(*gate.failure, result.get('result', ''))
            if any(not call['complete'] for call in gate.calls):
                raise AgentFailure('stream', 'Backend stream ended before completion', result.get('result', ''))
            if any(call.get('invalid') for call in gate.calls):
                raise AgentFailure('stream', 'Backend sent invalid SSE data', result.get('result', ''))
            # Qwen can recover an intermediate length event with later tool/final turns.
            if gate.calls and gate.calls[-1].get('limited'):
                raise AgentFailure('limit', 'Final backend generation reached its output limit', result.get('result', ''))
        except AgentFailure as e:
            if e.kind not in {'loop','timeout'}:
                failure = gate.failure
                if not failure and any(not c['complete'] or c.get('invalid') for c in gate.calls):
                    failure = ('stream','Backend stream incomplete or invalid')
                if not failure and gate.calls and gate.calls[-1].get('limited'):
                    failure = ('limit','Final backend generation reached its output limit')
                if failure: raise AgentFailure(failure[0],f'{failure[1]}; inspect {out}',e.report) from e
            raise AgentFailure(e.kind,f'{e}; inspect {out}',e.report) from e
        if result.get('session_id'):
            self.session_ids.append(result['session_id'])
        if role != 'PROMPT':
            value = result.get('structured_result')
            if not isinstance(value,dict) or set(value) != {'report'} or not isinstance(value['report'],dict):
                raise AgentFailure('protocol', 'Missing or invalid native worker report')
            return value['report']
        if structured:
            value = result.get('structured_result')
            if (not isinstance(value, dict) or not {'operations','terminal'}<=set(value) or set(value)-{'operations','terminal','task_notes'} or
                    not isinstance(value['operations'], list) or not isinstance(value['terminal'],dict)):
                raise AgentFailure('protocol', 'Missing or invalid native plan terminal')
            return dict(operations=value['operations']+[value['terminal']],**({'task_notes':value['task_notes']} if 'task_notes' in value else {}))
        return result.get('result', '')


class Harness:
    def __init__(self, session, agent, run_seconds=None):
        if run_seconds is not None and (type(run_seconds) not in {int,float} or not math.isfinite(run_seconds) or run_seconds<=0):
            raise Refused('Explicit diagnostic run allowance must be finite and positive')
        self.run_seconds = run_seconds
        self.session,self.agent = Path(session),agent
        self.workspace,self.path = self.session/'workspace',self.session/'state.log'
        self.state = State.load(self.path)
        if getattr(agent,'profile',None) in PROFILES and self.state.max_depth != PROFILES[agent.profile][3]:
            raise Refused('Session TODO depth differs from its selected static profile')

    def save(self):
        self.state.save(self.path)

    def requirements(self):
        goal = (self.session/'goal.txt').read_text(encoding='utf-8')
        constraints = (self.session/'memory.txt').read_text(encoding='utf-8')
        if len(goal)+len(constraints) > 20000: raise Refused('Goal/constraints too large; split the goal')
        return f'Goal:\n{goal}\nConstraints:\n{constraints}\n'

    def context(self, task, review):
        rows = [f'{t.id} | {t.parent} | {t.role} | {t.status} | needs {",".join(map(str,t.needs)) or "none"} | {t.path or "(no artifact)"}'
                for t in sorted(self.state.tasks.values(),key=lambda t:t.id)]
        evidence,candidate,parent = [],None,task
        while parent:
            if parent.reason and parent.status not in {'DONE','DROPPED'}:
                evidence.append(f'Task {parent.id}: {self.state.failure(parent)}')
            reviews = sorted((self.session/'reviews').glob(f'{parent.id}-*'),key=lambda p:p.stat().st_mtime_ns)
            if candidate is None and parent.path:
                current=self.workspace/parent.path
                for path in reversed(reviews):
                    artifact=path/parent.path
                    if path==review:continue
                    if (artifact.is_file() and (not current.is_file() or artifact.read_bytes()!=current.read_bytes()) or
                            artifact.is_dir() and files(artifact)!=files(current)):
                        candidate=artifact;break
            parent = self.state.tasks.get(parent.parent)
        children = [f'{t.id}: {t.result}'+(f'; details: {t.reason}' if t.reason else '') for t in sorted(self.state.tasks.values(),key=lambda t:t.id)
                    if t.parent == task.id and t.status == 'DONE']
        needs = []
        for id in sorted(task.needs):
            t = self.state.tasks[id]
            delivery = ' [TEST report only; no file edits promoted]' if t.role=='TEST' and not t.path else ''
            needs.append(f'{id}{delivery}: {t.summary}; {t.result}')
        return (self.requirements()+task_notes_context(self.session)+f'Assigned task {task.id}: {task.path or "(no artifact path)"} | {task.summary}\n'
                'Use your native tools for this scope. Inspect current files before acting. '
                f'This invocation has {AGENT_TURNS} session turns and {AGENT_TOOLS} ordinary tool calls; leave room for the final report. '
                'Keep checks focused. If the scope will not fit, report FAILED with completed/remaining work before the cap; do not keep expanding scratch checks. '
                'Only your own structured_output.report can complete this task. '
                f'Use task_id {task.id}, status DONE/FAILED/BLOCKED, summary, and reason for failure/blocking. '
                'related_tasks is information for planning, never an instruction to change another task.\n'
                'Other TODO paths identify separate work; do not take over their unfinished scope. '
                'Apply the goal and durable constraints to this assigned scope; do not require another unfinished TODO\'s final behavior in your focused check. '
                'Scratch .qwen/tmp is excluded from promotion; do not spend turns cleaning it before reporting.\n'
                + ('Completed children; use these recorded observations, finish only remaining parent work and give your own report. '
                   'Do not repeat completed children or search for unavailable original/backup versions:\n'+'\n'.join(children)+'\n' if children else '')
                + ('Accepted needs results:\n'+'\n'.join(needs)+'\n' if needs else '')
                + ('Earlier review artifact (unaccepted; read only if useful, not unchanged copies of other files): '+str(candidate.resolve())+'\n' if candidate else '')
                + 'Current TODO:\n'+'\n'.join(rows)+'\n'+'\n'.join(evidence))

    def plan_context(self):
        rows,evidence = [],[]
        for t in sorted(self.state.tasks.values(),key=lambda t:t.id):
            waiting = [str(id) for id in t.needs if self.state.tasks[id].status != 'DONE']
            rows.append(f'{t.id} | {t.parent} | {t.role} | {t.status} | attempts {t.attempts} | needs {",".join(map(str,t.needs)) or "none"} | {t.path} | {t.summary}'+
                        (' | waiting needs '+','.join(waiting) if waiting else ''))
            if t.reason and t.status not in {'DONE','DROPPED'}:
                label = ('prior accepted report' if t.kind in {'reopened','invalidated'} else
                         'work reported' if t.kind=='agent' else 'retained report (not current completion)')
                evidence.append(f'Task {t.id}: {self.state.failure(t)}; related tasks: '+(', '.join(map(str,t.related)) or 'none')+
                                (f'; {label}: {t.result}' if t.result else ''))
                if t.attempts >= LIMIT_ATTEMPTS:
                    evidence.append(f'Task {t.id}: no attempts remain for its own final report; use BLOCKED, not a free retry.')
                elif self.state.can_retry_parent(t):
                    evidence.append(f'Task {t.id}: children are DONE; {LIMIT_ATTEMPTS-t.attempts} attempt(s) remain. '
                        'Explicit EDIT with the exact unchanged summary resumes its remaining work and own report; FINISH alone does not. '
                        'Reuse useful unaccepted edits; do not replay children. Add a new child only for a distinct unresolved prerequisite.')
                elif self.state.needs_split(t):
                    evidence.append(f'Task {t.id}: split into smaller independent children; do not reissue the whole task.')
                if t.kind not in {'agent','reopened','invalidated'}:
                    evidence.append(f'Task {t.id}: execution/protocol failure, not proof of an implementation defect. '
                                    f'Unaccepted partial work: reviews/{t.id}-*; inspect actual files.')
        parents = self.state.allowed_add_parents()
        depths = []
        for id in parents:
            depth,parent = 1,id
            while parent: depth,parent = depth+1,self.state.tasks[parent].parent
            depths.append(f'parent {id} -> child depth {depth}')
        from vision import task_image_context
        return (self.requirements()+task_notes_context(self.session)+task_image_context(self.session)+f'Current TODO (id | parent | role | status | attempts | needs | optional path | summary; next ID {self.state.next_id}):\n'+
                '\n'.join(rows)+'\nUnresolved results:\n'+('\n'.join(evidence) or 'none')+
                '\nFAILED/BLOCKED tasks that must be resolved before FINISH (never skipped as history): '+
                (', '.join(str(t.id) for t in self.state.tasks.values() if t.status in {'FAILED','BLOCKED'}) or 'none')+
                '\nAllowed existing ADD parents: '+', '.join(map(str,parents))+
                f'\nDepth limit {self.state.max_depth}; roots have depth 1. Existing parent choices: '+'; '.join(depths)+
                '\nEDIT candidates: '+(', '.join(str(t.id) for t in self.state.tasks.values()
                    if t.status in {'PENDING','FAILED','BLOCKED'} and t.attempts < LIMIT_ATTEMPTS and
                    not any(c.parent == t.id and c.status != 'DROPPED' for c in self.state.tasks.values())) or 'none')+
                '\nParent continuation EDIT candidates (exact unchanged summary only): '+(', '.join(str(t.id) for t in self.state.tasks.values() if self.state.can_retry_parent(t)) or 'none')+
                '\nDROP candidates (require a reason): '+(', '.join(str(t.id) for t in self.state.tasks.values()
                    if t.status == 'PENDING' and not (t.attempts or t.result or t.reason) and
                    not any((c.parent == t.id or t.id in c.needs) and c.status != 'DROPPED' for c in self.state.tasks.values())) or 'none')+
                '\nREOPEN candidates (DONE, reason required): '+(', '.join(str(t.id) for t in self.state.tasks.values() if t.status == 'DONE') or 'none')+
                '\nDONE tasks with no remaining reports: '+(', '.join(str(t.id) for t in self.state.tasks.values() if t.status=='DONE' and t.attempts>=LIMIT_ATTEMPTS) or 'none')+
                f'\nLimits: {LIMIT_TASKS} tasks, {LIMIT_ATTEMPTS} worker attempts per task, {LIMIT_REPLANS-self.state.replans} planning rounds remaining. '
                'Plan only remaining work; preserve other tasks and every parent requirement. '
                'Execution order: child before parent at every depth, then the parent completes/checks its original scope and gives its own report within its remaining attempts. '
                'Workers run sequentially in dependency-aware DFS postorder: roots and siblings use creation-ID order; visit sorted needs, then children, then own work. '
                'Explicit needs can visit another branch before returning here; otherwise finish each whole subtree before the next sibling/root. '
                'Replan a failed/blocked branch before continuing elsewhere. Undeclared dependencies are not inferred. '
                'Use needs only for real prerequisites; a child must never need its parent or any ancestor. '
                'needs requires an accepted DONE result, not an attempted diagnostic. A repair cannot need the failing check or a task waiting on it; put the repair before the recheck. '
                'Final checks need the results they validate; root order alone does not preserve those dependencies after REOPEN. '
                'Independent or hierarchy-only plans are valid; do not create topology to demonstrate traversal. '
                'Children are prerequisites too, never post-checks below an unperformed repair. '
                'NEEDS replaces the needs list of an unfinished task; it does not clear FAILED/BLOCKED. '
                'Splitting prerequisite X does not replace its own result: keep needs X unless this consumer truly needs only a narrower child result. '
                'A child DONE does not complete X; root order cannot preserve a removed dependency after REOPEN. '
                'REOPEN explicitly reopens DONE with a reason, retaining scope and attempts; the harness transitively invalidates DONE dependents and ancestors. '
                'Check every affected task has attempts for its own new report; an exhausted dependent cannot be invalidated. '
                'Do not manually REOPEN those already invalidated. Never reset dependencies merely to bypass a failure. '
                'parent_id is not a topic group. Children must be narrower prerequisites, not copies of the whole parent. '
                'Choose child count, scope and role yourself. ADD immediately makes its parent WAITING: EDIT before adding children. '
                'All ADDs, including children, share one global ID sequence from the next ID; do not number roots separately. '
                'The parent may be an allowed existing ID or an earlier ADD in this batch; '
                'needs may reference tasks added later in this batch, only when genuinely required. '
                'A path is optional; TEST without a path cannot promote file edits. '
                'No automatic project tests follow a worker. related_tasks is advisory. Missing reports do not establish code defects. '
                'FINISH applies this planning batch, not task completion. BLOCKED is stop-only: no plan changes, a concrete unresolved obstacle, no promised next round.')

    def plan(self):
        if self.state.phase != 'planning':
            if self.state.replans >= LIMIT_REPLANS: raise Refused('Replanning limit exhausted')
            self.state.replans += 1
            self.state.phase = 'planning'
            self.save()
        context = self.plan_context()+f'\nPlan only. Summary limit {SUMMARY_LIMIT}; path limit {PATH_LIMIT}.\n'
        # Existing durable events retain this round's intent/rejections across interruption.
        attempts = [m for e in self.state.events if (m := re.fullmatch(
            rf'Planning attempt {self.state.replans}\.(\d+) (native|text) (started|rejected|unavailable)(?:: (.*))?',e.partition(' | ')[2]))]
        used = max((int(m[1]) for m in attempts),default=0)
        rejected = [f'{m[1]} {m[2]}: {m[4]}' for m in attempts if m[3] != 'started']
        if used and attempts[-1][3] == 'started':
            rejected.append(f'{used} {attempts[-1][2]}: interrupted before acceptance; attempt consumed, no operations committed')
        error = ('Previous attempts rejected: '+'; '.join(rejected)+'\nReturn a complete corrected plan.\n') if rejected else ''
        for attempt,structured in enumerate(PLAN_MODES,1):
            if attempt <= used: continue
            if attempt == 3 and used >= 2 and attempts and attempts[-1][3] == 'unavailable': break
            mode_name = 'native' if structured else 'text'
            label = f'Planning attempt {self.state.replans}.{attempt} {mode_name}'
            self.state.event(label+' started')
            self.save()  # Charge the invocation before starting it; restart cannot replenish it.
            review = self.session/'plans'/f'{self.state.replans}-{int(structured)}-{uuid.uuid4().hex[:8]}'
            before = clone(self.workspace,review)
            mode = ('NATIVE MODE: operations contains ADD/EDIT/DROP/NEEDS/REOPEN; terminal is required: {"op":"FINISH"} or {"op":"BLOCKED","reason":"concrete obstacle"}.'
                    if structured else
                    'TEXT MODE: output only the following line forms, using literal | separators. No introduction, explanation, Markdown or JSON. '
                    'Do not repeat existing or newly added tasks. Keep summaries within 280 characters.\n'
                    'ADD | parent_id | ALGORITHM or TEST | optional_path | short_summary\n'
                    'ADD | parent_id | ALGORITHM or TEST | optional_path | short_summary | comma_separated_needs\n'
                    'EDIT | id | short_summary\nDROP | id | reason\nNEEDS | id | comma_separated_ids_or_blank\n'
                    'REOPEN | id | reason\nFINISH\nBLOCKED | reason\n'
                    'End with exactly one FINISH or BLOCKED. Example of format only (choose your actual tasks):\n'
                    'ADD | 0 | TEST | | Check existing behavior\nFINISH')
            failure,correctable,notes = '',True,None
            try:
                response = self.agent.call('PROMPT',context+error+'\n'+mode,review,structured)
            except AgentFailure as e:
                if e.kind not in {'protocol','context'}: raise
                failure,correctable = str(e),e.kind == 'protocol'
            else:
                if files(review) != before: raise Refused('PROMPT modified files')
                try:
                    if structured:
                        if not isinstance(response,dict) or 'operations' not in response or set(response)-{'operations','task_notes'}: raise Refused('Missing native plan')
                        ops = response['operations']
                        if 'task_notes' in response:
                            notes=task_notes_text(response['task_notes'])
                            task_notes_path(self.session)
                    else: ops = parse_plan(response)
                    candidate = apply_plan(self.state,ops)
                except ValueError as e:
                    failure = str(e)
            if failure:
                detail = failure.split('; inspect ',1)[0][:500]
                rejected.append(f'{attempt} {mode_name}: '+detail)
                error = 'Previous attempts rejected: '+'; '.join(rejected)+'\nReturn a complete corrected plan.\n'
                outcome = 'rejected' if correctable else 'unavailable'
                self.state.event(label+f' {outcome}: '+detail)
                self.save()
                status('PROMPT','RETRY' if attempt < len(PLAN_MODES) and (correctable or attempt==1) else 'FAIL',failure)
                if not correctable and attempt >= 2: break
            else:
                previous,self.state = self.state,candidate
                try:
                    self.save()
                except BaseException:
                    self.state = previous
                    raise
                if notes is not None:
                    # Commit informational notes only after the validated plan is durable.
                    # A failed/interrupted note write must never replay accepted operations.
                    try:atomic(task_notes_path(self.session),notes)
                    except (OSError,ValueError) as error:
                        raise Refused('Plan accepted; task notes were not saved: '+str(error)) from error
                status('PROMPT','PASS','native structured plan' if structured else 'strict text fallback')
                return
        raise Refused('Bounded planning attempts exhausted: '+'; '.join(e[:150] for e in rejected))

    def step(self, task):
        if self.state.tasks.get(task.id) is not task or self.state.eligible() is not task:
            raise Refused('Only the next DFS postorder task can start')
        if any(t.status == 'RUNNING' for t in self.state.tasks.values()):
            raise Refused('Another agent is running')
        task.status,task.attempts = 'RUNNING',task.attempts+1
        self.state.actions += 1
        self.state.event(f'Task {task.id} started; attempt {task.attempts}')
        self.save()  # Intent survives interruption, including partial tool effects.
        review = self.session/'reviews'/f'{task.id}-{task.attempts}-{uuid.uuid4().hex[:8]}'
        candidate = None
        try:
            before = clone(self.workspace,review)
            report = self.agent.call(task.role,self.context(task,review),review,task_id=task.id)
            candidate = apply_report(self.state,task.id,report)
            merge(self.workspace,review,before,task.path if task.role == 'TEST' else None,lambda:candidate.save(self.path))
            self.state = candidate
        except (OSError,ValueError,subprocess.TimeoutExpired) as e:
            kind = e.kind if isinstance(e,AgentFailure) else ('timeout' if isinstance(e,subprocess.TimeoutExpired) else 'protocol' if isinstance(e,Refused) else 'workspace')
            reason = ' '.join(str(e).replace('|','/').split())[:560] or type(e).__name__
            if candidate is not None:
                reported = candidate.tasks[task.id]
                task.result,task.related = reported.result,reported.related
                if reported.reason:
                    reason = (reason+'; worker reported '+reported.status+': '+reported.reason)[:560]
            if task.reason and task.reason not in reason:
                reason = (reason[:280]+'; prior unresolved cause: '+task.reason)[:560]
            task.status,task.kind,task.reason = 'FAILED',kind,reason
            if kind in {'loop','stream','output','qwen','protocol','backend'} and task.attempts < LIMIT_ATTEMPTS:
                task.status = 'PENDING'
            self.state.event(f'Task {task.id}: [{kind}] {reason}; unaccepted candidate {review.name}')
            self.save()
            status(task.role,'RETRY' if task.status == 'PENDING' else 'FAILED',f'Task {task.id} | {kind} | {reason}')
            return None
        status(task.role,report['status'],f'Task {task.id} | {report["summary"]}')
        return report

    def run(self):
        with lock(self.session/'session.lock'):
            deadline = time.monotonic() + self.run_seconds if self.run_seconds is not None else None
            self.state.recover()
            self.save()
            try:
                while True:
                    s = self.state
                    if s.phase == 'blocked':
                        status('HARNESS','BLOCKED',s.reason); return 2
                    task = s.eligible()
                    if s.phase == 'blocked':
                        self.save(); continue
                    if s.phase != 'planning' and s.complete():
                        s.phase = 'complete'; self.save()
                        status('HARNESS','DONE',f'Every active task has its own DONE report | {self.workspace}')
                        return 0
                    if s.actions >= 64: raise Refused('Run action limit reached (64 delegations)')
                    if deadline is not None and time.monotonic() >= deadline:
                        s.event('Run deadline reached; committed work retained for --session resume')
                        self.save()
                        status('HARNESS','PAUSED','Run deadline reached; resume this session to continue; no task result was changed')
                        return 2
                    if s.phase == 'planning' or not s.closed or any(t.status in {'FAILED','BLOCKED'} for t in s.tasks.values()):
                        self.plan(); continue
                    if task is None: raise Refused('No eligible task; inspect unresolved TODO state')
                    self.step(task)
            except Refused as e:
                self.state.block(' '.join(str(e).replace('|','/').split())[:560])
                self.save()
                status('HARNESS','BLOCKED',self.state.reason)
                return 2


def port_free(port):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
        except OSError as e:
            raise Refused(f'Port {port} is occupied; refusing to reuse or stop an unrelated server') from e


class Server:
    def __init__(self, exe, model, context=32768, batch=512, ubatch=256, port=8080, log=None, thinking=8192, threads=8, trace=False, spec_type='none',mmproj=None):
        self.exe, self.model, self.context = str(exe), str(model), context
        self.batch, self.ubatch, self.port = batch, ubatch, port
        self.log, self.thinking, self.threads = log or ROOT / 'logs' / 'server.log', thinking, threads
        self.trace = trace
        from vision import MODEL_PROFILES,projector
        self.mmproj=Path(mmproj) if mmproj is not None else projector(MODEL_PROFILES.get(Path(model).name,'MID'))
        self.vision=False
        if spec_type not in {'none','ngram-mod','draft-mtp,ngram-mod'}:raise Refused('Unsupported speculative decoding mode')
        self.spec_type=spec_type
        self.url = f'http://127.0.0.1:{port}'
        self.process = self.handle = None

    def __enter__(self):
        port_free(self.port)
        existing = windows_servers()
        if existing:
            raise Refused('Existing llama-server PID ' + ', '.join(map(str, existing)) +
                          '; GPU ownership is unknown, even on another port. '
                          'Stop it through its owning terminal before retrying; no process was stopped')
        from vision import check_projector,pillow
        check_projector(self.model,self.mmproj);pillow()
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.log.open('w', encoding='utf-8')
        cmd = [self.exe, '-m', self.model, '--alias', MODEL_ID, '--host', '127.0.0.1',
               '--port', str(self.port), '-c', str(self.context), '-b', str(self.batch), '-ub', str(self.ubatch),
               '-np', '1', '-ngl', '99', '--split-mode', 'none', '-fa', 'on', '-ctk', 'q8_0', '-ctv', 'q8_0',
               '--jinja', '--reasoning-budget', str(self.thinking), '--reasoning-format', 'deepseek',
               '--fit', 'off', '-t', str(self.threads), '--no-context-shift']
        cmd+=['--mmproj',str(self.mmproj),'--mmproj-offload','--mmproj-device','CUDA0']
        cmd+=['--spec-type',self.spec_type]
        if self.spec_type!='none':
            cmd+=['--spec-draft-n-max','2','--spec-draft-p-min','0.025',
                  '--spec-ngram-mod-n-min','32','--spec-ngram-mod-n-max','64','--spec-ngram-mod-n-match','16']
        if self.trace:
            cmd += ['--verbosity', '5']
        self.handle.write('SERVER | COMMAND | '+subprocess.list2cmdline(cmd)+'\n');self.handle.flush()
        try:
            cache = ROOT / 'runtime' / 'cache' / 'cuda'
            cache.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env['CUDA_CACHE_PATH'] = str(cache)
            self.process = start_process(cmd, stdout=self.handle, stderr=subprocess.STDOUT, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
            for tick in range(180):
                if self.process.poll() is not None:
                    raise Refused(f'Selected model/projector GPU configuration failed to load; no CPU/profile/context fallback was applied. See {self.log}')
                try:
                    if request(self.url + '/health', timeout=2).get('status') == 'ok':
                        if request(self.url+'/props',timeout=5).get('modalities',{}).get('vision') is not True:
                            raise Refused('Pinned model loaded without required vision capability; no text-only fallback')
                        self.vision=True
                        status('LLAMA', 'PASS', f'one model loaded | context {self.context} | port {self.port}')
                        return self
                except Refused:
                    raise
                except (OSError, ValueError):
                    pass
                if tick % 20 == 0:
                    status('LLAMA', 'LOAD', f'waiting for health | {tick * 2}s | {self.log}')
                time.sleep(2)
            raise Refused(f'Model load timed out; see {self.log}')
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.process:
                stop_process(self.process)
        finally:
            if self.handle:
                self.handle.close()


def download(url, dest, sha256):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        with dest.open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() == sha256:
                status('DOWNLOAD', 'CACHED', dest.name)
                return
        raise Refused(f'Existing file checksum differs: {dest}; move it aside before retrying')
    part = dest.with_name(dest.name + '.part')
    hasher = hashlib.sha256()
    start, last, size = time.monotonic(), 0, 0
    try:
        with urllib.request.urlopen(url, timeout=60) as r, part.open('wb') as f:
            total = int(r.headers.get('Content-Length', 0))
            while chunk := r.read(1024 * 1024):
                f.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
                now = time.monotonic()
                if now - last >= 2:
                    progress = f'{size / 1048576:.1f} MiB' + (f' / {total / 1048576:.1f} MiB ({size * 100 / total:.1f}%)' if total else '')
                    status('DOWNLOAD', 'RUN', f'{dest.name} | {progress} | {size / 1048576 / max(now - start, .01):.1f} MiB/s')
                    last = now
        if hasher.hexdigest() != sha256:
            raise Refused(f'SHA-256 mismatch: {dest.name}')
        os.replace(part, dest)
        status('DOWNLOAD', 'PASS', f'{dest.name} | SHA-256 verified')
    finally:
        part.unlink(missing_ok=True)


def gguf_metadata(path):
    """Read GGUF scalar metadata without loading tensors or tokenizer arrays."""
    result = {}
    formats = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i', 6: 'f', 7: '?', 10: 'Q', 11: 'q', 12: 'd'}
    with Path(path).open('rb') as f:
        def unpack(fmt):
            size = struct.calcsize('<' + fmt)
            raw = f.read(size)
            if len(raw) != size:
                raise Refused('Truncated GGUF metadata')
            return struct.unpack('<' + fmt, raw)[0]
        def string(keep=True):
            n = unpack('Q')
            if n > 100_000_000:
                raise Refused('Invalid GGUF string length')
            if keep:
                return f.read(n).decode('utf-8')
            f.seek(n, 1)
        def value(kind, keep=True):
            if kind == 8:
                return string(keep)
            if kind == 9:
                child, count = unpack('I'), unpack('Q')
                if count > 10_000_000 or child == 9:
                    raise Refused('Invalid GGUF array')
                if child in formats:
                    f.seek(struct.calcsize('<' + formats[child]) * count, 1)
                else:
                    for _ in range(count):
                        value(child, False)
                return None
            if kind not in formats:
                raise Refused('Unknown GGUF metadata type')
            return unpack(formats[kind])
        if f.read(4) != b'GGUF' or unpack('I') != 3:
            raise Refused('Expected GGUF v3')
        unpack('Q')
        count = unpack('Q')
        if count > 100000:
            raise Refused('Invalid metadata count')
        for _ in range(count):
            key, kind = string(), unpack('I')
            result[key] = value(kind)
    arch = result.get('general.architecture')
    if arch != 'qwen35' or not result.get('tokenizer.chat_template') or not result.get(arch + '.context_length'):
        raise Refused('Expected qwen35 architecture, context metadata and native chat template')
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description='Backtrackify Harness | Plan. Build. Test. Backtrack. Repeat.')
    sub = p.add_subparsers(dest='command', required=True)
    dl = sub.add_parser('download', help=argparse.SUPPRESS)
    dl.add_argument('url'); dl.add_argument('dest', type=Path); dl.add_argument('sha256')
    meta = sub.add_parser('metadata', help='Inspect selected GGUF metadata')
    meta.add_argument('model', type=Path)
    meta.add_argument('--context', type=int, default=8192)
    for name in ('run', 'chat', 'qwen', 'smoke', 'load', 'longflow', 'diagnose'):
        a = sub.add_parser(name)
        a.add_argument('--server', type=Path, required=True)
        a.add_argument('--model', type=Path, required=True)
        a.add_argument('--mmproj',type=Path,help='Matching pinned F16 projector; defaults to this installation profile asset')
        a.add_argument('--qwen', type=Path, required=True)
        a.add_argument('--profile', choices=PROFILES, default='MID')
        a.add_argument('--context', type=int, default=32768)
        a.add_argument('--batch', type=int, default=512)
        a.add_argument('--ubatch', type=int, default=256)
        a.add_argument('--threads', type=int, default=8)
        a.add_argument('--port', type=int, default=8080)
        a.add_argument('--spec-type',choices=('none','ngram-mod','draft-mtp,ngram-mod'))
        a.add_argument('--agent-seconds',type=int,help='Finite per-agent wall allowance; defaults to the hardware profile')
        if name == 'run':
            a.add_argument('goal', nargs='?')
            a.add_argument('--project', type=Path)
            a.add_argument('--session', type=Path)
        if name in {'run','longflow'}:
            a.add_argument('--visualize', action='store_true', help='Open a separate read-only live TODO tree terminal')
        if name == 'chat':
            a.add_argument('--chat-port', type=int, default=8081)
        if name == 'qwen':
            a.add_argument('cli_args', nargs=argparse.REMAINDER)
        if name == 'diagnose':
            a.add_argument('--source-snapshot', type=Path, required=True)
    args = p.parse_args(argv)
    if args.command == 'download':
        download(args.url, args.dest, args.sha256)
        return 0
    if args.command == 'metadata':
        info = gguf_metadata(args.model)
        trained = info['qwen35.context_length']
        if not 8192 <= args.context <= trained:
            raise Refused(f'Context must be between 8192 and GGUF training limit {trained}')
        status('GGUF', 'PASS', f"{info['general.architecture']} | training context {trained} | {info.get('general.name', args.model.name)}")
        return 0
    for role in ROLES:
        generation(args.profile, role, args.context)
    if args.command in {'smoke', 'longflow', 'diagnose'}:
        from tests.e2e import require_snapshot
        require_snapshot()
    metadata = gguf_metadata(args.model)
    spec_type=args.spec_type or SPECULATION[args.profile]
    if 'draft-mtp' in spec_type and not metadata.get('qwen35.nextn_predict_layers',0):
        raise Refused('draft-mtp requires embedded MTP tensors; choose --spec-type none or ngram-mod for this model')
    if args.agent_seconds is not None and args.agent_seconds<=0:raise Refused('Agent timeout must be positive seconds')
    if not 8192 <= args.context <= metadata['qwen35.context_length']:
        raise Refused('Configured context is outside the selected GGUF training limits')
    if not 1 <= args.port <= 65535 or not 1 <= args.ubatch <= args.batch:
        raise Refused('Invalid port or batch sizes')
    if args.command != 'load':
        status('ISOLATION', 'LIMIT', 'Workspace copies are not an OS sandbox. Agent shell tools run with your privileges; use trusted projects or a VM.')
    from backend import connection
    args.spec_type=spec_type
    if args.command == 'chat':
        from chat import launch
        return launch(args,lambda:connection(args,Server,backend_lock))
    with connection(args,Server,backend_lock) as server:
        if args.command == 'load':
            return 0
        if args.command == 'qwen':
            session = ROOT / 'sessions' / ('interactive-' + uuid.uuid4().hex[:8])
            return Qwen(args.qwen, args.profile, args.context, server.url, session,args.agent_seconds,vision=server.vision).call(
                'ALGORITHM', '', Path.cwd(), interactive=True, cli_args=args.cli_args[1:] if args.cli_args[:1] == ['--'] else args.cli_args)
        if args.command == 'smoke':
            from tests.e2e import smoke
            return smoke(args, server)
        if args.command == 'longflow':
            from tests.longflow import longflow
            return longflow(args, server)
        if args.command == 'diagnose':
            from tests.diagnose import diagnose
            return diagnose(args, server)
        session = args.session or ROOT / 'sessions' / (time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
        session = session.resolve()
        if not (session / 'state.log').exists():
            if session.exists():
                raise Refused('New session path already exists; refusing to overwrite it')
            if not args.goal or len(args.goal) > 6000:
                raise Refused('Provide a goal of at most 6000 characters, or an existing --session to resume')
            session.mkdir(parents=True)
            if args.project:
                source = args.project.resolve()
                if not source.is_dir() or source == session or source in session.parents:
                    raise Refused('Project must exist and cannot contain its session directory')
                clone(source, session / 'workspace')
            else:
                (session / 'workspace').mkdir()
            atomic(session / 'goal.txt', args.goal + '\n')
            shared = ROOT / 'memory.txt'
            atomic(session / 'memory.txt', 'Constraints and durable findings:\n' + (shared.read_text(encoding='utf-8') if shared.exists() else 'Keep implementation simple. Preserve the original goal.\n'))
            State(max_depth=PROFILES[args.profile][3]).save(session / 'state.log')
        else:
            if args.goal or args.project:
                raise Refused('Resume uses persisted goal/workspace; omit goal and --project')
        status('SESSION', 'RUN', str(session))
        from visualize import open_view, view_channel, viewer_active
        environment=args.project or ROOT
        try:
            if args.visualize or viewer_active(view_channel(environment,session)):open_view(session,environment)
        except (OSError,ValueError) as e: status('VIEW','WARN',f'Cannot open viewer; execution continues: {e}')
        return Harness(session, Qwen(args.qwen, args.profile, args.context, server.url, session,args.agent_seconds,vision=server.vision)).run()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        status('HARNESS', 'BLOCKED', str(exc))
        raise SystemExit(2)
    except KeyboardInterrupt:
        status('HARNESS', 'INTERRUPTED', 'State retained; inspect session and resume at the next safe boundary')
        raise SystemExit(130)
