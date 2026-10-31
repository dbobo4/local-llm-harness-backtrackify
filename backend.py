"""One temporary backend owner for interactive CLI and browser chat clients."""
import argparse
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from harness import ROOT, Server, PROFILES, Refused, atomic, backend_lock, lock, request, status


def identity(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


@contextlib.contextmanager
def startup_lock(path):
    deadline=time.monotonic()+420;notified=False
    while True:
        guard=lock(path)
        try:guard.__enter__();break
        except Refused:
            if time.monotonic()>deadline:raise Refused('Backend startup/shutdown remained busy; inspect logs/shared-backend.log')
            if not notified:status('BACKEND','WAIT','another client is starting/stopping the shared model');notified=True
            time.sleep(.25)
    try:yield
    finally:guard.__exit__(None,None,None)


def launch(command):
    """Explorer, rather than either client's kill-on-close Job, owns the process."""
    if os.name!='nt':return subprocess.Popen(command,start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    import base64
    quote=lambda v:"'"+str(v).replace("'","''")+"'"
    script=("$ErrorActionPreference='Stop'; $s=New-Object -ComObject Shell.Application; $h=0; "
        "$d=$s.Windows().FindWindowSW(0,0,8,[ref]$h,1); if(!$d){throw 'Interactive Explorer desktop unavailable'}; "
        "$d.Document.Application.ShellExecute("+quote(command[0])+','+quote(subprocess.list2cmdline(command[1:]))+','+
        quote(ROOT)+",'open',0)")
    result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode()],
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    if result.returncode:raise Refused('Cannot launch independent backend: '+result.stderr.decode(errors='replace')[:400])


class SharedBackend:
    def __init__(self,config,root=ROOT,launcher=launch):
        self.config,self.root,self.launcher=config,root,launcher
        self.lease=None

    def __enter__(self):
        path=self.root/'runtime/shared-backend.log'
        with startup_lock(self.root/'runtime/shared-start.lock'):
            def locate(starting=False):
                try:
                    fingerprint,url=path.read_text(encoding='utf-8').splitlines()
                    if url.startswith('ERROR | '):
                        if starting:raise Refused(url[8:])
                        return None
                    info=request(url+'/owner',timeout=2)
                    self.vision=info.get('vision') is True
                    if info.get('identity')!=fingerprint:raise Refused('Shared backend ownership response mismatch')
                    if fingerprint!=identity(self.config):raise Refused('An interactive backend uses different model/profile settings; close its clients before changing configuration')
                    return url
                except (OSError,ValueError) as error:
                    if isinstance(error,Refused):raise
                    return None
            self.url=locate()
            if self.url is None:
                status('BACKEND','LOAD','one shared local model; see logs/server.log')
                command=[sys.executable,'-B',str(Path(__file__).resolve()),'--owner',json.dumps(self.config,separators=(',',':'))]
                self.launcher(command)
                deadline=time.monotonic()+420
                while self.url is None:
                    if time.monotonic()>deadline:raise Refused('Backend owner did not become ready; inspect logs/shared-backend.log and logs/server.log')
                    time.sleep(.25);self.url=locate(True)
                status('BACKEND','READY','shared model; CLI/chat requests are serialized')
            self.lease=urllib.request.urlopen(self.url+'/lease',timeout=15)
            # Heartbeats only detect an abruptly closed client; inference has its own budgets.
        return self

    def __exit__(self,*_):
        if self.lease:self.lease.close();self.lease=None


def owner(config,root=ROOT,server_factory=Server,idle_seconds=5):
    clients=0;last=time.monotonic();guard=threading.Lock();inference=threading.Lock()
    token=secrets.token_urlsafe(24);fingerprint=identity(config)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.0'
        def log_message(self,*_):pass
        def do_GET(self):
            nonlocal clients,last
            if self.path==f'/{token}/owner':
                data=json.dumps(dict(identity=fingerprint,vision=getattr(model,'vision',False))).encode();self.send_response(200);self.end_headers();self.wfile.write(data);return
            if self.path!=f'/{token}/lease':self.send_error(403);return
            with guard:clients+=1;last=time.monotonic()
            try:
                self.send_response(200);self.end_headers();self.wfile.flush()
                while True:self.wfile.write(b'.');self.wfile.flush();time.sleep(.5)
            except (OSError,ConnectionError):pass
            finally:
                with guard:clients-=1;last=time.monotonic()
        def do_POST(self):
            if not self.path.startswith(f'/{token}/'):self.send_error(403);return
            sent=False
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=16*1024**2:raise Refused('Invalid backend request size')
                body=self.rfile.read(size)
                # These endpoints read the template/vocabulary, not the model's
                # inference slot. Do not make their short client deadlines wait
                # behind another client's long generation.
                metadata=self.path[len(token)+1:] in {'/apply-template','/tokenize','/detokenize','/v1/chat/completions/input_tokens'}
                with contextlib.nullcontext() if metadata else inference:
                    conn=http.client.HTTPConnection('127.0.0.1',model_port,timeout=7200)
                    try:
                        conn.request('POST',self.path[len(token)+1:],body,{'Content-Type':'application/json'})
                        response=conn.getresponse();self.send_response(response.status);sent=True
                        self.send_header('Content-Type',response.getheader('Content-Type','application/json'));self.end_headers()
                        while block:=response.read1(65536):self.wfile.write(block);self.wfile.flush()
                    finally:conn.close()
            except (OSError,ValueError,http.client.HTTPException) as error:
                # A disconnected client does not own the shared model process.
                print('BACKEND | REQUEST ERROR | '+str(error),flush=True)
                if not sent:
                    with contextlib.suppress(OSError):self.send_error(502,str(error))
                self.close_connection=True
    class Listener(ThreadingHTTPServer):
        def get_request(self):
            nonlocal last
            connection=super().get_request()
            with guard:last=time.monotonic()
            return connection
    with contextlib.ExitStack() as closing,backend_lock(),Listener(('127.0.0.1',config['port']),Handler) as listener:
        listener.daemon_threads=True
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));model_port=probe.getsockname()[1]
        kwargs=dict(config);kwargs['port']=model_port;kwargs.pop('gpu',None)
        with server_factory(**kwargs) as model:
            last=time.monotonic()  # Loading is not client idleness.
            url=f'http://127.0.0.1:{listener.server_port}/{token}'
            atomic(root/'runtime/shared-backend.log',fingerprint+'\n'+url+'\n')
            listener.timeout=.25
            while True:
                process=getattr(model,'process',None)
                if process is not None and process.poll() is not None:
                    raise Refused('Shared model process exited; restart clients; checkpoints and conversations are retained')
                listener.handle_request()
                with guard:
                    idle=clients==0 and time.monotonic()-last>idle_seconds
                if idle:
                    try:closing.enter_context(lock(root/'runtime/shared-start.lock'))
                    except Refused:continue
                    with guard:
                        if clients==0 and time.monotonic()-last>idle_seconds:break
                    closing.close()
    # Descriptor is evidence, not proof of liveness. Clients always authenticate it.


