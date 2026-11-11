# Run by the .exe uninstaller (Inno Setup [UninstallRun]) before it deletes files.
# Takes away what Setup added outside the install folder, but ONLY the pieces
# that point into THIS install: a developer copy elsewhere on the same PC uses
# the same service and shortcut names and must be left alone.
#   1. the FasterWhisperAPI service and its firewall rule (one admin prompt, only if it's ours)
#   2. the tray listener and anything else running from this folder
#   3. the login shortcut
#   4. the right-click "Transcribe to Clipboard" entry
# Never fails the uninstall: each step reports and moves on.
param([Parameter(Mandatory = $true)][string]$AppDir)

$ErrorActionPreference = "Continue"
$AppDir = [System.IO.Path]::GetFullPath($AppDir).TrimEnd('\')
$ServiceName = "FasterWhisperAPI"

function Is-Ours($path) {
    if (-not $path) { return $false }
    $p = [System.IO.Path]::GetFullPath(([Environment]::ExpandEnvironmentVariables($path.Trim('"'))))
    return $p.StartsWith($AppDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
           $p.Equals($AppDir, [StringComparison]::OrdinalIgnoreCase)
}

# 1. Service. Its folder is readable without admin; removing it needs admin.
try {
    $params = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters" -ErrorAction Stop
    if (Is-Ours $params.AppDirectory) {
        $cmd = "sc.exe stop $ServiceName | Out-Null; Start-Sleep -Seconds 2; sc.exe delete $ServiceName | Out-Null; " +
               "Remove-NetFirewallRule -DisplayName 'Whisper Unchained Server' -ErrorAction SilentlyContinue"
        Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden `
            -ArgumentList "-NoProfile", "-Command", $cmd
    }
} catch [System.Management.Automation.ItemNotFoundException] {
    # No service installed.
} catch {
    # Most often: the admin prompt was declined. The service then stays until
    # it's removed by hand (sc.exe delete FasterWhisperAPI as admin).
}

# 2. Processes started from this folder (tray, Settings, wizard). The venv's
# pythonw.exe and the Python it launches both live under the install folder.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { Is-Ours $_.ExecutablePath } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 3. Login shortcut.
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) "FasterWhisperListener.lnk"
if (Test-Path $lnk) {
    try {
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath
        if (Is-Ours $target) { Remove-Item $lnk -Force }
    } catch { }
}

# 4. Right-click menu entry.
$key = "Software\Classes\*\shell\TranscribeToClipboard"
try {
    $cmdKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$key\command")
    if ($cmdKey) {
        $command = [string]$cmdKey.GetValue("")
        $cmdKey.Close()
        $exe = if ($command -match '^"([^"]+)"') { $matches[1] } else { ($command -split ' ')[0] }
        if (Is-Ours $exe) { [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($key, $false) }
    }
} catch { }

exit 0
