<# 
Build Windows exe with PyInstaller.

Run:
  powershell -ExecutionPolicy Bypass -File .\build.ps1

Optional:
  .\build.ps1 -CleanDist   # 打包前删除整个 dist\mm-qbank-gui（全新产物；会丢掉你手放在该目录里的文件）

默认不删 dist\mm-qbank-gui：PyInstaller 会覆盖同名文件，你在该目录里额外放的 configs/.env/data 等会保留。

Output:
  .\dist\mm-qbank-gui\mm-qbank-gui.exe   (onedir)
#>

param(
  [switch]$CleanDist
)

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
if ($CleanDist -and (Test-Path ".\dist\mm-qbank-gui")) {
  Remove-Item -Recurse -Force ".\dist\mm-qbank-gui"
}

python -m PyInstaller --noconfirm --clean ".\packaging\mm-qbank-gui.spec"
AssertOk "pyinstaller-build"

Write-Host ""
Write-Host "DONE: dist\\mm-qbank-gui\\mm-qbank-gui.exe"

