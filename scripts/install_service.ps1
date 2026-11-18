#Requires -RunAsAdministrator
# Installs the transcription server as the WhisperUnchained Windows service
# (via NSSM), so it starts with Windows and runs hidden. Safe to re-run: an
# existing service is replaced.
#
#   -NoPause             for the setup wizard: no "press any key" at the end.
#   -NoScheduledRestart  skip (and remove) the every-N-hours restart task. It
#                        briefly takes dictation offline each time and the server
#                        doesn't need it; the wizard installs without it.
#   -AsSystem            run as SYSTEM instead of the limited service account.
param([switch]$NoPause, [switch]$NoScheduledRestart, [switch]$AsSystem)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ServiceName = "WhisperUnchained"
# The service's name before the rename: removed here so the two never both run.
$LegacyName = "FasterWhisperAPI"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $scriptDir
$PythonPath = Join-Path $BaseDir ".venv\Scripts\python.exe"
$EnvFile = Join-Path $BaseDir ".env"

Write-Host "--- Whisper Unchained Server: service installer ---"

# 1. Settings from .env
$RestartInterval = 4
$Port = 5000          # plain HTTP, for programs on this network
$HttpsPort = 5001     # HTTPS, for phones
$Https = $false
$PlainSetting = ""
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^\s*RESTART_INTERVAL_HOURS\s*=\s*(\d+)") { $RestartInterval = [int]$matches[1] }
        # Settings > Save & Restart API rewrites the same AppParameters when the
        # port changes there.
        if ($_ -match "^\s*WHISPER_API_PORT\s*=\s*(\d+)") { $Port = [int]$matches[1] }
        if ($_ -match "^\s*WHISPER_HTTPS_PORT\s*=\s*(\d+)") { $HttpsPort = [int]$matches[1] }
        if ($_ -match "^\s*SERVER_HTTPS\s*=\s*(\S+)") { $Https = @("1","true","yes","on") -contains $matches[1].ToLower() }
        if ($_ -match "^\s*SERVER_PLAIN_HTTP\s*=\s*(\S*)") { $PlainSetting = $matches[1].ToLower() }
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
    # Our own unchanged copy first (nssm.cc is often down); the checksum below
    # is the same whichever one answers.
    $got = $false
    foreach ($u in @("https://github.com/Dominic-Shirazi/Whisper-Unchained-Server/releases/download/tools-nssm-2.24/nssm-2.24.zip", $NSSM_URL)) {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $u -OutFile $NSSM_ZIP; $got = $true; break }
        catch { Write-Host "  $u did not answer ($_)" }
    }
    if (-not $got) { throw "Could not download NSSM from either address." }
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

# 2b. Microsoft's Visual C++ runtime. The speech engine is built against a
# recent one; with an older msvcp140.dll (before 14.40) the server exits the
# moment it loads a model, with no error at all (seen on a Windows 11 laptop,
# 2026-11-18). Installed only when missing or old, and only when the download
# carries Microsoft's own valid signature. Never fails the install.
$VcMin = [version]"14.40.0.0"
$VcDll = Join-Path $env:SystemRoot "System32\msvcp140.dll"
$VcHave = $null
if (Test-Path $VcDll) {
    $fv = (Get-Item $VcDll).VersionInfo
    $VcHave = [version]("{0}.{1}.{2}.{3}" -f $fv.FileMajorPart, $fv.FileMinorPart, $fv.FileBuildPart, $fv.FilePrivatePart)
}
if (-not $VcHave -or $VcHave -lt $VcMin) {
    $VcExe = Join-Path $BaseDir "vc_redist.x64.exe"
    try {
        Write-Host "Installing Microsoft's Visual C++ runtime (found: $VcHave)..."
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $VcExe
        $sig = Get-AuthenticodeSignature $VcExe
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
            throw "the download is not signed by Microsoft, so it was not run"
        }
        $p = Start-Process -FilePath $VcExe -ArgumentList "/install", "/quiet", "/norestart" -Wait -PassThru
        if ($p.ExitCode -eq 3010) {
            Write-Host "RESTART NEEDED: the Visual C++ runtime was installed and Windows must restart before dictation works." -ForegroundColor Yellow
        } elseif ($p.ExitCode -ne 0) {
            throw "its installer ended with code $($p.ExitCode)"
        } else {
            Write-Host "Visual C++ runtime installed."
        }
    } catch {
        Write-Host "Couldn't install the Visual C++ runtime ($_). If dictation doesn't start, install it from https://aka.ms/vs/17/release/vc_redist.x64.exe and restart." -ForegroundColor Yellow
    } finally {
        Remove-Item $VcExe -Force -ErrorAction SilentlyContinue
    }
}

# 3. Stop and remove an existing service (and the old-named one)
foreach ($name in @($LegacyName, $ServiceName)) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "Stopping existing $name service..."
        Stop-Service -Name $name -Force -ErrorAction SilentlyContinue
        & $NSSM_EXE remove $name confirm
    }
}
Get-ScheduledTask -TaskName "Restart $LegacyName" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

