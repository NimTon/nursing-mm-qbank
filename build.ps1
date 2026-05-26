<# 
Build Windows exe with PyInstaller.

Run:
  powershell -ExecutionPolicy Bypass -File .\build.ps1

Output:
  .\dist\mm-qbank-gui\mm-qbank-gui.exe              (onedir)
  .\dist\mm-qbank-gui-setup.exe                     (自解压，需安装 7-Zip)
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function AssertOk([string]$Step) {
  if ($LASTEXITCODE -ne 0) {
    throw ("Step failed: " + $Step + " (exit_code=" + $LASTEXITCODE + ")")
  }
}

function Find-7Zip {
  foreach ($candidate in @(
      "${env:ProgramFiles}\7-Zip\7z.exe",
      "${env:ProgramFiles(x86)}\7-Zip\7z.exe"
    )) {
    if (Test-Path $candidate) { return $candidate }
  }
  $cmd = Get-Command 7z -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Build-SfxArchive {
  param(
    [string]$SourceDir,
    [string]$OutputExe
  )
  $sevenZip = Find-7Zip
  if (-not $sevenZip) {
    throw @"
未找到 7-Zip。请先安装：https://www.7-zip.org/
  winget install 7zip.7zip
安装后重新运行: powershell -ExecutionPolicy Bypass -File .\build.ps1
"@
  }

  $sfxModule = Join-Path (Split-Path $sevenZip -Parent) "7z.sfx"
  if (-not (Test-Path $sfxModule)) {
    throw "未找到 7z.sfx 模块: $sfxModule"
  }

  $sfxConfig = Join-Path $PSScriptRoot "packaging\sfx-config.txt"
  if (-not (Test-Path $sfxConfig)) {
    throw "未找到 SFX 配置: $sfxConfig"
  }

  if (Test-Path $OutputExe) { Remove-Item -Force $OutputExe }

  $folderName = Split-Path $SourceDir -Leaf
  $parentDir = Split-Path $SourceDir -Parent
  $archive7z = [System.IO.Path]::ChangeExtension($OutputExe, ".7z")
  if (Test-Path $archive7z) { Remove-Item -Force $archive7z }

  Push-Location $parentDir
  try {
    $cmdArgs = @("a", "-t7z", "-mx=9", $archive7z, $folderName)
    & $sevenZip @cmdArgs
    AssertOk "7z-archive"
  } finally {
    Pop-Location
  }

  # 7-Zip 26+ 单命令 -sfx + -sfxconfig 会报 Multiple instances；用 copy /b 拼接更稳
  $copyCmd = 'copy /b "{0}" + "{1}" + "{2}" "{3}"' -f $sfxModule, $sfxConfig, $archive7z, $OutputExe
  cmd /c $copyCmd | Out-Null
  if (-not (Test-Path $OutputExe)) {
    throw "SFX 拼接失败: $OutputExe"
  }
  Remove-Item -Force $archive7z
  Write-Host "SFX: $OutputExe"
}

Write-Host "== Build nursing-mm-qbank (PyInstaller) =="
Write-Host "Prerequisites (once): pip install -e . ; pip install paddlepaddle paddleclas --no-deps pyinstaller"

if (Test-Path ".\build") { Remove-Item -Recurse -Force ".\build" }
if (Test-Path ".\dist\mm-qbank-gui") { Remove-Item -Recurse -Force ".\dist\mm-qbank-gui" }
if (Test-Path ".\dist\mm-qbank-gui-setup.exe") { Remove-Item -Force ".\dist\mm-qbank-gui-setup.exe" }

python -m PyInstaller --noconfirm --clean ".\packaging\mm-qbank-gui.spec"
AssertOk "pyinstaller-build"

$distRoot = Join-Path $PSScriptRoot "dist\mm-qbank-gui"
Copy-Item -Recurse -Force (Join-Path $PSScriptRoot "configs") (Join-Path $distRoot "configs")
if (Test-Path (Join-Path $PSScriptRoot "models")) {
  Copy-Item -Recurse -Force (Join-Path $PSScriptRoot "models") (Join-Path $distRoot "models")
}
$envSrc = Join-Path $PSScriptRoot ".env"
if (Test-Path $envSrc) {
  Copy-Item -Force $envSrc (Join-Path $distRoot ".env")
} elseif (Test-Path (Join-Path $PSScriptRoot ".env.example")) {
  Copy-Item -Force (Join-Path $PSScriptRoot ".env.example") (Join-Path $distRoot ".env.example")
}
Write-Host "Copied configs/, models/, .env to dist root (beside exe, not _internal)."

$sfxOut = Join-Path $PSScriptRoot "dist\mm-qbank-gui-setup.exe"
Build-SfxArchive -SourceDir $distRoot -OutputExe $sfxOut

Write-Host ""
Write-Host "DONE: dist\\mm-qbank-gui\\mm-qbank-gui.exe"
Write-Host "DONE: dist\\mm-qbank-gui-setup.exe  (自解压，解压后运行 mm-qbank-gui\\mm-qbank-gui.exe)"

