@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python virtual environment...
  python -m venv .venv || (echo Python 3.11+ is required. & pause & exit /b 1)
)

echo [2/3] Installing packages...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)

if not defined ADMIN_KEY if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do if /i "%%A"=="ADMIN_KEY" set "ADMIN_KEY=%%B"
)
if not defined ADMIN_KEY set /p ADMIN_KEY=Admin key: 

echo [3/3] Starting server at http://127.0.0.1:8000  (close this window to stop)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
