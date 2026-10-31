@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Build PayrollSender.exe

echo ========================================================
echo   Building PayrollSender.exe  (one time, needs Python + internet)
echo ========================================================
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

python -m PyInstaller --noconfirm --clean --onefile --name PayrollSender ^
  --add-data "templates;templates" --add-data "static;static" ^
  --collect-all selenium --collect-submodules uvicorn --collect-submodules anyio ^
  --collect-submodules dns --hidden-import openpyxl --hidden-import xlrd ^
  --hidden-import multipart --hidden-import python_multipart --hidden-import email_service ^
  launcher.py
if errorlevel 1 goto fail

echo.
echo ========================================================
echo   DONE!  The program is here:  dist\PayrollSender.exe
echo   Copy that single file to any Windows PC and double-click it.
echo   (The PC needs Chrome or Edge + internet. No Python needed.)
echo ========================================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed. Send me the error text above.
pause
exit /b 1
