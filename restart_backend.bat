@echo off
echo Stopping existing backend...
taskkill /f /im python.exe /fi "WINDOWTITLE eq uvicorn*" 2>nul
taskkill /f /im python3.exe 2>nul
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /f /pid %%a 2>nul
timeout /t 2 /nobreak >nul

echo Starting StockVest backend...
cd /d "%~dp0backend"
start "StockVest Backend" cmd /k "python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
echo Backend restarting... wait 10-15 seconds for prices to load.
