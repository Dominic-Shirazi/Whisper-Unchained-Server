# Adds two entries to the right-click menu of audio and video files:
#   "Transcribe to clipboard"                 the text, straight to the clipboard
#   "Transcribe with Whisper Unchained..."    the Transcribe a file window (edit, subtitles)
# Both give a straight transcription: no cleanup, trigger phrases or name corrections.
# Registered for the current user (HKCU), so no admin rights are needed. On
# Windows 11 they're under "Show more options" (or Shift+F10).
#   -NoPause   for the setup wizard
#   -Remove    take the entries away again
param([switch]$NoPause, [switch]$Remove)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $scriptDir
$PythonPath = Join-Path $BaseDir ".venv\Scripts\pythonw.exe"
$QuickScript = Join-Path $BaseDir "client\transcribe_file.py"
$WindowScript = Join-Path $BaseDir "client\file_window.py"
$IconPath = Join-Path $BaseDir "client\assets\app_icon.ico"

# Keep in step with client/file_window.py FILE_TYPES (a test checks).
$Extensions = @(".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm", ".aac", ".wma",
                ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp")
$Verbs = @(
    @{ Name = "WhisperUnchained.Clipboard"; Label = "Transcribe to clipboard"; Script = $QuickScript },
    @{ Name = "WhisperUnchained.Open"; Label = "Transcribe with Whisper Unchained..."; Script = $WindowScript }
)
# Older versions put one entry on every file type.
$OldKey = "Software\Classes\*\shell\TranscribeToClipboard"

function Done($code) { if (-not $NoPause) { pause }; exit $code }

function Remove-All {
    $hkcu = [Microsoft.Win32.Registry]::CurrentUser
    $hkcu.DeleteSubKeyTree($OldKey, $false)
    foreach ($ext in $Extensions) {
        foreach ($v in $Verbs) {
            $hkcu.DeleteSubKeyTree("Software\Classes\SystemFileAssociations\$ext\shell\$($v.Name)", $false)
        }
    }
}

if ($Remove) {
    Remove-All
    Write-Host "Removed the right-click Transcribe entries."
    Done 0
}

Write-Host "--- Adding the right-click Transcribe entries ---"
foreach ($p in @($PythonPath, $QuickScript, $WindowScript)) {
    if (-not (Test-Path $p)) {
        Write-Host "Could not find $p. Run Setup.bat first." -ForegroundColor Red
        Done 1
    }
}

try {
    Remove-All   # a clean slate, so a moved install never leaves stale paths
    # .NET registry classes: PowerShell's registry provider treats '*' as a wildcard.
    $hkcu = [Microsoft.Win32.Registry]::CurrentUser
    foreach ($ext in $Extensions) {
        foreach ($v in $Verbs) {
            $key = "Software\Classes\SystemFileAssociations\$ext\shell\$($v.Name)"
            $k = $hkcu.CreateSubKey($key)
            $k.SetValue("MUIVerb", $v.Label)
            $k.SetValue("Icon", "`"$IconPath`"")
            $k.Close()
            $c = $hkcu.CreateSubKey("$key\command")
            $c.SetValue("", "`"$PythonPath`" `"$($v.Script)`" `"%1`"")   # "" = (default)
            $c.Close()
        }
    }
} catch {
    Write-Host "Failed to write registry keys: $_" -ForegroundColor Red
    Done 1
}

Write-Host "--- Done ---"
Write-Host "Right-click an audio or video file: 'Transcribe to clipboard' or 'Transcribe with Whisper Unchained...'."
Write-Host "On Windows 11 they're under 'Show more options'."
Done 0
