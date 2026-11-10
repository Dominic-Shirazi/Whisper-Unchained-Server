@echo off
cd /d "%~dp0.."
set "BASE_DIR=%cd%"
set PYTHON=%BASE_DIR%\.venv\Scripts\python.exe

:: Read .env file (ignoring comments and empty lines)
for /f "usebackq tokens=1,2 delims==" %%A in ("%BASE_DIR%\.env") do (
    set "key=%%A"
    set "val=%%B"
    cmd /c exit /b 0
    echo %%A | findstr /b /c:"#" >nul || set "%%A=%%B"
)

:: Ensure defaults if not set in .env
if "%API_VISIBLE%"=="" set API_VISIBLE=false
if "%LISTEN_DELAY_SECONDS%"=="" set LISTEN_DELAY_SECONDS=10

echo Starting API...
if /I "%API_VISIBLE%"=="true" (
    start "Faster Whisper API" "%PYTHON%" "%BASE_DIR%\api\whisper_api.py"
) else (
    echo Background service requested. Ensuring FasterWhisperAPI Windows Service is running...
    net start FasterWhisperAPI 2>nul
    if errorlevel 1 (
        echo [WARNING] Service failed to start or isn't installed. Fallback to normal boot is NOT enabled.
        echo If you haven't installed the service, run install_service.ps1 as Administrator.
    ) else (
        echo Service is started.
    )
)

echo Waiting %LISTEN_DELAY_SECONDS% seconds for model to load...
timeout /t %LISTEN_DELAY_SECONDS% /nobreak

echo Starting Background Watchdog (Invisible)...
start "Background Watchdog" "%BASE_DIR%\.venv\Scripts\pythonw.exe" "%BASE_DIR%\client\listener_watchdog.py"

echo Done. You should have heard two beeps if the listener started.
pause
