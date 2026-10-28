# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 ca-works
<#
.SYNOPSIS
Build ENG Drawing Viewer for Windows (x64): the application folder, a portable ZIP and, with
-Installer, a per-user Setup program.

.DESCRIPTION
Requires uv (https://docs.astral.sh/uv/), Go 1.26 or later (https://go.dev/dl/) and, for -Installer,
Inno Setup 6 (https://jrsoftware.org/isinfo.php). Uses a separate build environment (.venv-build)
with CPython 3.13 from uv and installs the exact, hash-checked packages in
packaging/requirements-windows.txt.

Output in release\windows\:
  ENG Drawing Viewer\                                         the application folder
  ENG-Drawing-Viewer-<version>-Windows-x64-Portable.zip
  ENG-Drawing-Viewer-<version>-Windows-x64-Setup.exe          (with -Installer)
  ENG-Drawing-Viewer-<version>-Windows-x64.cdx.json           software bill of materials

.EXAMPLE
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1 -Installer
#>
param(
    [string]$Python = "3.13.16",
    [switch]$Installer,
    [switch]$SkipSmokeTest
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
# Run a program and stop on failure. (A simple function, so arguments such as -o reach the program.)
function Run {
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$Init = Get-Content -Raw -Encoding UTF8 "src\eng_viewer\__init__.py"
$Version = [regex]::Match($Init, '__version__ = "([^"]+)"').Groups[1].Value
if (-not $Version) { throw "Could not read the version from src\eng_viewer\__init__.py" }
$Base = "ENG-Drawing-Viewer-$Version-Windows-x64"
$Out = Join-Path $Root "release\windows"
$Dist = Join-Path $Out "ENG Drawing Viewer"
$Work = Join-Path $Root "build\windows"
$Venv = if ($env:BUILD_VENV) { $env:BUILD_VENV } else { Join-Path $Root ".venv-build" }

# Steadier output: fixed hash seed and timestamps from the source's commit time when known.
$env:PYTHONHASHSEED = "0"
if (-not $env:SOURCE_DATE_EPOCH) {
    $ct = (git log -1 --format=%ct 2>$null)
    $env:SOURCE_DATE_EPOCH = if ($ct) { "$ct" } else { [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
}

foreach ($tool in "uv", "go") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is required (see docs\BUILDING.md)" }
}
# Python records the folder it is installed in; use a fixed, neutral folder so no personal folder
# name ends up in a release.
if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = "C:\eng-drawing-viewer-build\python" }
if (-not (Test-Path "$Venv\Scripts\python.exe")) {
    Step "Creating build environment ($Venv) with Python $Python"
    Run uv venv --quiet --managed-python --python $Python $Venv
}
$Py = "$Venv\Scripts\python.exe"
Step "Installing hash-checked build dependencies"
Run uv pip install --quiet --python $Py --require-hashes -r packaging\requirements-windows.txt
Run uv pip install --quiet --python $Py --no-deps --reinstall-package eng-drawing-viewer .
Run $Py -c "import sys; print('    Python', sys.version.split()[0])"

Step "Fetching OCR language data (hash-checked)"
Run $Py tools\fetch_tessdata.py "$Work\tessdata"

Step "Building the launcher"
$env:CGO_ENABLED = "0"; $env:GOOS = "windows"; $env:GOARCH = "amd64"; $env:GOFLAGS = "-mod=readonly"
New-Item -ItemType Directory -Force $Work | Out-Null
Push-Location packaging\windows\launcher
try {
    Run $Py ..\make_resources.py ..\..\..\src\eng_viewer\resources\ENG_Drawing_Viewer.ico $Version rsrc_windows_amd64.syso
    Run go version
    Run go vet .
    Run go build -trimpath -buildvcs=false -ldflags "-H windowsgui -s -w -buildid= -X main.version=$Version" -o "$Work\launcher.exe" .
} finally {
    Pop-Location
}

Step "Running PyInstaller"
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
$env:ENG_VIEWER_TESSDATA = "$Work\tessdata"
Run $Py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $Dist --workpath "$Work\pyinstaller" packaging\windows\ENG_Drawing_Viewer_win.spec
Remove-Item Env:\ENG_VIEWER_TESSDATA
Copy-Item "$Work\launcher.exe" "$Dist\ENG Drawing Viewer.exe"
Copy-Item packaging\windows\READ_ME.txt "$Dist\Read Me.txt"
Copy-Item LICENSE "$Dist\Licence.txt"

Step "Collecting licences and the software bill of materials"
New-Item -ItemType Directory -Force "$Dist\legal" | Out-Null
Copy-Item LICENSE "$Dist\legal\LICENSE"
Run $Py tools\collect_licenses.py $Dist --out "$Dist\legal\licenses" --sbom "$Out\$Base.cdx.json"

if (-not $SkipSmokeTest) {
    Step "Smoke test: start through the launcher, render a page in a worker process, run three PDF tools"
    $Smoke = Join-Path $Work "smoke"
    New-Item -ItemType Directory -Force $Smoke | Out-Null
    $Sample = Join-Path $Smoke "smoke test.pdf"
    $Log = Join-Path $Smoke "smoke.log"
    if (Test-Path $Log) { Remove-Item $Log }
    Run $Py -c "import sys, pymupdf; d = pymupdf.open(); p = d.new_page(width=842, height=595); p.draw_rect(pymupdf.Rect(40, 40, 802, 555), width=0.5); p.insert_text((60, 80), 'Smoke test', fontsize=18); d.save(sys.argv[1])" $Sample
    $env:ENG_VIEWER_SMOKE_TEST = "1"; $env:ENG_VIEWER_SMOKE_LOG = $Log
    try {
        $p = Start-Process -FilePath "$Dist\ENG Drawing Viewer.exe" -ArgumentList "`"$Sample`"" -PassThru -Wait
    } finally {
        Remove-Item Env:\ENG_VIEWER_SMOKE_TEST, Env:\ENG_VIEWER_SMOKE_LOG
    }
    if (Test-Path $Log) { Get-Content $Log }
    if ($p.ExitCode -ne 0 -or -not (Select-String -Quiet -Path $Log -Pattern "3 PDF tool jobs OK")) {
        throw "Smoke test failed (exit code $($p.ExitCode))"
    }
}

Step "Checking the bundle"
Run $Py tools\check_bundle.py $Dist --json "$Work\check_bundle.json"

Step "Packing the portable ZIP"
Run $Py packaging\windows\make_zip.py $Dist "$Out\$Base-Portable.zip"

if ($Installer) {
    Step "Building the installer"
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) { throw "Inno Setup 6 is required for -Installer (https://jrsoftware.org/isinfo.php)" }
    Run $iscc /Q "/DAppVersion=$Version" "/DSourceDir=$Dist" "/DOutputDir=$Out" "/DOutputName=$Base-Setup" "/DRepoRoot=$Root" packaging\windows\installer.iss
}

Get-ChildItem $Out -File | ForEach-Object { "{0,10:N1} MB  {1}" -f ($_.Length / 1MB), $_.Name }
Step "Done: $Dist"
