@echo off
chcp 65001 >nul
title 爬虫平台 - 启动
echo ========================================
echo   游戏平台爬虫系统 - 启动服务
echo ========================================
echo.

cd /d "%~dp0"

echo [1/3] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)
echo [OK] Python 环境正常

echo.
echo [2/3] 检查依赖...
python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [提示] 正在安装依赖...
    pip install fastapi uvicorn requests
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请手动执行: pip install fastapi uvicorn requests
        pause
        exit /b 1
    )
)
echo [OK] 依赖检查通过

echo.
echo [3/3] 启动服务...
echo.
echo ========================================
echo   服务启动中，请稍候...
echo   启动成功后浏览器访问: http://localhost:8000
echo   关闭服务请直接关闭本窗口，或运行 关闭.bat
echo ========================================
echo.

python -m uvicorn app:app --host 0.0.0.0 --port 8000

echo.
echo 服务已停止
pause
