@echo off
chcp 65001 >nul
title Trae-Learning
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 未找到本地 Python 环境，请先按 README 完成首次安装。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m app.launch
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上方信息。按任意键关闭此窗口。
    pause >nul
    exit /b 1
)
