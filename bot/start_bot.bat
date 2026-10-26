@echo off
chcp 65001 >nul
title Chamdom Trade Bot
cd /d "%~dp0"
:loop
python chamdom_bot.py
if %errorlevel%==3 (
  echo [updater] new version installed - restarting...
  goto loop
)
echo.
echo [watchdog] bot stopped. restarting in 15 seconds. close this window to stop completely.
timeout /t 15
goto loop
