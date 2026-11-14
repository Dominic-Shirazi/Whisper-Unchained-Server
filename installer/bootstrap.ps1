# Whisper Unchained Server setup, step 1 of 2 (run by Setup.bat; no admin needed).
#
# Gets everything the app needs onto a fresh Windows PC, then opens the setup
# wizard (installer/wizard.py) for step 2. Safe to run again: it reuses what
# is already there, so it also works as "repair" or "update dependencies".
#
#   1. uv: Astral's Python installer, pinned and checksum-verified.
#   2. Python, installed by uv INSIDE this folder (installer/.tools/python), so
#      it never touches a system Python and the background service (which runs
#      as LocalSystem) can read it.
#   3. The .venv with requirements.txt.
#   4. With an NVIDIA GPU: NVIDIA's cuBLAS libraries from pip, so no separate
#      CUDA install is needed (cuDNN already ships inside ctranslate2).
param(
    [switch]$NoWizard,
    [string]$PythonVersion = "3.13"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the bar

$Root  = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $PSScriptRoot ".tools"
$Venv  = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

# Pinned uv release and the SHA-256 published with it on GitHub. To update:
# pick a release at least two weeks old, copy its .sha256 file's hash here.
$UvVersion = "0.13.5"
$UvSha256  = "a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a"
$Uv = Join-Path $Tools "uv.exe"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

Write-Host "Whisper Unchained Server setup" -ForegroundColor Green
Write-Host "Folder: $Root"
Write-Host "Everything goes into this folder: its own Python and packages. Your system"
Write-Host "Python, PATH and other projects aren't touched."

# --- 1. uv ------------------------------------------------------------------
$haveUv = $false
if (Test-Path $Uv) {
    $haveUv = ((& $Uv --version) -match [regex]::Escape($UvVersion))
}
if (-not $haveUv) {
    Step "Downloading uv $UvVersion (Python installer)"
    New-Item -ItemType Directory -Force -Path $Tools | Out-Null
    $zip = Join-Path $Tools "uv.zip"
    Invoke-WebRequest -UseBasicParsing -OutFile $zip `
        -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
    # .NET directly, not Get-FileHash: that cmdlet goes missing when Windows
    # PowerShell is started from PowerShell 7 (module paths get mixed up).
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($zip)
    try { $hash = -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") }) }
    finally { $stream.Dispose(); $sha.Dispose() }
    if ($hash -ne $UvSha256) {
        Remove-Item $zip -Force
        throw "The uv download doesn't match its published checksum, so it was deleted and not run. Try again later."
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.Name) {   # skip folder entries
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile(
                    $entry, (Join-Path $Tools $entry.Name), $true)
            }
        }
    } finally { $archive.Dispose() }
    Remove-Item $zip -Force
}

# Keep uv's Python AND its download cache inside this folder (see note 2
# above), so nothing lands in the user's profile and uninstalling frees it all.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR = Join-Path $Tools "cache"

# --- 2 + 3. Python and the venv -------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Step "Installing Python $PythonVersion"
    # --no-bin: no python3.x.exe in the user's .local folder; --no-registry: not listed
    # with Windows (PEP 514). The app's Python stays private to this folder.
    & $Uv python install $PythonVersion --no-bin --no-registry
    Check "Installing Python"
    Step "Creating the app environment (.venv)"
    # --managed-python: always the private copy above, never a Python already on the PC.
    & $Uv venv $Venv --python $PythonVersion --managed-python
    Check "Creating .venv"
} else {
    Write-Host "Using the existing .venv."
}

Step "Installing packages (first run downloads a few hundred MB)"
# constraints.txt pins every package to versions proven to work together.
& $Uv pip install --python $VenvPython -r (Join-Path $Root "requirements.txt") -c (Join-Path $Root "constraints.txt")
Check "Installing packages"

# --- 4. NVIDIA GPU libraries ----------------------------------------------------------
# Only when nvidia-smi actually lists a card: a leftover nvidia-smi from an old
# driver (or a disabled/removed card) answers with an error and no name, and
# AMD/Intel-only PCs don't need ~500 MB of CUDA libraries. Without them the
# server runs on the processor (api/device_select.py).
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
$gpu = $null
if ($smi) {
    try {
        $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0) { $gpu = $null }
    } catch { $gpu = $null }
}
if ($gpu -and "$gpu".Trim()) {
    Step "NVIDIA GPU found ($gpu): installing CUDA libraries"
    & $Uv pip install --python $VenvPython "nvidia-cublas-cu12>=12.4,<13"
    Check "Installing CUDA libraries"
} else {
    Write-Host ""
    Write-Host "No NVIDIA GPU found: transcription will run on the CPU (slower, still works)." -ForegroundColor Yellow
}

# --- Windows' Uninstall button ----------------------------------------------------------
# Smart App Control blocks the .exe installer's unins000.exe (unsigned, and made
# on this PC). Point Settings > Apps > Uninstall at the app's own Python instead.
# Every run re-applies it, since installing a new version rewrites the entry.
# Does nothing in a copy the .exe didn't install.
try {
    & $VenvPython (Join-Path $PSScriptRoot "uninstall_app.py") --register
} catch {
    Write-Host "Couldn't update the uninstall entry: $_" -ForegroundColor Yellow
}

# --- Step 2: the wizard ----------------------------------------------------------------
if ($NoWizard) {
    Step "Done (wizard skipped)."
    exit 0
}
Step "Opening the setup wizard"
$Pythonw = Join-Path $Venv "Scripts\pythonw.exe"
Start-Process -FilePath $Pythonw -ArgumentList "`"$(Join-Path $PSScriptRoot 'wizard.py')`"" -WorkingDirectory $Root
exit 0
