@echo off
title BSE Price Filler v2 - Fast parallel fetcher
cd /d "%~dp0"
echo.
echo =============================================
echo  BSE Price Filler v2 - Fast parallel fetcher
echo  Uses direct Yahoo Finance + BSE API calls
echo =============================================
echo.
python fill_bse_v2.py
echo.
echo Done! Press any key to close.
pause
