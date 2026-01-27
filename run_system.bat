@echo off
title AI Shop Counter System
cls

echo ======================================================
echo    AI People Counter System (Windows Edition)
echo ======================================================

:: 1. Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
echo [ERROR] Python is not installed or not in PATH.
echo Please install Python 3.10+ from python.org and try again.
pause
exit /b
)

:: 2. Create Virtual Environment (if not exists)
if not exist "venv" (
echo [INFO] Creating virtual environment...
python -m venv venv
)

:: 3. Activate VENV
call venv\Scripts\activate

:: 4. Install Requirements
echo [INFO] Checking dependencies...
pip install -r requirements.txt --quiet

:: 5. Create Data Folder
if not exist "data" mkdir data

:: 6. Run System
echo.
echo [SUCCESS] System is starting...
echo Access Dashboard at: http://localhost:5000
echo.
python main.py

pause