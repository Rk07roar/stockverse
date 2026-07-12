@echo off
title StockVest Server
cd /d "%~dp0"

echo ============================================
echo  StockVest - Starting Server
echo ============================================
echo.

REM Try backend venv first, then root venv, then system Python
if exist "backend\.venv\Scripts\python.exe" (
    set PYTHON=backend\.venv\Scripts\python.exe
    echo Using: backend\.venv
) else if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
    echo Using: .venv
) else (
    set PYTHON=python
    echo Using: system Python
)

cd backend
echo.
echo Installing dependencies...
%PYTHON% -m pip install --quiet fastapi "uvicorn[standard]" httpx python-multipart 2>nul
echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
%PYTHON% -m uvicorn main:app --reload --port 8000
pause
