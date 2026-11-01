@echo off
chcp 65001 >nul
title Chamdom Binance (paper)
cd /d "%~dp0"
:loop
python binance_bot.py
if %errorlevel%==3 goto loop
echo [watchdog] binance engine stopped. restarting in 15 seconds.
timeout /t 15
goto loop
