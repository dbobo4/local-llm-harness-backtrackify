"""User-owned command registration and pinned-installation preflight; no model calls."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.request
import uuid
import zipfile
import sys

from harness import ROOT, Refused, atomic, lock

# Measured central-directory sizes of the SHA-pinned setup archives, bytes.
ARCHIVES = {'qwen': (65194084,199817148), 'llama': (146449775,187594013),
            'cuda': (390970417,512722768)}
VC_URL='https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist/'


def install_vision(model):
    from vision import MODEL_PROFILES,PROJECTORS,PILLOW_HASHES,PILLOW_VERSION,projector,check_projector,pillow
    from harness import download
    profile=MODEL_PROFILES.get(Path(model).name)
    if not profile:raise Refused('No pinned vision assets for selected model')
    pin=PROJECTORS[profile];target=projector(profile,ROOT)
    download('https://huggingface.co/'+pin[0]+'/resolve/'+pin[1]+'/mmproj-F16.gguf',target,pin[3])
    check_projector(model,target)
    abi=str(sys.version_info.major)+str(sys.version_info.minor)
    if abi not in PILLOW_HASHES:raise Refused('No pinned Windows x64 image decoder wheel for this Python version')
    name='pillow-'+PILLOW_VERSION+'-cp'+abi+'-cp'+abi+'-win_amd64.whl'
    wheel=ROOT/'runtime/vision'/name
    if wheel.exists():download('',wheel,PILLOW_HASHES[abi])
    else:
        with urllib.request.urlopen('https://pypi.org/pypi/Pillow/'+PILLOW_VERSION+'/json',timeout=30) as response:data=json.load(response)
        url=None
        for item in data['urls']:
            if item['filename']==name and item['digests']['sha256']==PILLOW_HASHES[abi] and item['url'].startswith('https://files.pythonhosted.org/'):
                url=item['url'];break
        if not url:raise Refused('Pinned Pillow wheel not found; no alternate decoder was installed')
        download(url,wheel,PILLOW_HASHES[abi])
    destination=ROOT/'runtime/vision/python'
    unpack(wheel,destination)
    sys.path.insert(0,str(destination));pillow()


def shared_instructions(root):
    """Setup creates user guidance once; upgrades and uninstall never rewrite it."""
    path=Path(root)/'memory.txt'
    if path.exists() and not path.is_file():raise Refused('ROOT memory.txt is not a file; existing path preserved')
    try:
        with path.open('x',encoding='utf-8',newline='\n') as handle:
            handle.write('Keep implementation simple.\nPreserve the original user goal.\n')
            handle.flush();os.fsync(handle.fileno())
    except FileExistsError:
        if not path.is_file():raise Refused('ROOT memory.txt is not a file; existing path preserved')


def vc_runtime(load=None):
    load=load or (lambda path:ctypes.WinDLL(str(path),winmode=0x800))
    for name in ('VCRUNTIME140.dll','VCRUNTIME140_1.dll','MSVCP140.dll'):
        try:load(Path(os.environ['WINDIR'])/'System32'/name)
        except OSError as error:raise Refused('Missing/unloadable x64 Visual C++ v14 runtime: '+name+
            '. Install the official current x64 redistributable, then rerun setup: '+VC_URL) from error


def remaining_space(assets,model,url,size=None):
    assets=Path(assets);need=0
    for name,(packed,unpacked) in ARCHIVES.items():
        archive={'qwen':'qwen-code-win-x64.zip','llama':'llama-b10636-bin-win-cuda-13.3-x64.zip',
                 'cuda':'cudart-llama-bin-win-cuda-13.3-x64.zip'}[name]
        if not (assets/'runtime/downloads'/archive).exists():need+=packed
        if not (assets/'runtime'/name/'.complete').exists():need+=unpacked
    if not model.exists():
        if size is None:
            with urllib.request.urlopen(urllib.request.Request(url,method='HEAD'),timeout=30) as response:
                size=int(response.headers.get('Content-Length',0))
        if not size:raise Refused('Cannot establish the pinned model download size; no download started')
        need+=size
    # Downloads and extraction stage beside their destination, never on TEMP.
    return need


def disk_check(assets,model,url,usage=shutil.disk_usage,size=None,root=ROOT):
    from vision import MODEL_PROFILES,PROJECTORS,projector
    extra=0;profile=MODEL_PROFILES.get(Path(model).name)
    if profile and not projector(profile,root).exists():extra=PROJECTORS[profile][4]
    demands={}
    for target,amount in ((Path(assets),remaining_space(assets,Path(model),url,size)),(root,128*1024**2+extra)):
        while not target.exists():target=target.parent
        drive=target.anchor.casefold()
        previous=demands.get(drive,(target,0));demands[drive]=(target,previous[1]+amount)
    for target,amount in demands.values():
        free=usage(target).free
        if free<amount:raise Refused(f'Insufficient space on {target.anchor}: need {amount/1024**3:.2f} GiB, available {free/1024**3:.2f} GiB; pinned profile unchanged')


def unpack(archive,destination):
    destination=Path(destination).resolve()
    if (destination/'.complete').is_file():
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                if item.is_dir():continue
                target=(destination/item.filename).resolve()
                if not target.is_relative_to(destination) or not target.is_file():raise Refused('Incomplete verified runtime: '+str(target))
                with z.open(item) as expected,target.open('rb') as actual:
                    if hashlib.file_digest(expected,'sha256').digest()!=hashlib.file_digest(actual,'sha256').digest():
                        raise Refused('Existing runtime differs from pinned archive; preserved: '+str(target))
        return
    if destination.exists():raise Refused(f'Incomplete runtime directory: {destination}. Preserve/move it aside and rerun setup')
    stage=destination.with_name(destination.name+'.part-'+uuid.uuid4().hex)
    try:
        stage.mkdir(parents=True)
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                path=(stage/item.filename).resolve()
                if not path.is_relative_to(stage) or item.external_attr>>16&0o170000==0o120000:
                    raise Refused('Unsafe archive member: '+item.filename)
            z.extractall(stage)
        atomic(stage/'.complete',Path(archive).name+'\n');stage.rename(destination)
    finally:
        if stage.exists():
            if stage.parent!=destination.parent or not stage.name.startswith(destination.name+'.part-'):
                raise Refused('Unsafe extraction cleanup target')
            shutil.rmtree(stage)


def required(assets,model,root=None):
    from vision import MODEL_PROFILES,projector
    root=Path(root) if root is not None else Path(assets)
    return [Path(assets)/p for p in ('runtime/qwen/.complete','runtime/llama/.complete','runtime/cuda/.complete',
        'runtime/qwen/qwen-code/node/node.exe','runtime/qwen/qwen-code/lib/cli.js','runtime/llama/llama-server.exe',
        'runtime/cuda/cudart64_13.dll','runtime/cuda/cublas64_13.dll','runtime/cuda/cublasLt64_13.dll')]+[Path(model),projector(MODEL_PROFILES.get(Path(model).name,'MID'),root),root/'runtime/vision/python/.complete']


def readiness(root,assets,model,commit=False):
    missing=[str(p) for p in required(assets,model,root) if not p.is_file()]
    if missing:raise Refused('Incomplete installation: '+', '.join(missing)+'. Run backtrackify setup; external assets are never repaired in place')
    static=sorted({Path(model),*(p for name in ARCHIVES for p in (Path(assets)/'runtime'/name).rglob('*') if p.is_file())})
    for path in sorted((root/'runtime/vision').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':static.append(path)
    observed=(root/'local.ps1').read_bytes()+''.join(str(p.resolve())+f' | {p.stat().st_size} | {p.stat().st_mtime_ns}\n' for p in static).encode()
    signature=hashlib.sha256(observed).hexdigest()
    path=root/'runtime/installed.log'
    if commit:atomic(path,'Backtrackify ready 1\n'+signature+'\n')
    elif not path.exists() or path.read_text().splitlines()!=['Backtrackify ready 1',signature]:
        raise Refused('Setup readiness is missing or configuration changed. Run .\\backtrackify.cmd setup once')


class UserPath:
    def machine(self):
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
            return winreg.QueryValueEx(key,'Path')[0]

    def read(self):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
            try:return winreg.QueryValueEx(key,'Path')
            except FileNotFoundError:return '',winreg.REG_EXPAND_SZ

    def write(self,value,kind):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
            winreg.SetValueEx(key,'Path',0,kind,value)
        result=ctypes.c_size_t()
        notify=ctypes.windll.user32.SendMessageTimeoutW
        notify.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_size_t)]
        notify(0xffff,0x1a,0,'Environment',2,5000,ctypes.byref(result))


def same(a,b):return os.path.normcase(os.path.abspath(os.path.expandvars(str(a).strip('" '))))==os.path.normcase(str(Path(b).resolve()))


def shim(root,name):
    return '@echo off\nsetlocal DisableDelayedExpansion\n"'+str(root/name).replace('%','%%')+'" %*\n'


def registration(root,home,store=None,remove=False,check=False,search=None):
    """Only our shims and exact owned PATH token may be removed; never user data."""
    root=Path(root).resolve();home=Path(home);bin=home/'bin';owner=home/'install.log';store=store or UserPath()
    raw,kind=store.read();owned=owner.read_text(encoding='utf-8').splitlines() if owner.exists() else []
    def write_path(value):
        if store.read()!=(raw,kind):raise Refused('User PATH changed during this operation; preserved. Rerun setup/uninstall')
        store.write(value,kind)
    if owned and (len(owned)!=3 or owned[0]!='Backtrackify commands 1' or
                  owned[2] not in {'PATH ADDED','PATH PRESERVED'} or not same(owned[1],root)):
        raise Refused('Command registration belongs to another installation: '+str(owner))
    for name in ('qwen.cmd','backtrackify.cmd'):
        path=bin/name
        if path.exists() and (not owned or path.read_text(encoding='utf-8')!=shim(root,name)):
            raise Refused('Command shim is not owned or was modified; preserved: '+str(path))
    if remove:
        if not owned:return 'No owned registration; all files and PATH entries preserved'
        if owned[2]=='PATH ADDED':
            pieces=raw.split(';');indices=[i for i,v in enumerate(pieces) if v and same(v,bin)]
            if len(indices)>1:raise Refused('Duplicate registration PATH entries; resolve ambiguity before uninstall')
            if indices:
                i=indices[0];pieces.pop(i);updated=';'.join(pieces)
                write_path(updated)
        for name in ('qwen.cmd','backtrackify.cmd'):(bin/name).unlink(missing_ok=True)
        owner.unlink();return 'Owned commands unregistered; configuration, models, sessions, logs, snapshots and source preserved'
    paths=search if search is not None else os.environ.get('PATH','')+';'+raw+';'+store.machine()
    for directory in paths.split(';'):
        if not directory:continue
        for name in ('qwen','backtrackify'):
            for ext in ('.exe','.cmd','.bat','.ps1','.com',''):
                candidate=Path(os.path.expandvars(directory.strip('"')))/(name+ext)
                if candidate.is_file() and not any(same(candidate,p) for p in (root/(name+'.cmd'),bin/(name+'.cmd'))):
                    raise Refused('Command-name conflict; existing command preserved: '+str(candidate)+'. Resolve its PATH registration explicitly, then rerun setup')
    if check:return 'Registration preflight passed'
    bin.mkdir(parents=True,exist_ok=True)
    added=not any(v and same(v,bin) for v in raw.split(';'))
    before={name:(bin/name).read_bytes() if (bin/name).exists() else None for name in ('qwen.cmd','backtrackify.cmd')}
    old=owner.read_bytes() if owner.exists() else None
    try:
        for name in before:atomic(bin/name,shim(root,name))
        atomic(owner,'Backtrackify commands 1\n'+str(root)+'\n'+('PATH ADDED' if added or owned and owned[2]=='PATH ADDED' else 'PATH PRESERVED')+'\n')
        if added:write_path(raw+(';' if raw else '')+str(bin))
        elif store.read()!=(raw,kind):raise Refused('User PATH changed during registration; preserved. Rerun setup')
    except BaseException:
        for name,value in before.items():
            if value is None:(bin/name).unlink(missing_ok=True)
            else:atomic(bin/name,value)
        if old is None:owner.unlink(missing_ok=True)
        else:atomic(owner,old)
        raise
    return 'Commands registered for this user. Open a fresh terminal; existing Windows Terminal processes may retain their old environment'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('preflight','check','ready','register','check-registration','uninstall','unpack','vision'))
    p.add_argument('paths',nargs='*');a=p.parse_args();root=ROOT
    home=Path(os.environ['LOCALAPPDATA'])/'Backtrackify'
    if a.action=='unpack':unpack(*a.paths);return
    if a.action=='vision':install_vision(Path(a.paths[0]));return
    if a.action in {'register','check-registration','uninstall'}:
        with lock(home/'registration.lock'):
            if a.action=='uninstall':
                with lock(root/'runtime/install.lock'),lock(root/'runtime/shared-start.lock'),lock(root/'runtime/backend.lock'):
                    print(registration(root,home,remove=True))
                    (root/'runtime/installed.log').unlink(missing_ok=True)
            else:print(registration(root,home,check=a.action=='check-registration'))
        return
    assets,model=map(Path,a.paths[:2])
    if a.action=='preflight':vc_runtime();disk_check(assets,model,a.paths[2]);return
    if a.action=='ready':shared_instructions(root)
    readiness(root,assets,model,commit=a.action=='ready')


if __name__=='__main__':
    try:main()
    except (OSError,ValueError) as error:print('Backtrackify | INSTALL | BLOCKED | '+str(error));raise SystemExit(2)
