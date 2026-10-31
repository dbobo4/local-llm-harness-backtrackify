param(
    [Parameter(Position=0)][string]$Action = '',
    [ValidateSet('LOW','MID','HIGH','EXTRA HIGH')][string]$Profile,
    [int]$SetupPort = 8080,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = $PSScriptRoot
$TestInstall = $env:BACKTRACKIFY_TEST_ROOT -and $Root.StartsWith($env:BACKTRACKIFY_TEST_ROOT + '\',[StringComparison]::OrdinalIgnoreCase)
if (!$env:BACKTRACKIFY_VIEW_HOME) { $env:BACKTRACKIFY_VIEW_HOME = Join-Path $Root 'logs' }
# Direct .\setup.ps1 use activates local commands in this PowerShell session only.
$env:PATH = $Root + ';' + $env:PATH
function Say($Component, $Status, $Detail) { Write-Host "Backtrackify | $Component | $Status | $Detail" }
function Invoke-Python {
    & python @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE)" }
}
function Inference-Policy($Selected) {
    $Values = & python -c "import sys; sys.path.insert(0,sys.argv[1]); from harness import PROFILES,SPECULATION; p=sys.argv[2]; print(SPECULATION[p]+'|'+str(PROFILES[p][2]))" $Root $Selected
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the static inference profile.' }
    return $Values -split '\|'
}
function Fetch($Url, $Destination, $Hash) {
    Invoke-Python @((Join-Path $Root 'harness.py'), 'download', $Url, $Destination, $Hash)
}
function Unzip($Archive, $Destination) {
    Invoke-Python @((Join-Path $Root 'install.py'),'unpack',$Archive,$Destination)
}
function Detect {
    if (![Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'Automatic setup supports Windows x64 with an NVIDIA CUDA GPU. Other backends require manual validation.'
    }
    if (!(Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { throw 'NVIDIA GPU/driver not found; no supported automatic profile.' }
    $Rows = @(& nvidia-smi --query-gpu=index,memory.total,memory.free,compute_cap --format=csv,noheader,nounits)
    if ($LASTEXITCODE -ne 0 -or !$Rows.Count) { throw 'Cannot query NVIDIA VRAM.' }
    # Pick one GPU. Never sum VRAM across devices.
    $Gpu = @($Rows | ForEach-Object { $v = $_ -split ','; [pscustomobject]@{Index=[int]$v[0]; Total=[int]$v[1]; Free=[int]$v[2]; Compute=[version]$v[3].Trim()} } | Sort-Object Total -Descending)[0]
    $DriverInfo = (& nvidia-smi) -join "`n"
    if ($DriverInfo -notmatch 'CUDA(?:\s+UMD)?\s+Version:\s*(\d+\.\d+)' -or [version]$Matches[1] -lt [version]'13.3') {
        throw 'The pinned CUDA 13.3 runtime requires a driver advertising CUDA 13.3 or newer. Update the NVIDIA driver explicitly.'
    }
    if ($Gpu.Compute -lt [version]'7.5') { throw 'The pinned CUDA 13.3 runtime requires Turing (compute capability 7.5) or newer.' }
    # Native Windows API avoids a dependency on CIM permissions.
    if (!('BacktrackifyMemory' -as [type])) {
        Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class BacktrackifyMemory {
 [StructLayout(LayoutKind.Sequential)] public class Info {
  public uint Length = (uint)Marshal.SizeOf(typeof(Info)); public uint Load;
  public ulong Total, Free, PageTotal, PageFree, VirtualTotal, VirtualFree, Extended;
 }
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GlobalMemoryStatusEx([In,Out] Info info);
}
'@
    }
    $Ram = New-Object BacktrackifyMemory+Info
    if (![BacktrackifyMemory]::GlobalMemoryStatusEx($Ram)) { throw 'Cannot determine available RAM.' }
    $Detected = if ($Gpu.Total -ge 32000) {'EXTRA HIGH'} elseif ($Gpu.Total -ge 23500) {'HIGH'} elseif ($Gpu.Total -ge 15800) {'MID'} elseif ($Gpu.Total -ge 7800) {'LOW'} else {throw 'At least an 8 GB NVIDIA GPU is required.'}
    $Chosen = if ($Profile) {$Profile} else {$Detected}
    $Rank = @{LOW=1; MID=2; HIGH=3; 'EXTRA HIGH'=4}
    if ($Rank[$Chosen] -gt $Rank[$Detected]) { throw "Requested $Chosen exceeds detected $Detected hardware." }
    $NeedRam = if ($Chosen -eq 'LOW') {12GB} else {24GB}
    if ($Ram.Total -lt $NeedRam -or [Environment]::ProcessorCount -lt 4 -or $Ram.Free -lt 6GB) {
        throw "Insufficient CPU/RAM: need 4 logical CPUs, $([int]($NeedRam/1GB)) GB RAM and 6 GB available RAM."
    }
    Say 'HARDWARE' $Chosen "GPU $($Gpu.Index) | $($Gpu.Total) MiB VRAM | $([int]($Ram.Total/1GB)) GB RAM | static selection, fit unproven"
    return @{Profile=$Chosen; Gpu=$Gpu.Index; Threads=[Math]::Min(16,[Math]::Max(2,[Environment]::ProcessorCount-2))}
}
function Install {
    [IO.Directory]::CreateDirectory((Join-Path $Root 'runtime')) | Out-Null
    try { $InstallLock = [IO.File]::Open((Join-Path $Root 'runtime\install.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another setup owns this installation. Wait for it to finish; no files were replaced.' }
    try {
    $Version = & python -c "import sys,sysconfig; sys.path.insert(0,sys.argv[1]); from vision import PILLOW_HASHES; print(int(sys.implementation.name == 'cpython' and str(sys.version_info.major)+str(sys.version_info.minor) in PILLOW_HASHES and not sysconfig.get_config_var('Py_GIL_DISABLED') and sys.maxsize > 2**32))" $Root
    if ($LASTEXITCODE -ne 0 -or $Version -ne '1') { throw 'Use standard 64-bit CPython 3.12, 3.13 or 3.14 from python.org on PATH. Other interpreters and free-threaded builds have no pinned Pillow wheel.' }
    $Config = Join-Path $Root 'local.ps1'
    if (Test-Path -LiteralPath $Config) { . $Config }
    $Detected = Detect
    $Pins = @{
        LOW=@('unsloth/Qwen3.5-9B-GGUF','3885219b6810b007914f3a7950a8d1b469d598a5','Qwen3.5-9B-Q4_K_M.gguf','03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8',8192,256,128)
        MID=@('unsloth/Qwen3.8-27B-GGUF','4ca720788d1e01f1bff70c033e0d0028fd02e502','Qwen3.8-27B-UD-Q3_K_XL.gguf','8c2a45ff85e7674ca185ec8eb6cdeab0e617ed9d8018caed0b64380eb2a67a5e',32768,512,256)
        HIGH=@('unsloth/Qwen3.8-27B-GGUF','4ca720788d1e01f1bff70c033e0d0028fd02e502','Qwen3.8-27B-UD-Q4_K_XL.gguf','3f227079003add2511437e5b1e94812e363385225bf6a9b47b0054a72bc8b01e',65536,1024,512)
    }
    $Pins['EXTRA HIGH'] = $Pins.HIGH  # Same pinned Q4 model/context; larger agent budget, no new variant.
    $Config = Join-Path $Root 'local.ps1'
    if (!(Test-Path -LiteralPath $Config)) {
        $Pin = $Pins[$Detected.Profile]
        $Policy = Inference-Policy $Detected.Profile
        $Text = @"
# Backtrackify local settings. Editable; subsequent launches preserve this file.
`$Profile = '$($Detected.Profile)'
`$Context = $($Pin[4])
`$Batch = $($Pin[5])
`$Microbatch = $($Pin[6])
`$Threads = $($Detected.Threads)
`$Gpu = $($Detected.Gpu)
`$Port = $SetupPort
`$ChatPort = 8081
`$AssetRoot = `$PSScriptRoot
`$SpecType = '$($Policy[0])'
`$AgentSeconds = $($Policy[1])
"@
        $Stage = $Config + '.part'
        try {
            [IO.File]::WriteAllText($Stage, $Text + "`n", (New-Object Text.UTF8Encoding($false)))
            [IO.File]::Move($Stage, $Config)
        } finally {
            if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage }
        }
    }
    . $Config
    if (!$Pins.ContainsKey($Profile)) { throw 'local.ps1 Profile must be LOW, MID, HIGH or EXTRA HIGH.' }
    $Pin = $Pins[$Profile]
    $Policy = Inference-Policy $Profile
    if (!(Test-Path variable:SpecType)) { $SpecType = $Policy[0] }
    if (!(Test-Path variable:AgentSeconds)) { $AgentSeconds = [int]$Policy[1] }
    $env:CUDA_VISIBLE_DEVICES = "$Gpu"
    $Model = Join-Path $AssetRoot "models\$($Pin[2])"
    $ModelUrl = "https://huggingface.co/$($Pin[0])/resolve/$($Pin[1])/$($Pin[2])"
    if (!$TestInstall) { Invoke-Python @((Join-Path $Root 'install.py'),'check-registration') }
    Invoke-Python @((Join-Path $Root 'install.py'),'preflight',$AssetRoot,$Model,$ModelUrl)
    $Downloads = Join-Path $AssetRoot 'runtime\downloads'
    $QwenZip = Join-Path $Downloads 'qwen-code-win-x64.zip'
    $External = [IO.Path]::GetFullPath($AssetRoot).TrimEnd('\') -ne [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($External) {
        foreach ($Required in @($QwenZip,(Join-Path $Downloads 'llama-b10636-bin-win-cuda-13.3-x64.zip'),(Join-Path $Downloads 'cudart-llama-bin-win-cuda-13.3-x64.zip'),$Model,
            (Join-Path $AssetRoot 'runtime\qwen\.complete'),(Join-Path $AssetRoot 'runtime\llama\.complete'),(Join-Path $AssetRoot 'runtime\cuda\.complete'))) {
            if (!(Test-Path -LiteralPath $Required -PathType Leaf)) { throw "External/shared assets incomplete: $Required. Nothing there was changed; select a complete installation explicitly." }
        }
    }
    Fetch 'https://github.com/QwenLM/qwen-code/releases/download/v0.22.3/qwen-code-win-x64.zip' $QwenZip 'd331418c8779e5e709e0f772e0b69a4233c4d2ee2f43edd4a7466debdfc840ff'
    Unzip $QwenZip (Join-Path $AssetRoot 'runtime\qwen')
    $LlamaZip = Join-Path $Downloads 'llama-b10636-bin-win-cuda-13.3-x64.zip'
    Fetch 'https://github.com/ggml-org/llama.cpp/releases/download/b10636/llama-b10636-bin-win-cuda-13.3-x64.zip' $LlamaZip '8a60cadf69ae2e850fdf06412a8d1205cfa54d75130086c8e14108c75e7625c1'
    Unzip $LlamaZip (Join-Path $AssetRoot 'runtime\llama')
    $CudaZip = Join-Path $Downloads 'cudart-llama-bin-win-cuda-13.3-x64.zip'
    Fetch 'https://github.com/ggml-org/llama.cpp/releases/download/b10636/cudart-llama-bin-win-cuda-13.3-x64.zip' $CudaZip '1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e'
    # CUDA DLLs accompany the server; extraction only touches this pinned runtime directory.
    $CudaDir = Join-Path $AssetRoot 'runtime\cuda'
    Unzip $CudaZip $CudaDir
    $env:PATH = "$CudaDir;" + $env:PATH
    $Model = Join-Path $AssetRoot "models\$($Pin[2])"
    Fetch "https://huggingface.co/$($Pin[0])/resolve/$($Pin[1])/$($Pin[2])" $Model $Pin[3]
    # Vision belongs to this installation, even when language/runtime assets are shared read-only.
    Invoke-Python @((Join-Path $Root 'install.py'),'vision',$Model)
    Invoke-Python @((Join-Path $Root 'harness.py'),'metadata',$Model,'--context',"$Context")
    $QwenCli = Join-Path $AssetRoot 'runtime\qwen\qwen-code\lib\cli.js'
    $QwenVersion = & (Join-Path $AssetRoot 'runtime\qwen\qwen-code\node\node.exe') $QwenCli --version
    if ($LASTEXITCODE -ne 0 -or $QwenVersion.Trim() -ne '0.22.3') { throw 'Pinned Qwen CLI does not report version 0.22.3.' }
    Say 'SETUP' 'INSTALLED' 'Pinned files verified. Loading the model once; no benchmark.'
    # A load-only smoke is safe during ordinary installation. Agent/GPU E2E uses test.ps1 snapshots.
    Invoke-Python @((Join-Path $Root 'harness.py'),'load','--server',(Join-Path $AssetRoot 'runtime\llama\llama-server.exe'),
        '--qwen',$QwenCli,'--model',$Model,'--profile',$Profile,'--context',"$Context",'--batch',"$Batch",'--ubatch',"$Microbatch",'--port',"$Port",'--threads',"$Threads",'--spec-type',$SpecType,'--agent-seconds',"$AgentSeconds")
    if (!$TestInstall) { Invoke-Python @((Join-Path $Root 'install.py'),'register') }
    Invoke-Python @((Join-Path $Root 'install.py'),'ready',$AssetRoot,$Model)
    if ($TestInstall) { Say 'SETUP' 'PASS' 'Isolated test installation loaded; no user command registration was performed.' }
    else {
        Say 'SETUP' 'PASS' "$Profile | $($Pin[2]) | Qwen 0.22.3 / llama b10636 | model load passed | commands registered. Agent acceptance remains separate."
        Say 'CLI' 'READY' 'New terminal: qwen | qwen chat | backtrackify help. Uninstall: .\uninstall.cmd (preserves data).'
    }
    } finally { $InstallLock.Dispose() }
}
try {
    if ($Action -eq 'qwen' -and $CliArgs -and $CliArgs[0] -eq 'chat') {
        $Action = 'chat'
        $CliArgs = @($CliArgs | Select-Object -Skip 1)
    }
    if ($Action -in @('help','--help','-h')) {
        Say 'CLI' 'HELP' 'setup [-Profile LOW/MID/HIGH/"EXTRA HIGH"] | run "goal" [--project PATH] | run --session PATH | qwen | qwen chat | chat | test -Gpu [-LongFlow | -Diagnose -SourceSnapshot PATH] [-AssetsFrom PATH]'
        exit 0
    }
    if ($Action -eq 'test') {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'test.ps1') @CliArgs
        exit $LASTEXITCODE
    }
    if ($Action -notin @('','setup','run','chat','qwen','smoke','longflow','diagnose','uninstall')) { throw "Unknown command '$Action'. Use backtrackify help." }
    if ($Action -eq 'uninstall') { Invoke-Python @((Join-Path $Root 'install.py'),'uninstall'); exit 0 }
    if ($Action -in @('smoke','longflow','diagnose') -and (!$env:BACKTRACKIFY_TEST_ROOT -or !$Root.StartsWith($env:BACKTRACKIFY_TEST_ROOT + '\',[StringComparison]::OrdinalIgnoreCase))) {
        throw 'GPU acceptance must run in a fresh Desktop snapshot. Use test.ps1 -Gpu -Install.'
    }
    if ($Action -eq 'run' -and (!$CliArgs -or $CliArgs.Count -eq 0)) { throw 'Use backtrackify run "goal" or backtrackify run --session PATH.' }
    if ($Action -in @('run','chat','qwen') -and ('--help' -in $CliArgs -or '-h' -in $CliArgs)) {
        & python (Join-Path $Root 'harness.py') $Action --help
        exit $LASTEXITCODE
    }
    if ($Action -eq 'setup' -or !(Test-Path -LiteralPath (Join-Path $Root 'local.ps1')) -or
        ($Action -eq '' -and !(Test-Path -LiteralPath (Join-Path $Root 'runtime\installed.log')))) {
        Install
        if ($Action -in @('','setup')) { exit 0 }
    }
    if ($Action -eq 'setup') { exit 0 }
    . (Join-Path $Root 'local.ps1')
    $Policy = Inference-Policy $Profile
    if (!(Test-Path variable:SpecType)) { $SpecType = $Policy[0] }
    if (!(Test-Path variable:AgentSeconds)) { $AgentSeconds = [int]$Policy[1] }
    $Names = @{LOW='Qwen3.5-9B-Q4_K_M.gguf'; MID='Qwen3.8-27B-UD-Q3_K_XL.gguf'; HIGH='Qwen3.8-27B-UD-Q4_K_XL.gguf'}
    $Names['EXTRA HIGH'] = $Names.HIGH
    if (!$Names.ContainsKey($Profile)) { throw 'Invalid local profile.' }
    if ($Action -in @('','run','qwen','chat')) {
        Invoke-Python @((Join-Path $Root 'install.py'),'check',$AssetRoot,(Join-Path $AssetRoot "models\$($Names[$Profile])"))
    }
    if ($Action -eq '') {
        if (!$TestInstall) {
            Invoke-Python @((Join-Path $Root 'install.py'),'register')
            Say 'CLI' 'READY' 'qwen | qwen chat | backtrackify help | .\uninstall.cmd. Commands require a fresh terminal after setup.'
        } else { Say 'CLI' 'READY' 'Isolated snapshot runtime; user command registration unchanged.' }
        exit 0
    }
    $env:CUDA_VISIBLE_DEVICES = "$Gpu"
    $env:PATH = (Join-Path $AssetRoot 'runtime\cuda') + ';' + $env:PATH
    $Arguments = @((Join-Path $Root 'harness.py'),$Action,'--server',(Join-Path $AssetRoot 'runtime\llama\llama-server.exe'),
        '--qwen',(Join-Path $AssetRoot 'runtime\qwen\qwen-code\lib\cli.js'),'--model',(Join-Path $AssetRoot "models\$($Names[$Profile])"),
        '--profile',$Profile,'--context',"$Context",'--batch',"$Batch",'--ubatch',"$Microbatch",'--threads',"$Threads",'--port',"$Port",'--spec-type',$SpecType,'--agent-seconds',"$AgentSeconds")
    if ($Action -eq 'chat') { $Arguments += @('--chat-port',"$ChatPort") }
    if ($Action -eq 'qwen' -and $CliArgs) { $Arguments += '--' }
    & python @Arguments @CliArgs
    exit $LASTEXITCODE
} catch {
    Say 'SETUP' 'BLOCKED' $_.Exception.Message
    exit 2
}
