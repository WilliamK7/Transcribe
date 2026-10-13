$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$previousUtf8 = $env:PYTHONUTF8
Push-Location -LiteralPath $root
try {
    $env:PYTHONUTF8 = '1'
    & (Join-Path $root '.venv\Scripts\python.exe') -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
} finally { $env:PYTHONUTF8 = $previousUtf8; Pop-Location }
