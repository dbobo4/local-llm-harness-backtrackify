import os
import contextlib
import io
from pathlib import Path
import re
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import install
from harness import Refused, atomic, lock


def installation_environment(base,home):
    """Only trusted executables and this fixture's profile/temp; no host settings."""
    base,home=Path(base),Path(home);windows=Path(os.environ['SystemRoot']);system=windows/'System32'
    temp=base/'temp';roaming=home/'Roaming'
    temp.mkdir(exist_ok=True);roaming.mkdir(parents=True,exist_ok=True)
    return dict(SystemRoot=str(windows),WINDIR=str(windows),COMSPEC=str(system/'cmd.exe'),
        PATH=os.pathsep.join(map(str,[Path(sys.executable).parent,system,system/'WindowsPowerShell/v1.0'])),
        PATHEXT='.COM;.EXE;.BAT;.CMD',USERPROFILE=str(home),LOCALAPPDATA=str(home),APPDATA=str(roaming),
        TEMP=str(temp),TMP=str(temp),PYTHONIOENCODING='utf-8',PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')


class PathStore:
    def __init__(self,value=''):self.value=value;self.kind=2;self.writes=0;self.system=''
    def machine(self):return self.system
    def read(self):return self.value,self.kind
    def write(self,value,kind):self.value=value;self.kind=kind;self.writes+=1


class InstallTests(unittest.TestCase):
    def test_setup_rejects_interpreters_without_a_pinned_decoder_before_hardware_or_downloads(self):
        import sysconfig
        import vision
        script=(install.ROOT/'setup.ps1').read_text(encoding='utf-8')
        code=re.search(r'\$Version = & python -c "([^"]+)" \$Root',script)[1]
        self.assertLess(script.index('$Version = & python'),script.index('$Detected = Detect'))
        for major,minor,implementation,bits,threaded,expected in (
                (3,12,'cpython',64,False,1),(3,13,'cpython',64,False,1),(3,14,'cpython',64,False,1),
                (3,11,'cpython',64,False,0),(3,15,'cpython',64,False,0),(4,0,'cpython',64,False,0),
                (3,14,'pypy',64,False,0),(3,14,'cpython',32,False,0),(3,14,'cpython',64,True,0)):
            with self.subTest(version=(major,minor),implementation=implementation,bits=bits,threaded=threaded):
                output=io.StringIO()
                with patch('sys.version_info',SimpleNamespace(major=major,minor=minor)),patch('sys.implementation.name',implementation),\
                     patch('sys.maxsize',2**(bits-1)-1),patch('sysconfig.get_config_var',return_value=threaded),\
                     patch('sys.argv',['-c',str(install.ROOT)]),patch('sys.path',list(sys.path)),contextlib.redirect_stdout(output):
                    exec(code,{})
                self.assertEqual(output.getvalue().strip(),str(expected))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Backtrackify install spaces ')
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.root=self.base/'source app';self.root.mkdir();self.home=self.base/'user home'
        self.assets=self.base/'shared assets';self.model=self.assets/'models/model.gguf'

    def register(self,store,**kwargs):return install.registration(self.root,self.home,store,search='',**kwargs)

    @unittest.skipUnless(os.name=='nt','Windows setup environment')
    def test_install_child_environment_does_not_inherit_host_path_or_override_flags(self):
        hostile=dict(PATH=str(self.base/'foreign commands'),BACKTRACKIFY_TEST_ROOT=str(self.base),
                     QWEN_HOME='foreign profile',PYTHONPATH='foreign imports',PYTHONHOME='foreign Python',
                     LOCALAPPDATA='foreign registration',TEMP='foreign temp')
        with patch.dict(os.environ,hostile):
            before=dict(os.environ);env=installation_environment(self.base,self.home)
            self.assertEqual(dict(os.environ),before)
        self.assertNotIn(hostile['PATH'],env['PATH'])
        for key in ('BACKTRACKIFY_TEST_ROOT','QWEN_HOME','PYTHONPATH','PYTHONHOME'):self.assertNotIn(key,env)
        for key in ('USERPROFILE','LOCALAPPDATA','APPDATA','TEMP','TMP'):
            self.assertTrue(Path(env[key]).is_relative_to(self.base),key)
        self.assertTrue(Path(env['COMSPEC']).is_file());self.assertTrue((Path(env['PATH'].split(os.pathsep)[0])/'python.exe').is_file())

    def test_register_is_persistent_idempotent_and_preserves_raw_path(self):
        store=PathStore('C:\\other;;%USERPROFILE%\\tools;')
        before=store.value;self.register(store);registered=store.value
        self.register(store);self.assertEqual(store.value,registered)
        self.assertIn(str(self.home/'bin'),store.value)
        for name in ('qwen.cmd','backtrackify.cmd'):
            text=(self.home/'bin'/name).read_text();self.assertIn(str(self.root/name),text);self.assertNotIn('cd ',text)
        self.register(store,remove=True);self.assertEqual(store.value,before)
        self.register(store,remove=True);self.assertEqual(store.value,before)

    def test_uninstall_keeps_new_unrelated_entries_and_every_user_file(self):
        store=PathStore('C:\\old');self.register(store);store.value+=';C:\\new'
        for path in (self.root/'local.ps1',self.root/'sessions/checkpoint',self.assets/'model',self.root/'logs/result'):
            atomic(path,'preserve')
        self.register(store,remove=True);self.assertEqual(store.value,'C:\\old;C:\\new')
        self.assertTrue(all(p.read_text()=='preserve' for p in (self.root/'local.ps1',self.root/'sessions/checkpoint',self.assets/'model',self.root/'logs/result')))

    def test_existing_path_token_is_not_removed_and_name_conflicts_fail_before_changes(self):
        store=PathStore(str(self.home/'bin'));self.register(store);self.register(store,remove=True)
        self.assertEqual(store.value,str(self.home/'bin'))
        other=self.base/'another install';atomic(other/'qwen.cmd','unrelated')
        with self.assertRaisesRegex(Refused,'conflict'):
            install.registration(self.root,self.home,store,search=str(other))
        store.system=str(other)
        with self.assertRaisesRegex(Refused,'conflict'):
            install.registration(self.root,self.home,store)
        self.assertEqual((other/'qwen.cmd').read_text(),'unrelated')

    def test_modified_shim_foreign_owner_and_partial_unowned_files_are_preserved(self):
        store=PathStore();self.register(store)
        atomic(self.home/'bin/qwen.cmd','user modified')
        with self.assertRaisesRegex(Refused,'modified'):self.register(store,remove=True)
        self.assertEqual((self.home/'bin/qwen.cmd').read_text(),'user modified')
        with self.assertRaisesRegex(Refused,'another installation'):
            install.registration(self.base/'elsewhere',self.home,store,remove=True)
        (self.home/'install.log').unlink()
        with self.assertRaisesRegex(Refused,'not owned'):self.register(store)

    def test_registration_failure_rolls_back_owned_shims(self):
        store=PathStore('C:\\old')
        with patch.object(store,'write',side_effect=OSError('registry denied')),self.assertRaises(OSError):self.register(store)
        self.assertEqual(store.value,'C:\\old');self.assertFalse((self.home/'install.log').exists())
        self.assertFalse((self.home/'bin/qwen.cmd').exists())

    def test_concurrent_unrelated_path_edit_is_preserved_instead_of_overwritten(self):
        store=PathStore('C:\\old');read=store.read;calls=0
        def changed():
            nonlocal calls
            calls+=1
            if calls==2:store.value+=';C:\\other-app'
            return read()
        with patch.object(store,'read',side_effect=changed),self.assertRaisesRegex(Refused,'PATH changed'):self.register(store)
        self.assertEqual(store.value,'C:\\old;C:\\other-app');self.assertFalse((self.home/'install.log').exists())

    def test_vc_runtime_is_fail_fast_and_never_runs_an_installer(self):
        loaded=[]
        install.vc_runtime(lambda path:loaded.append(path.name));self.assertEqual(len(loaded),3)
        with self.assertRaisesRegex(Refused,'official current x64'):
            install.vc_runtime(lambda _:(_ for _ in ()).throw(OSError('DLL missing')))

    def test_space_matches_remaining_pinned_download_and_extraction_bytes(self):
        self.assets.mkdir();fresh=install.remaining_space(self.assets,self.model,'unused',size=1000)
        self.assertEqual(fresh,1000+sum(a+b for a,b in install.ARCHIVES.values()))
        atomic(self.model,'valid');atomic(self.assets/'runtime/qwen/.complete','verified')
        atomic(self.assets/'runtime/downloads/qwen-code-win-x64.zip','verified')
        expected=sum(a+b for name,(a,b) in install.ARCHIVES.items() if name!='qwen')
        self.assertEqual(install.remaining_space(self.assets,self.model,'unused'),expected)
        with self.assertRaisesRegex(Refused,'Insufficient space'):
            install.disk_check(self.assets,self.model,'unused',usage=lambda _:SimpleNamespace(free=expected-1),root=self.root)
        install.disk_check(self.assets,self.model,'unused',usage=lambda _:SimpleNamespace(free=expected+128*1024**2),root=self.root)

    def test_model_head_size_and_separate_source_drive_preflight(self):
        response=SimpleNamespace(headers={'Content-Length':'1000'})
        with patch('install.urllib.request.urlopen') as open:
            open.return_value.__enter__.return_value=response
            needed=install.remaining_space(self.assets,self.model,'https://pinned.invalid/model')
            self.assertEqual(open.call_args.args[0].get_method(),'HEAD')
            self.assertEqual(needed,1000+sum(a+b for a,b in install.ARCHIVES.values()))
            response.headers={}
            with self.assertRaisesRegex(Refused,'Cannot establish'):install.remaining_space(self.assets,self.model,'https://pinned.invalid/model')
        if self.root.anchor!=install.ROOT.anchor:
            def usage(path):return SimpleNamespace(free=128*1024**2-1 if path.anchor==install.ROOT.anchor else needed)
            with self.assertRaisesRegex(Refused,'Insufficient space'):
                install.disk_check(self.assets,self.model,'unused',usage=usage,size=1000)

    def test_atomic_extraction_reuses_verified_files_and_rejects_incomplete_or_unsafe(self):
        archive=self.base/'pinned.zip';destination=self.assets/'runtime/tool'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('bin/app','pinned')
        install.unpack(archive,destination);self.assertEqual((destination/'bin/app').read_text(),'pinned')
        install.unpack(archive,destination);self.assertEqual((destination/'.complete').read_text().strip(),archive.name)
        atomic(destination/'bin/app','changed')
        with self.assertRaisesRegex(Refused,'differs from pinned archive'):install.unpack(archive,destination)
        self.assertEqual((destination/'bin/app').read_text(),'changed')
        partial=self.assets/'runtime/partial';partial.mkdir()
        with self.assertRaisesRegex(Refused,'Incomplete runtime'):install.unpack(archive,partial)
        with zipfile.ZipFile(archive,'w') as z:z.writestr('../escape','bad')
        with self.assertRaisesRegex(Refused,'Unsafe archive'):install.unpack(archive,self.assets/'runtime/unsafe')
        self.assertFalse((self.assets/'runtime/escape').exists());self.assertFalse(list((self.assets/'runtime').glob('*.part-*')))

    def test_readiness_is_not_config_existence_and_changed_assets_need_setup(self):
        atomic(self.root/'local.ps1',"$AssetRoot='shared assets'\n")
        with self.assertRaisesRegex(Refused,'Incomplete installation'):install.readiness(self.root,self.assets,self.model)
        for p in install.required(self.assets,self.model,self.root):atomic(p,'verified')
        with self.assertRaisesRegex(Refused,'readiness'):install.readiness(self.root,self.assets,self.model)
        before=(self.root/'local.ps1').read_bytes();install.readiness(self.root,self.assets,self.model,commit=True)
        install.readiness(self.root,self.assets,self.model);self.assertEqual((self.root/'local.ps1').read_bytes(),before)
        atomic(self.root/'runtime/vision/python/PIL/__pycache__/Image.cpython-314.pyc','generated import cache')
        install.readiness(self.root,self.assets,self.model)
        atomic(self.model,'different')
        with self.assertRaisesRegex(Refused,'configuration changed'):install.readiness(self.root,self.assets,self.model)

    def test_root_uninstall_refuses_active_owned_backend_without_touching_path(self):
        store=PathStore();self.register(store);before=store.value
        with lock(self.root/'runtime/backend.lock'):
            with self.assertRaises(Refused):
                with lock(self.root/'runtime/backend.lock'):self.register(store,remove=True)
        self.assertEqual(store.value,before);self.assertTrue((self.home/'install.log').exists())

    @unittest.skipUnless(os.name=='nt','Windows setup workflow')
    def test_real_first_setup_registration_fresh_shell_partial_state_and_uninstall(self):
        source=install.ROOT
        for name in ('backtrackify.cmd','qwen.cmd','launch-qwen.ps1','uninstall.cmd','harness.py','install.py','vision.py'):
            (self.root/name).write_bytes((source/name).read_bytes())
        # Only this temporary installation uses synthetic hardware/packages and a
        # file-backed registry. Production setup has no test bypass or tuning flags.
        script=(source/'setup.ps1').read_text(encoding='utf-8')
        start=script.index('function Detect {');end=script.index('function Install {',start)
        script=script[:start]+"function Detect { return @{Profile='MID';Gpu=0;Threads=16} }\n"+script[end:]
        start=script.index('function Fetch(');end=script.index('function Unzip(',start)
        script=script[:start]+'''function Fetch($Url,$Destination,$Hash) {
 if (!(Test-Path -LiteralPath $Destination)) {
  if ($Destination.EndsWith('.zip')) { Copy-Item -LiteralPath (Join-Path $Root 'fixture.zip') -Destination $Destination }
  else { [IO.Directory]::CreateDirectory((Split-Path $Destination -Parent)) | Out-Null; [IO.File]::WriteAllText($Destination,'fixture model') }
 }
}
'''+script[end:]
        script=script.replace("$QwenVersion = & (Join-Path $AssetRoot 'runtime\\qwen\\qwen-code\\node\\node.exe') $QwenCli --version","$QwenVersion = '0.22.3'; $LASTEXITCODE = 0")
        (self.root/'setup.ps1').write_text(script,encoding='utf-8')
        with zipfile.ZipFile(self.root/'fixture.zip','w') as z:
            for name in ('qwen-code/node/node.exe','qwen-code/lib/cli.js','llama-server.exe','cudart64_13.dll','cublas64_13.dll','cublasLt64_13.dll'):z.writestr(name,'fixture')
        harness=(self.root/'harness.py').read_text(encoding='utf-8').split("if __name__ == '__main__':",1)[0]
        harness+="\nif __name__ == '__main__':\n    print('CPU setup harness recorder: '+sys.argv[1])\n"
        (self.root/'harness.py').write_text(harness,encoding='utf-8')
        helper=(self.root/'install.py').read_text(encoding='utf-8').split("if __name__=='__main__':",1)[0]
        helper+=r'''
class FixturePath:
 def machine(self):return ''
 def read(self):
  p=ROOT/'user-path.txt'
  return (p.read_text() if p.exists() else str(ROOT.parent/'unrelated tools')),2
 def write(self,value,kind):atomic(ROOT/'user-path.txt',value)
if __name__=='__main__':
 UserPath=FixturePath
 vc_runtime=lambda:None
 disk_check=lambda *a:None
 def install_vision(model):
  from vision import projector
  atomic(projector('MID',ROOT),'fixture projector')
  atomic(ROOT/'runtime/vision/python/.complete','fixture decoder')
 try:main()
 except (OSError,ValueError) as error:print('Backtrackify | INSTALL | BLOCKED | '+str(error));raise SystemExit(2)
'''
        (self.root/'install.py').write_text(helper,encoding='utf-8')
        (self.root/'runtime/downloads').mkdir(parents=True)
        foreign=self.base/'foreign commands'
        for name in ('qwen.cmd','backtrackify.cmd'):atomic(foreign/name,'Unrelated installation; preserve me')
        with patch.dict(os.environ,PATH=str(foreign)+os.pathsep+os.environ['PATH'],
                        BACKTRACKIFY_TEST_ROOT=str(self.base),QWEN_HOME='foreign profile',PYTHONPATH='foreign imports'):
            env=installation_environment(self.base,self.home)
        def call(*args,cwd=None,environment=env):
            return subprocess.run([env['COMSPEC'],'/d','/c',*map(str,args)],cwd=cwd or self.root,env=environment,capture_output=True,text=True,timeout=30)
        result=call('backtrackify.cmd');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('SETUP | PASS',result.stdout);self.assertTrue((self.root/'runtime/installed.log').exists())
        self.assertFalse((self.root/'local.ps1.part').exists())
        local=(self.root/'local.ps1').read_bytes();saved=(self.root/'user-path.txt').read_text()
        conflict=dict(env,PATH=str(foreign)+os.pathsep+env['PATH'])
        result=call('backtrackify.cmd','setup',environment=conflict)
        self.assertEqual(result.returncode,2);self.assertIn('Command-name conflict',result.stdout)
        self.assertEqual((self.root/'user-path.txt').read_text(),saved)
        self.assertTrue(all((foreign/name).read_text()=='Unrelated installation; preserve me' for name in ('qwen.cmd','backtrackify.cmd')))
        result=call('backtrackify.cmd','setup');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'local.ps1').read_bytes(),local);self.assertEqual((self.root/'user-path.txt').read_text(),saved)
        with lock(self.root/'runtime/install.lock'):
            result=call('backtrackify.cmd','setup');self.assertEqual(result.returncode,2);self.assertIn('Another setup owns',result.stdout)
        self.assertEqual((self.root/'local.ps1').read_bytes(),local)
        fresh=dict(env,PATH=saved+';'+env['PATH'])
        for args in [('qwen','--help'),('qwen','chat','--help'),('backtrackify','help')]:
            result=call(*args,cwd=self.base,environment=fresh);self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        (self.root/'runtime/installed.log').unlink()
        result=call('qwen.cmd');self.assertEqual(result.returncode,2);self.assertIn('readiness',result.stdout)
        result=call('backtrackify.cmd','setup');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        with lock(self.root/'runtime/backend.lock'):
            result=call('uninstall.cmd');self.assertEqual(result.returncode,2)
        self.assertEqual((self.root/'user-path.txt').read_text(),saved)
        result=call('uninstall.cmd');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.root/'runtime/installed.log').exists())
        self.assertEqual((self.root/'user-path.txt').read_text(),str(self.base/'unrelated tools'));self.assertEqual((self.root/'local.ps1').read_bytes(),local)
        self.assertTrue((self.root/'models').is_dir())
        isolated=dict(env,BACKTRACKIFY_TEST_ROOT=str(self.base))
        result=call('backtrackify.cmd','setup',environment=isolated)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('no user command registration',result.stdout)
        self.assertEqual((self.root/'user-path.txt').read_text(),str(self.base/'unrelated tools'))
        self.assertFalse((self.home/'Backtrackify/install.log').exists())


if __name__=='__main__':unittest.main()
