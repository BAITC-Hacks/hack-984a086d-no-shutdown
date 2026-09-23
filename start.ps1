param([switch]$SetupOnly, [int]$Port = 8000, [string]$PythonExecutable)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if (-not $PythonExecutable) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) { $PythonExecutable = $pythonCommand.Source }
        else {
            $bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
            if (Test-Path -LiteralPath $bundledPython) { $PythonExecutable = $bundledPython }
            else { throw 'Install Python 3.12 or pass -PythonExecutable C:/path/to/python.exe' }
        }
    }
    & $PythonExecutable -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
$dependencyStamp = Join-Path $PSScriptRoot '.venv/.windagent-dependencies'
$lockHash = (Get-FileHash -LiteralPath 'requirements.lock.txt' -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $dependencyStamp) { (Get-Content -LiteralPath $dependencyStamp -Raw).Trim() } else { '' }
if ($installedHash -ne $lockHash) {
    & $venvPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network access and retry.' }
    Set-Content -LiteralPath $dependencyStamp -Value $lockHash
}
if ($SetupOnly) { Write-Host "Environment ready: $venvPython"; exit 0 }
$env:WINDAGENT_HOME = $PSScriptRoot
& $venvPython -m windagent serve --port $Port
exit $LASTEXITCODE
