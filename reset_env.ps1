param(
    [string]$Root = ".",
    [int]$BuyClientId = 1019,
    [int]$SellClientId = 1020,
    [int]$SyncClientId = 1021
)

Write-Host "=== Resetting local state files under $Root ==="

$exports = Join-Path $Root "exports"
$inputs  = Join-Path $Root "inputs\ai_strategy_configs"

# Ensure folders exist
foreach ($dir in @($exports,$inputs)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

# Remove any lingering .lock files from atomic saves
Get-ChildItem -Path $exports -Filter *.lock -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

# Reset JSON state
@"
{}
"@ | Set-Content -Encoding UTF8 (Join-Path $exports "live_trades.json")
@"
[]
"@ | Set-Content -Encoding UTF8 (Join-Path $exports "orders_buy.json")
@"
[]
"@ | Set-Content -Encoding UTF8 (Join-Path $exports "orders_sell.json")
@"
{}
"@ | Set-Content -Encoding UTF8 (Join-Path $exports "last_exit_tracker.json")
@"
{}
"@ | Set-Content -Encoding UTF8 (Join-Path $exports "live_prices.json")

Write-Host "Local state cleared."
Write-Host "Next steps:"
Write-Host " 1) (Optional) Cancel working orders in IBKR using cancel_all_ibkr.py"
Write-Host " 2) (Optional) Flatten any test positions using flatten_positions.py"
Write-Host " 3) Start backtest_engine.py"