@contextlib.contextmanager
def connection(args,server_factory=Server,ownership=backend_lock):
    config=dict(exe=str(args.server.resolve()),model=str(args.model.resolve()),context=args.context,batch=args.batch,ubatch=args.ubatch,
        port=args.port,thinking=PROFILES[args.profile][0],threads=args.threads,spec_type=args.spec_type or 'none')
    from vision import MODEL_PROFILES,projector
    config['mmproj']=str(getattr(args,'mmproj',None) or projector(MODEL_PROFILES.get(args.model.name,'MID')))
    if args.command in {'qwen','chat'}:
        config['gpu']=os.environ.get('CUDA_VISIBLE_DEVICES','default')
        with SharedBackend(config) as backend:
            if not backend.vision:raise Refused('Shared backend lacks required vision capability; close its clients and run setup')
            yield backend
    else:
        with ownership(),server_factory(**config,trace=args.command in {'smoke','diagnose'}) as backend:yield backend


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--owner',required=True);args=p.parse_args()
    (ROOT/'logs').mkdir(exist_ok=True)
    with (ROOT/'logs/shared-backend.log').open('a',encoding='utf-8') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        config=json.loads(args.owner)
        os.environ['CUDA_VISIBLE_DEVICES']=config['gpu']
        os.environ['PATH']=str(Path(config['exe']).parent.parent/'cuda')+os.pathsep+os.environ['PATH']
        try:owner(config)
        except (OSError,ValueError) as error:
            atomic(ROOT/'runtime/shared-backend.log',identity(config)+'\nERROR | '+str(error).replace('\n',' ')+'\n')
            print('BACKEND | BLOCKED | '+str(error),flush=True);return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
