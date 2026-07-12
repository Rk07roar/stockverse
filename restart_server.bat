@echo off
title StockVest Restart
echo Stopping any existing server on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo Starting StockVest...
cd /d "%~dp0backend"

if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else if exist "..\venv\Scripts\python.exe" (
    set PYTHON=..\venv\Scripts\python.exe
) else (
    set PYTHON=python
)

echo Server starting at http://localhost:8000
%PYTHON% -m uvicorn main:app --reload --port 8000
pause
