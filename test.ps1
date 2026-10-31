param([Alias('Gpu')][switch]$RunGpu, [switch]$Install, [string]$AssetsFrom, [switch]$LongFlow, [switch]$Diagnose, [string]$SourceSnapshot, [switch]$Visualize)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (!$env:BACKTRACKIFY_VIEW_HOME) { $env:BACKTRACKIFY_VIEW_HOME = Join-Path $PSScriptRoot 'logs' }
$RunLog = $null
function Say($Status,$Detail) {
    $Line = "Backtrackify | SNAPSHOT | $Status | $Detail"
    Write-Host $Line
    if ($script:RunLog) { $Line | Out-File -LiteralPath $script:RunLog -Append -Encoding Unicode }
}
function Invoke-UnitTests($Log, $Source = '') {
    $Assets = ''
    if ($Source) {
        $Config = Join-Path $Source 'local.ps1'
        if (!(Test-Path -LiteralPath $Config -PathType Leaf)) { throw "No installed assets for CPU preflight: $Config. Use -AssetsFrom a configured installation." }
        # Child scope: select immutable assets without changing the GPU/profile variables.
        $Assets = & { $AssetRoot = $null; . $Config; $AssetRoot }
        if ($Assets -isnot [string] -or [string]::IsNullOrWhiteSpace($Assets)) { throw "Missing or ambiguous AssetRoot in CPU preflight configuration: $Config" }
    }
    $Runner = @'
import os,sys,subprocess
from pathlib import Path
env=dict(os.environ)
env.pop('BACKTRACKIFY_NATIVE_CLI',None)
with open(sys.argv[1],'w',encoding='utf-8') as log:
    if len(sys.argv)>2 and sys.argv[2]!='-':
        assets=Path(sys.argv[2]).resolve()
        cli=assets/'runtime/qwen/qwen-code/lib/cli.js'
        node=assets/'runtime/qwen/qwen-code/node/node.exe'
        if not cli.is_file() or not node.is_file():
            detail='CPU preflight selected incomplete native Qwen assets: '+str(assets)
            log.write('BLOCKED | '+detail+'\n');print(detail);raise SystemExit(2)
        env['BACKTRACKIFY_NATIVE_CLI']=str(cli)
    if len(sys.argv)>3:
        decoder=Path(sys.argv[3])/'runtime/vision/python'
        if decoder.is_dir():env['PYTHONPATH']=str(decoder)
    log.write('NATIVE_CLI | '+env.get('BACKTRACKIFY_NATIVE_CLI','snapshot-local assets; native coverage may be unavailable')+'\n');log.flush()
    result=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_*.py','-v'],env=env,stdout=log,stderr=subprocess.STDOUT)
sys.exit(result.returncode)
'@
    $SelectedAssets = if ($Assets) { $Assets } else { '-' }
    & python -c $Runner $Log $SelectedAssets $PSScriptRoot
}
try {
    if ($Diagnose -and (!$RunGpu -or $Install -or $LongFlow -or !$SourceSnapshot)) { throw '-Diagnose requires -Gpu and -SourceSnapshot PATH; omit -Install and -LongFlow.' }
    if ($SourceSnapshot -and !$Diagnose) { throw '-SourceSnapshot requires -Diagnose.' }
    if ($LongFlow -and !$RunGpu) { throw '-LongFlow requires -Gpu.' }
    if ($Visualize -and (!$RunGpu -or !$LongFlow)) { throw '-Visualize requires -Gpu -LongFlow.' }
    if ($LongFlow -and $Install) { throw '-LongFlow reuses installed assets. Omit -Install; use -AssetsFrom PATH if needed.' }
    if ($Install -and $AssetsFrom) { throw 'Use either -Install or -AssetsFrom, not both.' }
    $Desktop = [Environment]::GetFolderPath('Desktop')
    if (!$Desktop) { throw 'Windows Desktop path is unavailable.' }
    $Base = [IO.Path]::GetFullPath((Join-Path $Desktop 'Backtrackify-TEST'))
    $Snapshot = [IO.Path]::GetFullPath((Join-Path $Base ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))))
    if (!$Snapshot.StartsWith($Base + '\',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $Snapshot)) {
        throw 'Unsafe or existing snapshot destination.'
    }
    # The Python copier includes current untracked files, rejects links and omits generated assets.
    $CopyScript = @'
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness import clone
try:
    clone(Path(sys.argv[1]),Path(sys.argv[2]),snapshot=True)
except (OSError, ValueError) as e:
    print('Backtrackify | SNAPSHOT | BLOCKED | '+str(e))
    sys.exit(2)
'@
    & python -c $CopyScript $PSScriptRoot $Snapshot
    if ($LASTEXITCODE -ne 0) { throw "Cannot create fresh snapshot at $Snapshot. Desktop writes may be denied by the current sandbox." }
    if ($RunGpu) {
        $GpuLog = if ($Diagnose) { 'diagnostic-results.log' } elseif ($LongFlow) { 'longflow-results.log' } else { 'gpu-results.log' }
        $RunLog = Join-Path $Snapshot $GpuLog
        '' | Out-File -LiteralPath $RunLog -Encoding Unicode
    }
    Say 'READY' $Snapshot
    Push-Location -LiteralPath $Snapshot
    try {
        $UnitLog = Join-Path $Snapshot 'unit-results.log'
        $CpuSource = if ($AssetsFrom) { [IO.Path]::GetFullPath($AssetsFrom) } elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'local.ps1')) { $PSScriptRoot } else { '' }
        Invoke-UnitTests $UnitLog $CpuSource
        if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed; GPU acceptance not attempted.' }
        Say 'PASS' "Unit tests passed against the fresh development snapshot | $UnitLog"
        if (!$RunGpu) { Say 'NOT RUN' 'GPU E2E. Run test.ps1 -Gpu (or -Gpu -Install for first-run installation).'; exit 0 }
        $Socket = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0)
        $Socket.Start(); $TestPort = $Socket.LocalEndpoint.Port; $Socket.Stop()
        $env:BACKTRACKIFY_TEST_ROOT = $Base
        if ($Install) {
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Snapshot 'setup.ps1') setup -SetupPort $TestPort
            if ($LASTEXITCODE -ne 0) { throw 'First-run setup in snapshot failed.' }
        } else {
            $SourceRoot = if ($AssetsFrom) { [IO.Path]::GetFullPath($AssetsFrom) } else { $PSScriptRoot }
            $SourceConfig = Join-Path $SourceRoot 'local.ps1'
            if (!(Test-Path -LiteralPath $SourceConfig)) { throw 'No installed assets. Use -AssetsFrom PATH or -Install.' }
            # Read editable settings, but write a new isolated config. No source config is changed.
            . $SourceConfig
            $SharedAssets = $AssetRoot
            $DevConfig = Join-Path $PSScriptRoot 'local.ps1'
            if (Test-Path -LiteralPath $DevConfig) { . $DevConfig }
            $AssetRoot = $SharedAssets
        }
        if ($Install) { . (Join-Path $Snapshot 'local.ps1') }
        $Policy = & python -c "from harness import PROFILES,SPECULATION; import sys; p=sys.argv[1]; print(SPECULATION[p]+'|'+str(PROFILES[p][2]))" $Profile
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read the static inference profile.' }
        $Policy = $Policy -split '\|'
        if (!(Test-Path variable:SpecType)) { $SpecType = $Policy[0] }
        if (!(Test-Path variable:AgentSeconds)) { $AgentSeconds = [int]$Policy[1] }
        $EscapedAssets = $AssetRoot.Replace("'","''")
        $Text = "`$Profile = '$Profile'`n`$Context = $Context`n`$Batch = $Batch`n`$Microbatch = $Microbatch`n`$Threads = $Threads`n`$Gpu = $Gpu`n`$Port = $TestPort`n`$ChatPort = 0`n`$AssetRoot = '$EscapedAssets'`n`$SpecType = '$SpecType'`n`$AgentSeconds = $AgentSeconds`n"
        [IO.File]::WriteAllText((Join-Path $Snapshot 'local.ps1'),$Text,(New-Object Text.UTF8Encoding($false)))
        Say 'RUN' "GPU E2E | isolated port $TestPort | shared assets read only | no unrelated process cleanup"
        $ErrorActionPreference = 'Continue'
        $GpuAction = if ($Diagnose) { 'diagnose' } elseif ($LongFlow) { 'longflow' } else { 'smoke' }
        $GpuArgs = @()
        if (!$Install) {
            $Projector = & python -c "from vision import projector; import sys; print(projector(sys.argv[1],sys.argv[2]))" $Profile $PSScriptRoot
            if (!(Test-Path -LiteralPath $Projector -PathType Leaf)) { throw 'Missing pinned vision asset in source installation; run setup there first.' }
            $env:PYTHONPATH = Join-Path $PSScriptRoot 'runtime/vision/python'
            $GpuArgs += @('--mmproj',$Projector)
        }
        if ($Diagnose) { $GpuArgs += @('--source-snapshot',$SourceSnapshot) }
        if ($Visualize) { $GpuArgs += '--visualize' }
        Say 'LAUNCH' "setup.ps1 $GpuAction $($GpuArgs -join ' ') | $RunLog"
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Snapshot 'setup.ps1') $GpuAction @GpuArgs 2>&1 | Tee-Object -FilePath $RunLog -Append
        $GpuExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        Say 'EXIT' "GPU child exit $GpuExit | $RunLog"
        if ($GpuExit -ne 0) {
            $Outcome = if ($GpuExit -eq 1) { 'FAIL' } else { 'BLOCKED' }
            Say $Outcome "GPU acceptance exited $GpuExit; inspect $GpuLog and sessions."
            exit $GpuExit
        }
        if ($Diagnose) { Say 'MEASURED' "Diagnostic replay completed; inspect the structured result | $Snapshot" }
        else { Say 'PASS' "GPU E2E passed | evidence retained at $Snapshot" }
    } finally { Pop-Location }
} catch {
    Say 'BLOCKED' $_.Exception.Message
    exit 2
}
