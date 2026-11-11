# Builds the Windows installer: installer\dist\WhisperUnchainedServer-Setup-<version>.exe
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_exe.ps1 -Version 1.0.0
#
# Stages the app's git-tracked files (never .env, word lists, transcripts,
# models or the venv: those are git-ignored), adds NSSM (checksum-verified, so
# installs don't depend on the slow nssm.cc site), then runs Inno Setup's ISCC.
#   -FromHead   package the last commit instead of the working tree (use for releases)
# Needs Inno Setup 6: winget install JRSoftware.InnoSetup
param(
    [string]$Version = "1.0.0",
    [switch]$FromHead
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $PSScriptRoot "build"
$Stage = Join-Path $Build "stage"

# Left out of the package: developer-only folders.
$Exclude = @("tests/", ".github/", ".gitattributes", ".gitignore", "ANDROID_APK_HERE/",
             "readme_images/", "PRE_CLOUD_DEPLOYMENT_NOTES.md", "scripts/compare_",
             "scripts/start_listener.bat")

function Sha256($path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $s = [System.IO.File]::OpenRead($path)
    try { return -join ($sha.ComputeHash($s) | ForEach-Object { $_.ToString("x2") }) }
    finally { $s.Dispose(); $sha.Dispose() }
}

# 1. Inno Setup compiler
$iscc = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup" }

# 2. Stage the files
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force $Stage | Out-Null
Push-Location $Root
try {
    if ($FromHead) {
        $zip = Join-Path $Build "head.zip"
        git archive --format=zip -o $zip HEAD
        if ($LASTEXITCODE -ne 0) { throw "git archive failed" }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $Stage)
        Remove-Item $zip
        foreach ($e in $Exclude) {
            $p = Join-Path $Stage $e.TrimEnd('/')
            if (Test-Path $p) { Remove-Item $p -Recurse -Force }
        }
    } else {
        $dirty = git status --porcelain --untracked-files=no
        if ($dirty) { Write-Host "Note: packaging the working tree, which has uncommitted changes." -ForegroundColor Yellow }
        # Tracked plus new not-yet-committed files; git-ignored files never.
        $files = git ls-files --cached --others --exclude-standard
        if ($LASTEXITCODE -ne 0) { throw "git ls-files failed" }
        foreach ($f in $files) {
            if ($Exclude | Where-Object { $f -eq $_ -or $f.StartsWith($_) }) { continue }
            if (-not (Test-Path -LiteralPath $f)) { continue }   # deleted but not committed
            $dest = Join-Path $Stage $f
            New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
            Copy-Item -LiteralPath $f -Destination $dest
        }
    }
} finally { Pop-Location }

# Guard: none of the user's private files may ever end up in the package.
foreach ($private in @(".env", "api\processing_config.json", "api\transcripts.log")) {
    if (Test-Path (Join-Path $Stage $private)) { throw "Refusing to package private file: $private" }
}

# 3. NSSM (same pinned zip and checksum as scripts\install_service.ps1)
$NssmZip = Join-Path $Build "nssm-2.24.zip"
$NssmSha = "727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743"
if (-not (Test-Path $NssmZip) -or (Sha256 $NssmZip) -ne $NssmSha) {
    Write-Host "Downloading NSSM..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $NssmZip
    if ((Sha256 $NssmZip) -ne $NssmSha) {
        Remove-Item $NssmZip -Force
        throw "The NSSM download doesn't match its known checksum; not using it."
    }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($NssmZip)
try {
    [System.IO.Compression.ZipFileExtensions]::ExtractToFile(
        $archive.GetEntry("nssm-2.24/win64/nssm.exe"), (Join-Path $Stage "nssm.exe"), $true)
} finally { $archive.Dispose() }

# 4. Compile
& $iscc "/DAppVersion=$Version" "/DStageDir=$Stage" (Join-Path $PSScriptRoot "WhisperUnchained.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }
$exe = Join-Path $PSScriptRoot "dist\WhisperUnchainedServer-Setup-$Version.exe"
Write-Host ""
Write-Host "Built: $exe" -ForegroundColor Green
Write-Host ("SHA-256: " + (Sha256 $exe))
