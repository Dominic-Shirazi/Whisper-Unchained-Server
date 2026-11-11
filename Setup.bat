@echo off
:: Open Whisper Flow setup. Double-click to install (or repair) everything.
:: No admin rights needed here; the wizard asks once, when it installs the
:: background service.
cd /d "%~dp0"
:: Started from PowerShell 7, Windows PowerShell inherits 7's module path and
:: loses built-in cmdlets. Clearing it makes 5.1 rebuild its own default.
set "PSModulePath="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\bootstrap.ps1" %*
if errorlevel 1 (
    echo.
    echo Setup stopped because of the error above. Nothing was half-installed:
    echo fix the problem ^(often the internet connection^) and run Setup.bat again.
    pause
    exit /b 1
)
