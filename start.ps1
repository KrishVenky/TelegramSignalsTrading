# start.ps1 — One-command morning startup for trading_intel
# Run from the trading_intel directory:
#   .\start.ps1            → batch fetch + dashboard (default)
#   .\start.ps1 -Live      → also starts realtime listener

param(
    [switch]$Live   # pass -Live to also run the realtime listener after batch
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "ERROR: .venv not found. Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== Trading Intel Startup ===" -ForegroundColor Cyan
Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm') IST" -ForegroundColor DarkGray
Write-Host ""

# Step 1: Batch fetch (runs in foreground — must complete before dashboard)
$mode = if ($Live) { "both" } else { "batch" }
Write-Host ">> Step 1: Running pipeline (mode=$mode)..." -ForegroundColor Yellow
& $python "$root\main.py" --mode $mode
Write-Host ">> Pipeline done." -ForegroundColor Green
Write-Host ""

# Step 2: Start dashboard in background
Write-Host ">> Step 2: Starting dashboard at http://localhost:8000 ..." -ForegroundColor Yellow
$dashJob = Start-Job -ScriptBlock {
    param($py, $r)
    & $py -m uvicorn dashboard.app:app --port 8000
} -ArgumentList $python, $root

Write-Host ">> Dashboard running (Job ID: $($dashJob.Id))" -ForegroundColor Green
Write-Host ""
Write-Host "Open: http://localhost:8000" -ForegroundColor Cyan
Write-Host "  1. Click the Kite chip (top-right) to log in to Zerodha" -ForegroundColor White
Write-Host "  2. Enable the Trading toggle when ready" -ForegroundColor White
Write-Host "  3. Press Ctrl+C here to stop everything" -ForegroundColor DarkGray
Write-Host ""

try {
    # Keep alive + stream dashboard logs
    while ($true) {
        Start-Sleep -Seconds 5
        $logs = Receive-Job -Job $dashJob
        if ($logs) { $logs | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray } }
        if ($dashJob.State -eq "Failed") {
            Write-Host "Dashboard crashed. Check logs." -ForegroundColor Red
            break
        }
    }
} finally {
    Write-Host "`nShutting down..." -ForegroundColor Yellow
    Stop-Job -Job $dashJob -ErrorAction SilentlyContinue
    Remove-Job -Job $dashJob -ErrorAction SilentlyContinue
    Write-Host "Done." -ForegroundColor Green
}
