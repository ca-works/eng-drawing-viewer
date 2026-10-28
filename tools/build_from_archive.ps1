# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 ca-works
<#
.SYNOPSIS
Build the Windows app from a published source archive, exactly as a release is built.

.DESCRIPTION
  powershell -ExecutionPolicy Bypass -File tools\build_from_archive.ps1 eng-drawing-viewer-<version>-source.zip [work-folder]

Unpacks the archive into a fresh folder (default C:\eng-drawing-viewer-build\src-<n>), checks every
file against SOURCE_MANIFEST.txt, builds with packaging\windows\build_windows.ps1, runs the tests on
the unpacked source and checks the result with tools\check_bundle.py.
#>
param(
    [Parameter(Mandatory)][string]$Archive,
    [string]$Work = "",
    [switch]$Installer
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Archive = (Resolve-Path $Archive).Path
if (-not $Work) {
    $n = 1
    while (Test-Path "C:\eng-drawing-viewer-build\src-$n") { $n++ }
    $Work = "C:\eng-drawing-viewer-build\src-$n"
}
New-Item -ItemType Directory -Force $Work | Out-Null
Expand-Archive -Path $Archive -DestinationPath $Work
$Src = Get-ChildItem $Work -Directory -Filter "eng-drawing-viewer-*" | Select-Object -First 1
if (-not $Src) { throw "The archive has no eng-drawing-viewer-<version> folder" }
Set-Location $Src.FullName
Write-Host "==> Building from $Archive in $PWD"

$bad = 0
foreach ($line in Get-Content -Encoding UTF8 SOURCE_MANIFEST.txt) {
    $hash, $name = $line -split "  ", 2
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $name).Hash.ToLower() -ne $hash) { Write-Host "    changed: $name"; $bad++ }
}
if ($bad) { throw "$bad file(s) do not match SOURCE_MANIFEST.txt" }
Write-Host "    every file matches SOURCE_MANIFEST.txt"

# Archive timestamps are fixed (1980-01-01), so builds don't depend on when they ran.
if (-not $env:SOURCE_DATE_EPOCH) { $env:SOURCE_DATE_EPOCH = "315532800" }
$env:BUILD_VENV = Join-Path $PWD ".venv-build"
& powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1 @(if ($Installer) { "-Installer" })
if ($LASTEXITCODE -ne 0) { throw "build_windows.ps1 failed" }

Write-Host "==> Tests"
$env:PYTHONPATH = Join-Path $PWD "src"
& "$env:BUILD_VENV\Scripts\python.exe" -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw "tests failed" }
Write-Host "==> Built in $PWD\release\windows"
