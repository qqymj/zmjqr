@echo off
REM ========================================================================
REM  小伴 · Windows 一键启动（CMD）
REM  用法：
REM    1. 把整个项目文件夹（包含 robot/ config/ pyproject.toml 等）放到任意位置
REM    2. 进入项目目录，双击本文件即可
REM    前置条件：已安装 Python 3.11+（https://www.python.org/downloads/）
REM ========================================================================

setlocal enabledelayedexpansion

echo ========================================================
echo   小伴 · 完整功能演示（Windows）
echo   Python 版本检查中...
echo ========================================================

REM --- 检查 Python ---
where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 未找到 Python，请先安装：https://www.python.org/downloads/
  echo        安装时记得勾选 "Add Python to PATH"
  pause
  exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo   检测到 %PY_VER%

REM --- 虚拟环境 ---
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [首次运行] 创建虚拟环境并安装依赖...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip -q
  python -m pip install httpx pytest pytest-asyncio ruff onnxruntime tokenizers -q
  echo   依赖就绪
) else (
  call .venv\Scripts\activate.bat
)

echo.
echo   依赖检查中...
.venv\Scripts\python.exe -c "import httpx; print('  httpx', httpx.__version__)" 2>nul
if errorlevel 1 (
  echo [警告] httpx 未安装，正在补装...
  python -m pip install httpx -q
)

REM --- 启动机器人（演示模式）---
echo.
echo   准备启动... （按 Ctrl+C 可中途退出）
echo ========================================================
echo.

REM 用管道预先喂入演示输入序列
REM Windows 没有 mkfifo，用 heredoc + 临时文件代替
set DEMO_FILE=%TEMP%\xiaoban_demo_input.txt
if exist "%DEMO_FILE%" del "%DEMO_FILE%"

REM 写演示输入到临时文件（每行一条，行之间用 timeout 模拟间隔）
REM 注意：Windows timeout 是"阻塞指定秒数"，需要配合批处理语法

.venv\Scripts\python.exe -u -m robot.main --sim --fake < nul

REM 上面是直接交互模式：启动后你手动输入
REM 想要自动跑完整演示，用下面这个（需要把所有命令写进 demo_input.txt）
REM type demo_input.txt | .venv\Scripts\python.exe -u -m robot.main --sim --fake

echo.
echo ========================================================
echo   演示结束
echo ========================================================
pause
