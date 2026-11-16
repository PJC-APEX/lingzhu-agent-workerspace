param([string]$Python = "$PSScriptRoot/../../.venv/Scripts/python.exe")
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path "$PSScriptRoot/../..").Path
Push-Location $projectRoot
try {
    & $Python tools/jianying_import/build_windows.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows importer build failed' }
} finally { Pop-Location }
