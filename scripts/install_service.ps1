#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$ServiceName = "FasterWhisperAPI"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $scriptDir
$PythonPath = Join-Path $BaseDir ".venv\Scripts\python.exe"

Write-Host "--- Faster-Whisper API Service Installer ---"

# 1. Read .env file for configuration
$RestartInterval = 4
$EnvFile = Join-Path $BaseDir ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^RESTART_INTERVAL_HOURS=(.+)$") {
            $RestartInterval = [int]$matches[1]
        }
    }
}
Write-Host "Configured to restart every $RestartInterval hours."

# 2. Locate or Download NSSM
$NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"
$NSSM_ZIP = Join-Path $BaseDir "nssm.zip"
$NSSM_EXE = Join-Path $BaseDir "nssm.exe"

if (-not (Test-Path $NSSM_EXE)) {
    Write-Host "Downloading NSSM..."
    Invoke-WebRequest -Uri $NSSM_URL -OutFile $NSSM_ZIP
    Write-Host "Extracting NSSM..."
    Expand-Archive -Path $NSSM_ZIP -DestinationPath (Join-Path $BaseDir "nssm_temp") -Force
    
    # Move the 64-bit nssm.exe to root
    Move-Item -Path (Join-Path $BaseDir "nssm_temp\nssm-2.24\win64\nssm.exe") -Destination $NSSM_EXE -Force
    
    # Cleanup
    Remove-Item -Path $NSSM_ZIP -Force
    Remove-Item -Path (Join-Path $BaseDir "nssm_temp") -Recurse -Force
}

# 3. Stop and Remove Existing Service (if any)
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Host "Stopping existing $ServiceName service..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & $NSSM_EXE remove $ServiceName confirm
}

# 4. Install Service with NSSM
Write-Host "Installing $ServiceName via NSSM..."
# Port from .env (WHISPER_API_PORT), default 5000. Settings > Save & Restart API
# rewrites the same AppParameters when the port changes there.
$Port = 5000
$EnvFile = Join-Path $BaseDir ".env"
if (Test-Path $EnvFile) {
    $m = Select-String -Path $EnvFile -Pattern '^\s*WHISPER_API_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
}
& $NSSM_EXE install $ServiceName $PythonPath "-m uvicorn api.whisper_api:app --host 0.0.0.0 --port $Port"
& $NSSM_EXE set $ServiceName AppDirectory $BaseDir
& $NSSM_EXE set $ServiceName Description "Faster-Whisper local API"
& $NSSM_EXE set $ServiceName Start SERVICE_AUTO_START

# Start the service
Write-Host "Starting $ServiceName..."
Start-Service -Name $ServiceName

# 5. Create Scheduled Task to Restart Service
$TaskName = "Restart FasterWhisperAPI"

# Remove old task if it exists
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false

# Create new task
Write-Host "Setting up Scheduled Task ($TaskName) to restart service every $RestartInterval hours..."
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command `"Restart-Service -Name $ServiceName -Force`""

# Create an hourly trigger, but set RepetitionDuration to a finite long value (Task Scheduler XML rejects MaxValue)
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours $RestartInterval) -RepetitionDuration (New-TimeSpan -Days 10950)

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Description "Restarts the Faster Whisper API service to clear VRAM." | Out-Null

Write-Host "--- Installation Complete! ---"
Write-Host "Service is running in the background."
Write-Host "Ensure API_VISIBLE=false in your .env file so the LAUNCHER doesn't spawn a second API window."
pause
