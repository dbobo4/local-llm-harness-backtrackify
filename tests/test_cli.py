import os
from pathlib import Path
import subprocess
import tempfile
import hashlib
import zipfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name=='nt','Windows launcher checks')
class LauncherTests(unittest.TestCase):
    def test_first_run_persists_each_static_inference_policy_without_model_start(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='profile policy ') as tmp:
            script=Path(tmp)/'policy.ps1'
            script.write_text('''param($Root,$Destination)
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'setup.ps1'),[ref]$null,[ref]$null)
$definition=$tree.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Inference-Policy'},$true)
. ([scriptblock]::Create($definition.Extent.Text))
$create=$tree.Find({param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.StartsWith('if (!(Test-Path -LiteralPath $Config))')},$true)
if (!$create) { throw 'Initial config block missing' }
$Pins=@{}; foreach($Selected in @('LOW','MID','HIGH','EXTRA HIGH')) { $Pins[$Selected]=@('repo','revision','model','hash',32768,512,256) }
$SetupPort=8080
foreach($Selected in @('LOW','MID','HIGH','EXTRA HIGH')) {
 $Detected=@{Profile=$Selected; Threads=16; Gpu=0}; $Config=Join-Path $Destination ($Selected+'.ps1')
 . ([scriptblock]::Create($create.Extent.Text)); . $Config
 Write-Output ($Profile+'|'+$SpecType+'|'+$AgentSeconds)
}
''',encoding='utf-8')
            result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),str(ROOT),tmp],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout.splitlines(),['LOW|none|3600','MID|draft-mtp,ngram-mod|1800',
                'HIGH|draft-mtp,ngram-mod|1800','EXTRA HIGH|draft-mtp,ngram-mod|2700'])

    def test_four_static_profiles_and_qwen_wrappers_on_scoped_path(self):
        for profile in ('LOW','MID','HIGH','EXTRA HIGH'):
            result=self.call('help','-Profile',profile)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        env=dict(os.environ,PATH=str(ROOT)+os.pathsep+os.environ['PATH'])
        for command in ('qwen --help','qwen chat --help','qwen -p "hello world" --help'):
            result=subprocess.run(['cmd.exe','/d','/c',command],cwd=ROOT,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('usage:',result.stdout)
            self.assertNotIn('DOWNLOAD',result.stdout)
        result=subprocess.run(['where.exe','qwen'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(Path(result.stdout.splitlines()[0]),ROOT/'qwen.cmd')
        script="& (Join-Path $env:BACKTRACKIFY_LAUNCHER_TEST_ROOT 'setup.ps1') help; (Get-Command qwen).Source"
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',script],
                              env=dict(os.environ,BACKTRACKIFY_LAUNCHER_TEST_ROOT=str(ROOT)),capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(str(ROOT/'qwen.cmd'),result.stdout)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='qwen launcher ') as directory:
            base=Path(directory)
            (base/'launch-qwen.ps1').write_bytes((ROOT/'launch-qwen.ps1').read_bytes())
            (base/'setup.ps1').write_text("param($Action,[string]$Profile,[string[]]$CliArgs)\nWrite-Output ($Action+'|'+$CliArgs.Count+'|'+($CliArgs -join ';'))",encoding='utf-8')
            for args,expected in [((), 'qwen|0|'),(('-p','two words'), 'qwen|2|-p;two words'),(('chat',),'qwen|1|chat')]:
                result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(base/'launch-qwen.ps1'),*args],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn(expected,result.stdout)

    def call(self,*args):
        return subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'setup.ps1'),*args],
            capture_output=True,text=True,timeout=15)

    def test_help_before_install_and_unknown_command(self):
        result=self.call('help')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('Backtrackify | CLI | HELP',result.stdout)
        result=self.call('not-a-command')
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertIn('Unknown command',result.stdout)

    def test_empty_run_and_run_help_do_not_install(self):
        result=self.call('run')
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertIn('run "goal"',result.stdout)
        self.assertNotIn('DOWNLOAD',result.stdout)
        result=self.call('run','--help')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('usage:',result.stdout)
        self.assertNotIn('DOWNLOAD',result.stdout)

        result=self.call('run','--visualize','--help')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--visualize',result.stdout);self.assertNotIn('DOWNLOAD',result.stdout)

    def test_gpu_switch_survives_loading_integer_gpu_config(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='gpu switch ') as temp:
            base=Path(temp)
            bootstrap=base/'bootstrap.ps1'
            bootstrap.write_text('''param($Root,$Probe)
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'test.ps1'),[ref]$null,[ref]$null)
$body=$tree.ParamBlock.Extent.Text + @'

$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'local.ps1')
if (!$RunGpu.IsPresent -or !$Install.IsPresent -or $Gpu -isnot [int] -or $Gpu -ne 0) { throw 'GPU switch or device index was overwritten' }
Write-Output ('PASS | PowerShell ' + $PSVersionTable.PSVersion + ' | RunGpu=' + $RunGpu + ' | device=' + $Gpu)
'@
[IO.File]::WriteAllText($Probe,$body)
''',encoding='utf-8')
            (base/'local.ps1').write_text("$Gpu = 0\n",encoding='utf-8')
            probe=base/'probe.ps1'
            result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(bootstrap),str(ROOT),str(probe)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(probe),'-Gpu','-Install'],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('PASS | PowerShell 5.1.',result.stdout)
            self.assertIn('device=0',result.stdout)

    def test_existing_assets_config_loads_without_install_or_modification(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='reuse assets ') as temp:
            base=Path(temp)
            assets=base/'installed snapshot'; assets.mkdir()
            config=assets/'local.ps1'
            original="$Gpu = 0\n$AssetRoot = $PSScriptRoot\n"
            config.write_text(original,encoding='utf-8')
            bootstrap=base/'bootstrap.ps1'
            bootstrap.write_text('''param($Root,$Probe)
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'test.ps1'),[ref]$null,[ref]$null)
$assignments=$tree.FindAll({param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -in @('$SourceRoot','$SourceConfig')},$true)
$body=$tree.ParamBlock.Extent.Text + "`n" + (($assignments | ForEach-Object { $_.Extent.Text }) -join "`n") + @'

$ErrorActionPreference='Stop'
. $SourceConfig
if ($Install.IsPresent -or !$RunGpu.IsPresent -or $Gpu -ne 0 -or $AssetRoot -ne $AssetsFrom) { throw 'Existing assets were not reused independently of the GPU switch' }
Write-Output 'PASS | Reused existing configuration without installation'
'@
[IO.File]::WriteAllText($Probe,$body)
''',encoding='utf-8')
            probe=base/'probe.ps1'
            result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(bootstrap),str(ROOT),str(probe)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(probe),'-Gpu','-AssetsFrom',str(assets)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('PASS | Reused existing configuration',result.stdout)
            self.assertEqual(config.read_text(encoding='utf-8'),original)

    def test_full_longflow_launch_chain_preserves_array_arguments_and_early_diagnostics(self):
        # Real CMD -> setup.ps1 -> test.ps1 -> setup.ps1, ending at a CPU-only Python recorder.
        # Replace Desktop with this disposable workspace; never reach model/server startup.
        for scenario,visualize,expected in [('success',True,0),('success',False,0),('dev-policy',True,0),
                                            ('silent',True,2),('stderr',True,2),('missing-config',True,2)]:
            with self.subTest(scenario=scenario,visualize=visualize),tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='launch chain ') as temp:
                base=Path(temp)
                for name in ('backtrackify.cmd','setup.ps1'):(base/name).write_bytes((ROOT/name).read_bytes())
                script=(ROOT/'test.ps1').read_text(encoding='utf-8')
                script=script.replace("$Desktop = [Environment]::GetFolderPath('Desktop')",'$Desktop = $PSScriptRoot')
                (base/'test.ps1').write_text(script,encoding='utf-8')
                (base/'tests').mkdir()
                (base/'tests/test_tiny.py').write_text('import unittest\nclass Tiny(unittest.TestCase):\n def test_cpu(self): self.assertEqual(2+2,4)\n',encoding='utf-8')
                harness=(ROOT/'harness.py').read_text(encoding='utf-8')
                entry="\nif __name__ == '__main__':"
                self.assertIn(entry,harness)  # Fail closed if the real entry point changes.
                harness=harness.split(entry,1)[0]
                harness+='''
if __name__ == '__main__':
    import sys
    (Path(__file__).parent/'received-args.log').write_text('\\n'.join(sys.argv[1:]),encoding='utf-8')
    (Path(__file__).parent/'view-home.log').write_text(os.environ.get('BACKTRACKIFY_VIEW_HOME',''),encoding='utf-8')
'''
                if scenario=='silent':harness+='    raise SystemExit(2)\n'
                elif scenario=='stderr':harness+="    print('CPU startup failure before model load',file=sys.stderr); raise SystemExit(2)\n"
                else:harness+="    print('CPU launcher reached Python; no model was started')\n"
                (base/'harness.py').write_text(harness,encoding='utf-8')
                (base/'vision.py').write_bytes((ROOT/'vision.py').read_bytes())
                from vision import projector
                pinned=projector('MID',base);pinned.parent.mkdir(parents=True);pinned.write_text('CPU projector fixture')
                assets=base/'shared assets';assets.mkdir()
                for relative in ('runtime/qwen/qwen-code/lib/cli.js','runtime/qwen/qwen-code/node/node.exe'):
                    asset=assets/relative;asset.parent.mkdir(parents=True,exist_ok=True);asset.write_text('CPU launch-chain fixture',encoding='utf-8')
                config="$Profile='MID'\n$Context=32768\n$Batch=512\n$Microbatch=256\n$Threads=8\n$Gpu=0\n$AssetRoot='"+str(assets).replace("'","''")+"'\n"
                (base/'local.ps1').write_text(config,encoding='utf-8')
                source=base/'missing assets' if scenario=='missing-config' else base
                if scenario=='dev-policy':
                    (assets/'local.ps1').write_text(config+"$SpecType='none'\n$AgentSeconds=9999\n",encoding='utf-8')
                    config+="$SpecType='draft-mtp,ngram-mod'\n$AgentSeconds=2222\n"
                    (base/'local.ps1').write_text(config,encoding='utf-8');source=assets
                args=['cmd.exe','/d','/c','backtrackify.cmd','test','-Gpu','-LongFlow','-AssetsFrom',str(source)]
                if visualize:args+=['-Visualize']
                env=dict(os.environ);env.pop('BACKTRACKIFY_VIEW_HOME',None)
                result=subprocess.run(args,cwd=base,env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
                snapshot,=list((base/'Backtrackify-TEST').iterdir())
                log=(snapshot/'longflow-results.log').read_text(encoding='utf-16')
                self.assertIn('SNAPSHOT | READY',log)
                self.assertFalse((snapshot/'logs/server.log').exists())
                if scenario=='missing-config':
                    self.assertIn('No installed assets',log);self.assertIn('No installed assets',result.stdout)
                    self.assertFalse((snapshot/'received-args.log').exists())
                else:
                    received=(snapshot/'received-args.log').read_text(encoding='utf-8').splitlines()
                    self.assertEqual(Path((snapshot/'view-home.log').read_text(encoding='utf-8')),base/'logs')
                    self.assertEqual(received[0],'longflow')
                    self.assertEqual(received[received.index('--mmproj')+1],str(pinned))
                    self.assertEqual(received.count('--visualize'),int(visualize))
                    self.assertEqual(received[received.index('--spec-type')+1],'draft-mtp,ngram-mod')
                    self.assertEqual(received[received.index('--agent-seconds')+1],'2222' if scenario=='dev-policy' else '1800')
                    saved=(snapshot/'local.ps1').read_text(encoding='utf-8')
                    self.assertIn("$AssetRoot = '"+str(assets),saved)
                    if scenario=='dev-policy':self.assertIn('$AgentSeconds = 2222',saved)
                    self.assertNotIn('-',received)
                    self.assertIn('SNAPSHOT | LAUNCH',log)
                    self.assertIn(f'GPU child exit {expected}',log);self.assertIn(f'GPU child exit {expected}',result.stdout)
                    if scenario=='stderr':self.assertIn('CPU startup failure before model load',log)
                    if scenario=='silent':self.assertNotIn('CPU launcher reached Python',log)
                self.assertEqual((base/'local.ps1').read_text(encoding='utf-8'),config)

    def test_cpu_snapshot_preflight_uses_selected_assets_before_tests_and_refuses_missing_runtime(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix="CPU preflight's ") as temp:
            base=Path(temp);source=base/'source';source.mkdir();(source/'tests').mkdir()
            assets=base/'shared assets';config_root=base/'configured installation';config_root.mkdir()
            cli=assets/'runtime/qwen/qwen-code/lib/cli.js';node=assets/'runtime/qwen/qwen-code/node/node.exe'
            for path in (cli,node):path.parent.mkdir(parents=True,exist_ok=True);path.write_text('selected CPU fixture',encoding='utf-8')
            config=config_root/'local.ps1';original="$AssetRoot='"+str(assets).replace("'","''")+"'\n$Gpu=0\n"
            config.write_text(original,encoding='utf-8')
            (assets/'local.ps1').write_text('$AssetRoot=$PSScriptRoot\n$Gpu=0\n',encoding='utf-8')
            script=(ROOT/'test.ps1').read_text(encoding='utf-8')
            script=script.replace("$Desktop = [Environment]::GetFolderPath('Desktop')","$Desktop = '"+str(base/'desktop').replace("'","''")+"'")
            (source/'test.ps1').write_text(script,encoding='utf-8')
            # Keep the real copier; the command is CPU-only and cannot reach GPU startup.
            (source/'harness.py').write_bytes((ROOT/'harness.py').read_bytes())
            probe='''import os,unittest
from pathlib import Path
class Probe(unittest.TestCase):
 def test_selected_native_assets(self):
  Path('cpu-test-ran.flag').write_text('ran')
  self.assertEqual(os.environ.get('BACKTRACKIFY_NATIVE_CLI'),EXPECTED)
  self.assertEqual(Path(os.environ['BACKTRACKIFY_NATIVE_CLI']).read_text(),'selected CPU fixture')
'''.replace('EXPECTED',repr(str(cli)))
            (source/'tests/test_probe.py').write_text(probe,encoding='utf-8')
            env=dict(os.environ,BACKTRACKIFY_NATIVE_CLI=str(base/'unrelated ambient CLI.js'))
            args=['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(source/'test.ps1'),'-AssetsFrom',str(config_root)]
            previous=set()
            for missing,selected in ((None,config_root),(None,assets),(cli,config_root),(node,config_root)):
                if missing:missing.unlink()
                args[-1]=str(selected)
                result=subprocess.run(args,cwd=source,env=env,capture_output=True,text=True,timeout=30)
                current=set((base/'desktop/Backtrackify-TEST').iterdir());snapshot,=current-previous;previous=current
                log=(snapshot/'unit-results.log').read_text(encoding='utf-8')
                self.assertEqual(result.returncode,2 if missing else 0,result.stdout+result.stderr)
                self.assertFalse((snapshot/'local.ps1').exists());self.assertFalse((snapshot/'logs/server.log').exists())
                if missing:
                    self.assertIn('incomplete native Qwen assets',log);self.assertFalse((snapshot/'cpu-test-ran.flag').exists())
                    missing.write_text('selected CPU fixture',encoding='utf-8')
                else:
                    self.assertIn('Ran 1 test',log);self.assertIn('NATIVE_CLI | '+str(cli),log)
                    self.assertTrue((snapshot/'cpu-test-ran.flag').exists());self.assertIn('GPU E2E',result.stdout)
                self.assertEqual(config.read_text(encoding='utf-8'),original)
                self.assertEqual(env['BACKTRACKIFY_NATIVE_CLI'],str(base/'unrelated ambient CLI.js'))
            for invalid in ("$AssetRoot=''\n","$Gpu=0\n"):
                config.write_text(invalid,encoding='utf-8');args[-1]=str(config_root)
                result=subprocess.run(args,cwd=source,env=env,capture_output=True,text=True,timeout=30)
                current=set((base/'desktop/Backtrackify-TEST').iterdir());snapshot,=current-previous;previous=current
                self.assertEqual(result.returncode,2,result.stdout+result.stderr);self.assertIn('AssetRoot',result.stdout)
                self.assertFalse((snapshot/'cpu-test-ran.flag').exists())
                self.assertEqual(config.read_text(encoding='utf-8'),invalid)

    def test_installer_helpers_verify_download_and_extract_with_spaces(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='installer test ') as temp:
            base=Path(temp)
            (base/'tests').mkdir()
            (base/'tests/test_tiny.py').write_text('import unittest\nclass Tiny(unittest.TestCase):\n def test_real(self): self.assertEqual(2+3,5)\n',encoding='utf-8')
            source=base/'pinned.zip'
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('bundle/content.txt','pinned content\n')
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            script=base/'check.ps1'
            script.write_text('''param($Root,$Source,$Target,$Hash)
$ErrorActionPreference='Stop'
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'setup.ps1'),[ref]$null,[ref]$null)
$functions=$tree.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Invoke-Python','Fetch','Unzip')},$true)
foreach($definition in $functions) { . ([scriptblock]::Create($definition.Extent.Text)) }
Fetch $Source $Target $Hash
Fetch $Source $Target $Hash
Unzip $Target (Join-Path (Split-Path $Target -Parent) 'extracted')
if(!(Test-Path -LiteralPath (Join-Path (Split-Path $Target -Parent) 'extracted/bundle/content.txt'))) { throw 'Extraction missing' }
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'test.ps1'),[ref]$null,[ref]$null)
$definition=$tree.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-UnitTests'},$true)
. ([scriptblock]::Create($definition.Extent.Text))
Push-Location -LiteralPath (Split-Path $Target -Parent)
try { Invoke-UnitTests (Join-Path (Split-Path $Target -Parent) 'units.log'); if($LASTEXITCODE -ne 0) { throw 'Unit helper failed' } } finally { Pop-Location }
''',encoding='utf-8')
            result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),str(ROOT),source.as_uri(),str(base/'cached.zip'),digest],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('SHA-256 verified',result.stdout)
            self.assertTrue((base/'extracted/.complete').is_file())
            self.assertIn('Ran 1 test',(base/'units.log').read_text(encoding='utf-8'))
