<# 
Build Windows exe with PyInstaller.

Run:
  powershell -ExecutionPolicy Bypass -File .\build.ps1

Output:
  .\dist\mm-qbank-gui\mm-qbank-gui.exe   (onedir)
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function AssertOk([string]$Step) {
  if ($LASTEXITCODE -ne 0) {
    throw ("Step failed: " + $Step + " (exit_code=" + $LASTEXITCODE + ")")
  }
}

Write-Host "== Build nursing-mm-qbank (PyInstaller) =="

python -m pip install --upgrade pip
AssertOk "pip-upgrade"
python -m pip install -e .
AssertOk "pip-install-editable"
python -m pip install -U pyinstaller
AssertOk "pip-install-pyinstaller"

if (Test-Path ".\build") { Remove-Item -Recurse -Force ".\build" }
if (Test-Path ".\dist\mm-qbank-gui") { Remove-Item -Recurse -Force ".\dist\mm-qbank-gui" }

python -m PyInstaller --noconfirm --clean ".\packaging\mm-qbank-gui.spec"
AssertOk "pyinstaller-build"

Write-Host ""
Write-Host "DONE: dist\\mm-qbank-gui\\mm-qbank-gui.exe"

