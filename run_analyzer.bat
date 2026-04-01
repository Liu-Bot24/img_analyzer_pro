@echo off
chcp 65001 >nul
title Image Analyzer Pro - Self-healing Runner

echo ==================================================
echo    图片识别分析器专业版 (Image Analyzer Pro)
echo ==================================================
echo.

rem 1. 检查 Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [状态] 没在系统找到 Python，正在尝试自动安装...
    winget install -e --id Python.Python.3 --accept-source-agreements --accept-package-agreements
    if %errorlevel% neq 0 (
        echo [错误] 自动安装 Python 失败，请手动安装！
        pause
        exit
    )
    echo [提示] Python 安装成功，请重新运行此脚本。
    pause
    exit
)

rem 2. 切换目录并同步依赖
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

echo [状态] 正在同步 Python 依赖环境...
python -m pip install --upgrade pip --quiet --user
python -m pip install -r requirements.txt --quiet --user

echo [状态] 环境已就绪，正在启动服务...
echo --------------------------------------------------
python main.py

echo.
echo --------------------------------------------------
echo 任务运行已结束。
pause
