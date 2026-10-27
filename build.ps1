param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    & $Python -m pip install "pyinstaller>=6.16,<7"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed" }
    & $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name VTGUI main.py
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination (Join-Path $PSScriptRoot "dist\VTGUI\README.md")
    Write-Host "Created: $PSScriptRoot\dist\VTGUI\VTGUI.exe"
} finally {
    Pop-Location
}
