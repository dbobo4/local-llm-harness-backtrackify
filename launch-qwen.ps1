# No parameter block: native flags such as -p must not bind to setup's -Profile.
& (Join-Path $PSScriptRoot 'setup.ps1') qwen -CliArgs $args
exit $LASTEXITCODE
