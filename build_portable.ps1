$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "Checking PyInstaller..."
py -m PyInstaller --version

Write-Host "Building portable application..."
py -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name ITProvisioningCenter `
  app.py

$bundle = Join-Path $projectRoot "dist\ITProvisioningCenter"
New-Item -ItemType Directory -Force -Path (Join-Path $bundle "packages") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $bundle "drivers") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $bundle "logs") | Out-Null

if (Test-Path (Join-Path $projectRoot "packages")) {
    Copy-Item (Join-Path $projectRoot "packages\*") (Join-Path $bundle "packages") -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path (Join-Path $projectRoot "drivers")) {
    Copy-Item (Join-Path $projectRoot "drivers\*") (Join-Path $bundle "drivers") -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path (Join-Path $projectRoot "package_manifest.json")) {
    Copy-Item (Join-Path $projectRoot "package_manifest.json") (Join-Path $bundle "package_manifest.json") -Force
}
Copy-Item (Join-Path $projectRoot "README.md") (Join-Path $bundle "README.md") -Force

Write-Host "Build completed: $bundle"
