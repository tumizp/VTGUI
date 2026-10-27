param(
    [string]$Python = "python",
    [string]$MakeNSIS = "",
    [switch]$SkipAppBuild
)
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    if (-not $SkipAppBuild) {
        & (Join-Path $PSScriptRoot "build.ps1") -Python $Python
    }
    $buildArgs = @((Join-Path $PSScriptRoot "installer\build_installer.py"))
    if ($MakeNSIS) { $buildArgs += @("--compiler", $MakeNSIS) }
    & $Python @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
} finally {
    Pop-Location
}
