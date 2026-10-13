param([string]$PythonPath = '', [switch]$SkipDependencyInstall)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not [Environment]::Is64BitProcess -or $env:OS -ne 'Windows_NT') { throw 'Local setup requires Windows x64.' }
Push-Location -LiteralPath $root
try {
    $env:UV_CACHE_DIR = Join-Path $root '.build\uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.build\python'
    $env:PYTHONUTF8 = '1'
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (-not $PythonPath) {
            $PythonPath = Join-Path $root '.build\python\cpython-3.12.10-windows-x86_64-none\python.exe'
            if (-not (Test-Path -LiteralPath $PythonPath)) {
                if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv or pass -PythonPath with Python 3.12.10 x64.' }
                uv python install 3.12.10 --no-bin --no-registry
                if ($LASTEXITCODE -ne 0) { throw 'Python bootstrap failed.' }
            }
        }
        $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
        & $PythonPath -c "import sys,struct; assert sys.version_info[:3] == (3,12,10) and struct.calcsize('P') == 8"
        if ($LASTEXITCODE -ne 0) { throw 'Expected Python 3.12.10 x64.' }
        & $PythonPath -m venv (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $venvPython -c "import sys,struct; assert sys.version_info[:3] == (3,12,10) and struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv must use Python 3.12.10 x64.' }
    if (-not $SkipDependencyInstall) {
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            uv pip sync (Join-Path $root 'requirements.lock') --require-hashes --python $venvPython
        } else {
            & $venvPython -m pip install --require-hashes -r (Join-Path $root 'requirements.lock')
        }
        if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
    }
    & $venvPython (Join-Path $PSScriptRoot 'prepare_assets.py')
    if ($LASTEXITCODE -ne 0) { throw 'Pinned FFmpeg preparation failed.' }
    & (Join-Path $PSScriptRoot 'install-sendto.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'SendTo shortcut installation failed.' }
    Write-Output 'Local CLI environment ready. Run scripts\transcribe.ps1 --configure.'
} finally { Pop-Location }
