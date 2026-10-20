# ========================================================================
#  小伴 · Windows 一键启动（PowerShell）
#  用法：右键本文件 → 使用 PowerShell 运行
#  前置条件：Python 3.11+ 已安装并加入 PATH
# ========================================================================

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "========================================================"
Write-Host "  小伴 · 完整功能演示（Windows PowerShell）" -ForegroundColor Cyan
Write-Host "========================================================"

# ── 检查 Python ──────────────────────────────────────────
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[错误] 未找到 Python，请先安装 https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "       安装时记得勾选 'Add Python to PATH'" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
$pyVer = & python --version 2>&1
Write-Host "  检测到 $pyVer"

# ── 虚拟环境 ────────────────────────────────────────────
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host ""
    Write-Host "[首次运行] 创建虚拟环境并安装依赖..." -ForegroundColor Yellow
    & python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
    & .\.venv\Scripts\python.exe -m pip install httpx pytest pytest-asyncio ruff onnxruntime tokenizers -q
    Write-Host "  依赖就绪" -ForegroundColor Green
}

$PyExe = ".\.venv\Scripts\python.exe"

# ── 启动演示 ────────────────────────────────────────────
Write-Host ""
Write-Host "  启动中... （按 Ctrl+C 可中途退出）" -ForegroundColor Yellow
Write-Host "========================================================"
Write-Host ""

& $PyExe -u -m robot.main --sim --fake

Write-Host ""
Write-Host "========================================================"
Write-Host "  演示结束" -ForegroundColor Green
Write-Host "========================================================"
Read-Host "按回车关闭窗口"
