# Adds "Transcribe to Clipboard" to the right-click menu of every file.
# Registered for the current user (HKCU), so no admin rights are needed.
#   -NoPause   for the setup wizard
#   -Remove    take the menu entry away again
param([switch]$NoPause, [switch]$Remove)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $scriptDir
$PythonPath = Join-Path $BaseDir ".venv\Scripts\pythonw.exe"
$ScriptPath = Join-Path $BaseDir "client\transcribe_file.py"
$KeyPath = "Software\Classes\*\shell\TranscribeToClipboard"

function Done($code) { if (-not $NoPause) { pause }; exit $code }

if ($Remove) {
    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($KeyPath, $false)
    Write-Host "Removed the right-click 'Transcribe to Clipboard' entry."
    Done 0
}

Write-Host "--- Installing Right-Click 'Transcribe' Context Menu ---"
foreach ($p in @($PythonPath, $ScriptPath)) {
    if (-not (Test-Path $p)) {
        Write-Host "Could not find $p. Run Setup.bat first." -ForegroundColor Red
        Done 1
    }
}

$Command = "`"$PythonPath`" `"$ScriptPath`" `"%1`""
try {
    # .NET registry classes: PowerShell's registry provider treats the '*' key
    # name as a wildcard.
    $baseKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($KeyPath)
    $baseKey.SetValue("MUIVerb", "Transcribe to Clipboard")
    $baseKey.SetValue("Icon", "shell32.dll,301")
    $baseKey.Close()
    $cmdKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("$KeyPath\command")
    $cmdKey.SetValue("", $Command)   # "" = the key's (default) value
    $cmdKey.Close()
} catch {
    Write-Host "Failed to write registry keys: $_" -ForegroundColor Red
    Done 1
}

Write-Host "--- Installation Complete! ---"
Write-Host "Right-click any audio file and choose 'Transcribe to Clipboard'."
Done 0
