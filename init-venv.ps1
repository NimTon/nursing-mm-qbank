<#
初始化本地 Python 虚拟环境（venv）。

用法:
  powershell -ExecutionPolicy Bypass -File .\init-venv.ps1

行为:
  - 若 .\venv 不存在则创建
  - 升级 pip 并以可编辑模式安装本项目（pip install -e .）
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvDir = Join-Path $PSScriptRoot "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

function Assert-Command([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "未找到命令: $Name。请先安装 Python 3.10+ 并加入 PATH。"
    }
    return $cmd.Source
}

Assert-Command "python" | Out-Null

if (-not (Test-Path $venvPython)) {
    Write-Host "创建虚拟环境: $venvDir"
    python -m venv $venvDir
    if (-not (Test-Path $venvPython)) {
        throw "venv 创建失败: $venvPython"
    }
} else {
    Write-Host "虚拟环境已存在: $venvDir"
}

Write-Host "升级 pip ..."
& $venvPython -m pip install -U pip

Write-Host "安装项目依赖 (pip install -e .) ..."
& $venvPython -m pip install -e .

Write-Host ""
Write-Host "完成。激活环境:"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "启动 GUI:"
Write-Host "  .\start-gui.bat"
Write-Host "  或: .\venv\Scripts\python.exe -m mm_qbank.gui_app"
