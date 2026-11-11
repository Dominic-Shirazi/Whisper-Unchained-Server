#Requires -RunAsAdministrator
# Installs the transcription server as the FasterWhisperAPI Windows service
# (via NSSM), so it starts with Windows and runs hidden. Safe to re-run: an
# existing service is replaced.
#
#   -NoPause             for the setup wizard: no "press any key" at the end.
#   -NoScheduledRestart  skip (and remove) the every-N-hours restart task. It
#                        briefly takes dictation offline each time and the server
#                        doesn't need it; the wizard installs without it.
param([switch]$NoPause, [switch]$NoScheduledRestart)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ServiceName = "FasterWhisperAPI"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $scriptDir
$PythonPath = Join-Path $BaseDir ".venv\Scripts\python.exe"
$EnvFile = Join-Path $BaseDir ".env"

Write-Host "--- Faster-Whisper API Service Installer ---"

# 1. Settings from .env
$RestartInterval = 4
$Port = 5000
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^\s*RESTART_INTERVAL_HOURS\s*=\s*(\d+)") { $RestartInterval = [int]$matches[1] }
        # Settings > Save & Restart API rewrites the same AppParameters when the
        # port changes there.
        if ($_ -match "^\s*WHISPER_API_PORT\s*=\s*(\d+)") { $Port = [int]$matches[1] }
    }
}

# 2. NSSM (the tool that runs a program as a service)
$NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"
$NSSM_ZIP = Join-Path $BaseDir "nssm.zip"
$NSSM_EXE = Join-Path $BaseDir "nssm.exe"
# SHA-256 of the official nssm-2.24.zip, so a tampered download is never run.
$NSSM_SHA256 = "727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743"

if (-not (Test-Path $NSSM_EXE)) {
    Write-Host "Downloading NSSM..."
    Invoke-WebRequest -UseBasicParsing -Uri $NSSM_URL -OutFile $NSSM_ZIP
    # .NET instead of Get-FileHash / Expand-Archive: those cmdlets can be
    # missing when Windows PowerShell is started from PowerShell 7.
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($NSSM_ZIP)
    try { $hash = -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") }) }
    finally { $stream.Dispose(); $sha.Dispose() }
    if ($hash -ne $NSSM_SHA256) {
        Remove-Item $NSSM_ZIP -Force
        throw "The NSSM download doesn't match its known checksum, so it was deleted and not used."
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($NSSM_ZIP)
    try {
        $entry = $archive.GetEntry("nssm-2.24/win64/nssm.exe")
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $NSSM_EXE, $true)
    } finally { $archive.Dispose() }
    Remove-Item -Path $NSSM_ZIP -Force
}

# 3. Stop and remove an existing service
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Host "Stopping existing $ServiceName service..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & $NSSM_EXE remove $ServiceName confirm
}

# 4. Install and start
Write-Host "Installing $ServiceName (port $Port)..."
& $NSSM_EXE install $ServiceName $PythonPath "-m uvicorn api.whisper_api:app --host 0.0.0.0 --port $Port"
if ($LASTEXITCODE -ne 0) { throw "NSSM could not install the service (exit $LASTEXITCODE)." }
& $NSSM_EXE set $ServiceName AppDirectory $BaseDir | Out-Null
& $NSSM_EXE set $ServiceName Description "Whisper Unchained Server (transcription)" | Out-Null
& $NSSM_EXE set $ServiceName Start SERVICE_AUTO_START | Out-Null

Write-Host "Starting $ServiceName..."
Start-Service -Name $ServiceName

# 5. Optional scheduled restart
$TaskName = "Restart FasterWhisperAPI"
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
if (-not $NoScheduledRestart) {
    Write-Host "Setting up Scheduled Task ($TaskName) to restart the service every $RestartInterval hours..."
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command `"Restart-Service -Name $ServiceName -Force`""
    # Task Scheduler XML rejects MaxValue, so repeat for a finite (30-year) duration.
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours $RestartInterval) -RepetitionDuration (New-TimeSpan -Days 10950)
    $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Description "Restarts the Faster Whisper API service to clear VRAM." | Out-Null
}

Write-Host "--- Installation Complete! ---"
Write-Host "Service is running in the background."
if (-not $NoPause) { pause }
exit 0
