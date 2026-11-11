# Starts the tray listener (hotkey + tray icon) when you log into Windows, via
# a shortcut in your Startup folder. No admin rights needed.
#   -NoPause   for the setup wizard
#   -Remove    take the shortcut away again
param([switch]$NoPause, [switch]$Remove)

$BaseDir = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $BaseDir ".venv\Scripts\pythonw.exe"
$Watchdog = Join-Path $BaseDir "client\listener_watchdog.py"
$StartupFolder = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupFolder "FasterWhisperListener.lnk"

function Done($code) { if (-not $NoPause) { Pause }; exit $code }

if ($Remove) {
    Remove-Item $ShortcutPath -ErrorAction SilentlyContinue
    Write-Host "Removed the login shortcut."
    Done 0
}

if (-Not (Test-Path $Pythonw)) {
    Write-Host "Error: Could not find pythonw.exe at $Pythonw" -ForegroundColor Red
    Write-Host "Run Setup.bat first."
    Done 1
}

Try {
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Pythonw
    $Shortcut.Arguments = "`"$Watchdog`""
    $Shortcut.WorkingDirectory = $BaseDir
    $Icon = Join-Path $BaseDir "client\assets\app_icon.ico"
    $Shortcut.IconLocation = if (Test-Path $Icon) { "$Icon, 0" } else { "$Pythonw, 0" }
    $Shortcut.Save()
    Write-Host "--- Success! ---" -ForegroundColor Green
    Write-Host "The tray listener will now start when you log into Windows:"
    Write-Host $ShortcutPath
} Catch {
    Write-Host "Error creating shortcut: $_" -ForegroundColor Red
    Done 1
}
Done 0
