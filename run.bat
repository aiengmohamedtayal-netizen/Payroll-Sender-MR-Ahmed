@echo off
cd /d "%~dp0"
title Payroll WhatsApp Sender v2.5
chcp 65001 >nul

echo Closing any old server on port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

echo ========================================================
echo   Installing requirements (first time only)...
echo ========================================================
python -m pip install -q -r requirements.txt

rem Auto-copy email_config if missing
if not exist "email_config.json" (
    if exist "email_config.example.json" (
        copy /y "email_config.example.json" "email_config.json" >nul
    )
)

echo.
echo ========================================================
echo   Server v2.5 starting...  http://127.0.0.1:8000
echo   Keep this window open while using the website!
echo ========================================================
echo.

start "" "http://127.0.0.1:8000"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
