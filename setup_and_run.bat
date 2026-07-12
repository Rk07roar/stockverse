@echo off
title StockVest Setup & Run
cd /d "%~dp0"

echo ============================================
echo  StockVest - Full Setup and Start
echo ============================================
echo.

REM Kill any existing backend on port 8000
echo Stopping any existing backend...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING" 2^>nul') do taskkill /f /pid %%a 2>nul
timeout /t 2 /nobreak >nul

cd backend

echo Installing all dependencies from requirements.txt...
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo pip install failed, trying with --break-system-packages...
    python -m pip install -r requirements.txt --quiet --break-system-packages
)

echo.
echo ============================================
echo  All packages installed. Starting server...
echo  Open browser at: http://localhost:8000
echo ============================================
echo.

python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
pause
