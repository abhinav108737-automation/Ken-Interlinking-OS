param(
    [ValidateRange(1, 31)]
    [int]$Days = 7,
    [string]$DatabaseUrl = "postgresql+asyncpg://ken:ken@127.0.0.1:5433/ken_full"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment is missing."
}

$previousDatabaseUrl = $env:KEN_DATABASE_URL
$previousGoogleEnabled = $env:KEN_ANALYTICS_GOOGLE_ENABLED
try {
    $env:KEN_DATABASE_URL = $DatabaseUrl
    $env:KEN_ANALYTICS_GOOGLE_ENABLED = "true"
    Push-Location $projectRoot
    try {
        & $python -m ken_services.analytics.cli sync --days $Days --queries
        if ($LASTEXITCODE -ne 0) {
            throw "Analytics sync failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $previousDatabaseUrl) {
        Remove-Item Env:KEN_DATABASE_URL -ErrorAction SilentlyContinue
    }
    else {
        $env:KEN_DATABASE_URL = $previousDatabaseUrl
    }
    if ($null -eq $previousGoogleEnabled) {
        Remove-Item Env:KEN_ANALYTICS_GOOGLE_ENABLED -ErrorAction SilentlyContinue
    }
    else {
        $env:KEN_ANALYTICS_GOOGLE_ENABLED = $previousGoogleEnabled
    }
}
