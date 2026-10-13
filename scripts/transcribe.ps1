$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run scripts\setup.ps1 first.' }
$previousUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONUTF8 = '1'
    # Keep the caller's working directory so relative media/report paths retain their meaning.
    & $venvPython (Join-Path $root 'launcher.py') @args
    $cliExitCode = $LASTEXITCODE
} finally { $env:PYTHONUTF8 = $previousUtf8 }
exit $cliExitCode
