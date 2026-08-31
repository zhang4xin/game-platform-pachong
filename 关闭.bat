@echo off
chcp 65001 >nul
title 爬虫平台 - 关闭
echo ========================================
echo   游戏平台爬虫系统 - 关闭服务
echo ========================================
echo.

echo 正在查找占用 8000 端口的进程...
echo.

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo 找到进程 PID: %%a
    echo 正在终止进程...
    taskkill /F /PID %%a >nul 2>&1
    if errorlevel 1 (
        echo [警告] 终止进程 %%a 失败，可能需要管理员权限
    ) else (
        echo [OK] 进程 %%a 已终止
    )
)

echo.
echo 检查是否还有残留进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo [警告] 仍有进程 %%a 在运行
    goto :still_running
)

echo.
echo ========================================
echo   服务已成功关闭！
echo ========================================
goto :end

:still_running
echo.
echo ========================================
echo   部分进程未能关闭，请手动处理：
echo   1. 以管理员身份运行本脚本
echo   2. 或手动执行: taskkill /F /PID 进程ID
echo ========================================

:end
echo.
pause
