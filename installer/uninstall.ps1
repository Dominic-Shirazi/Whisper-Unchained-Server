# Run by the .exe uninstaller (Inno Setup [UninstallRun]) before it deletes files.
# Takes away what Setup added outside the install folder, but ONLY the pieces
# that point into THIS install: a developer copy elsewhere on the same PC uses
# the same service and shortcut names and must be left alone.
#   1. the WhisperUnchained service (or its old name, FasterWhisperAPI) and its firewall rule (one admin prompt, only if it's ours)
#   2. the tray listener and anything else running from this folder
#   3. the login shortcut
#   4. the right-click Transcribe entries (audio/video files, and the old all-files one)
# Never fails the uninstall: each step reports and moves on.
param([Parameter(Mandatory = $true)][string]$AppDir)

$ErrorActionPreference = "Continue"
$AppDir = [System.IO.Path]::GetFullPath($AppDir).TrimEnd('\')
$ServiceNames = @("WhisperUnchained", "FasterWhisperAPI")

function Is-Ours($path) {
    if (-not $path) { return $false }
    $p = [System.IO.Path]::GetFullPath(([Environment]::ExpandEnvironmentVariables($path.Trim('"'))))
    return $p.StartsWith($AppDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
           $p.Equals($AppDir, [StringComparison]::OrdinalIgnoreCase)
}

# 1. Service. Its folder is readable without admin; removing it needs admin.
foreach ($ServiceName in $ServiceNames) {
try {
    $params = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters" -ErrorAction Stop
    if (Is-Ours $params.AppDirectory) {
        $cmd = "sc.exe stop $ServiceName | Out-Null; Start-Sleep -Seconds 2; sc.exe delete $ServiceName | Out-Null; " +
               "Remove-NetFirewallRule -DisplayName 'Whisper Unchained Server' -ErrorAction SilentlyContinue; " +
               "Unregister-ScheduledTask -TaskName 'Restart Whisper Unchained','Restart FasterWhisperAPI' " +
               "-Confirm:`$false -ErrorAction SilentlyContinue"
        Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden `
            -ArgumentList "-NoProfile", "-Command", $cmd
    }
} catch [System.Management.Automation.ItemNotFoundException] {
    # No service installed.
} catch {
    # Most often: the admin prompt was declined. The service then stays until
    # it's removed by hand (sc.exe delete WhisperUnchained as admin).
}
}

# 2. Processes started from this folder (tray, Settings, wizard). The venv's
# pythonw.exe and the Python it launches both live under the install folder.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { Is-Ours $_.ExecutablePath } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 3. Login shortcut.
foreach ($lnkName in @("Whisper Unchained.lnk", "FasterWhisperListener.lnk")) {
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) $lnkName
if (Test-Path $lnk) {
    try {
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath
        if (Is-Ours $target) { Remove-Item $lnk -Force }
    } catch { }
}
}

# 3b. Older setups (before 0.9.12) let uv register its Python with Windows and
# drop a python3.x.exe into ~\.local\bin. Remove those, only when they point here.
try {
    Get-ChildItem 'HKCU:\Software\Python\Astral' -ErrorAction Stop | ForEach-Object {
        $p = (Get-ItemProperty (Join-Path $_.PSPath 'InstallPath') -ErrorAction SilentlyContinue).'(default)'
        if (Is-Ours $p) { Remove-Item $_.PSPath -Recurse -Force -ErrorAction SilentlyContinue }
    }
    $astral = 'HKCU:\Software\Python\Astral'
    if (-not (Get-ChildItem $astral -ErrorAction SilentlyContinue)) { Remove-Item $astral -ErrorAction SilentlyContinue }
} catch { }
$bin = Join-Path $env:USERPROFILE ".local\bin"
Get-ChildItem $bin -Filter "python3*.exe" -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        # uv's launcher stores the real python.exe path inside the file.
        $text = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($_.FullName))
        if ($text.IndexOf($AppDir, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Remove-Item $_.FullName -Force
        }
    } catch { }
}

# 4. Right-click menu entries.
$keys = @("Software\Classes\*\shell\TranscribeToClipboard")
foreach ($ext in @(".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm", ".aac", ".wma",
                   ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp")) {
    foreach ($verb in @("WhisperUnchained.Clipboard", "WhisperUnchained.Open")) {
        $keys += "Software\Classes\SystemFileAssociations\$ext\shell\$verb"
    }
}
foreach ($key in $keys) {
    try {
        $cmdKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$key\command")
        if ($cmdKey) {
            $command = [string]$cmdKey.GetValue("")
            $cmdKey.Close()
            $exe = if ($command -match '^"([^"]+)"') { $matches[1] } else { ($command -split ' ')[0] }
            if (Is-Ours $exe) { [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($key, $false) }
        }
    } catch { }
}

exit 0
