@echo off
title StockVest - Upgrade & Restart
echo ============================================
echo  Upgrading yfinance and restarting server
echo ============================================

REM Kill existing server
echo Stopping existing server on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

cd /d "%~dp0backend"

if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else if exist "..\venv\Scripts\python.exe" (
    set PYTHON=..\venv\Scripts\python.exe
) else (
    set PYTHON=python
)

echo Upgrading yfinance...
%PYTHON% -m pip install --upgrade yfinance --quiet
echo Done. Starting server...
echo.
%PYTHON% -m uvicorn main:app --reload --port 8000
pause
