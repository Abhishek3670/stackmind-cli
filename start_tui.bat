@echo off
setlocal
chcp 65001 >nul
title StackMind Interactive TUI (v3.2.0 GA)

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment Python not found at: %PYTHON_EXE%
    echo Please ensure the .venv directory exists in the workspace.
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_DIR%tui.py" %*

if errorlevel 1 (
    echo.
    echo [ERROR] TUI exited with error code %errorlevel%.
    pause
)