# 4. Install and start
# Same rule as api/tls.py: the plain port is on unless switched off, and always
# on when HTTPS is off.
$Plain = ($PlainSetting -eq "") -or (@("1","true","yes","on") -contains $PlainSetting) -or (-not $Https)
$OpenPorts = @()
if ($Plain) { $OpenPorts += $Port }
if ($Https) { $OpenPorts += $HttpsPort }
Write-Host "Installing $ServiceName (ports $($OpenPorts -join ', '))..."
& $NSSM_EXE install $ServiceName $PythonPath "-m api.serve --host 0.0.0.0 --port $Port --https-port $HttpsPort"
if ($LASTEXITCODE -ne 0) { throw "NSSM could not install the service (exit $LASTEXITCODE)." }
& $NSSM_EXE set $ServiceName AppDirectory $BaseDir | Out-Null
& $NSSM_EXE set $ServiceName Description "Whisper Unchained Server (transcription)" | Out-Null
& $NSSM_EXE set $ServiceName Start SERVICE_AUTO_START | Out-Null

# 4a. Run as a limited account, not SYSTEM. "NT SERVICE\WhisperUnchained" is the
# service's own built-in account: no password, and it can only touch what we
# grant here. Read + run the app folder; write only api\ (logs, temporary
# uploads, word lists) and models\ (speech models). If the environment's Python
# lives outside the app folder (a hand-made .venv), it needs read access too.
# -AsSystem keeps the old behavior.
$Account = "NT SERVICE\$ServiceName"
$Limited = $false
if (-not $AsSystem) {
    try {
        $ModelsDir = Join-Path $BaseDir "models"
        New-Item -ItemType Directory -Force $ModelsDir | Out-Null
        & icacls $BaseDir /grant "${Account}:(OI)(CI)RX" /C /Q | Out-Null
        foreach ($d in @((Join-Path $BaseDir "api"), $ModelsDir)) {
            & icacls $d /grant "${Account}:(OI)(CI)M" /C /Q | Out-Null
        }
        $cfg = Join-Path $BaseDir ".venv\pyvenv.cfg"
        if (Test-Path $cfg) {
            $line = Get-Content $cfg | Where-Object { $_ -match "^\s*home\s*=" } | Select-Object -First 1
            if ($line) {
                $pyHome = ($line -split "=", 2)[1].Trim()
                if ($pyHome -and -not $pyHome.StartsWith($BaseDir, [StringComparison]::OrdinalIgnoreCase)) {
                    & icacls $pyHome /grant "${Account}:(OI)(CI)RX" /C /Q | Out-Null
                }
            }
        }
        & sc.exe config $ServiceName obj= $Account | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sc.exe config failed (exit $LASTEXITCODE)" }
        $Limited = $true
        Write-Host "Runs as $Account (limited account)."
    } catch {
        Write-Host "Couldn't set up the limited account ($_); running as SYSTEM." -ForegroundColor Yellow
    }
}

Write-Host "Starting $ServiceName..."
Start-Service -Name $ServiceName

# If the server doesn't answer under the limited account (a folder it can't
# read, say), fall back to SYSTEM rather than leave dictation dead.
if ($Limited) {
    $ok = $false
    $deadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $deadline) {
        try {
            # The port opens only once the model has loaded. A plain TCP check
            # works for HTTP and HTTPS alike (no certificate prompt in PS 5.1).
            $c = New-Object System.Net.Sockets.TcpClient
            try { $c.Connect("127.0.0.1", $OpenPorts[0]); $ok = $true } finally { $c.Close() }
            if ($ok) { break }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if (-not $ok) {
        Write-Host "WARNING: the server didn't answer under $Account; switching to SYSTEM." -ForegroundColor Yellow
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        & sc.exe config $ServiceName obj= LocalSystem | Out-Null
        Start-Service -Name $ServiceName
    }
}

# 4b. Firewall: let the phone and other PCs reach the port, but only from this
# network and from Tailscale (100.64.0.0/10), never the open internet. Without
# it Windows silently blocks them. Never fails the install.
$RuleName = "Whisper Unchained Server"
try {
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort ($OpenPorts | ForEach-Object { "$_" }) -RemoteAddress LocalSubnet, "100.64.0.0/10" -Profile Any | Out-Null
    Write-Host "Firewall: port(s) $($OpenPorts -join ', ') open to this network and Tailscale."
} catch {
    Write-Host "Couldn't add the firewall rule ($_). Other devices may not reach this PC." -ForegroundColor Yellow
}

# 5. Optional scheduled restart
$TaskName = "Restart Whisper Unchained"
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
if (-not $NoScheduledRestart) {
    Write-Host "Setting up Scheduled Task ($TaskName) to restart the service every $RestartInterval hours..."
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command `"Restart-Service -Name $ServiceName -Force`""
    # Task Scheduler XML rejects MaxValue, so repeat for a finite (30-year) duration.
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours $RestartInterval) -RepetitionDuration (New-TimeSpan -Days 10950)
    $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Description "Restarts the Whisper Unchained service to clear VRAM." | Out-Null
}

Write-Host "--- Installation Complete! ---"
Write-Host "Service is running in the background."
if (-not $NoPause) { pause }
exit 0
