@echo off
chcp 65001 >nul
title Chamdom Trade Bot
cd /d "%~dp0"
if not exist bot_config.env (
  echo [설정 필요] bot_config.env 파일이 없습니다.
  echo bot_config.env.example 이름을 bot_config.env 로 바꾸고 내용을 채운 뒤 다시 실행하세요.
  pause
  exit /b
)
python -m pip install --quiet requests pyjwt
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
