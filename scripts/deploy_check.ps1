# 部署验收脚本：给定后端/前端公网地址，逐项检查
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_check.ps1 `
#     -Backend https://xxx.onrender.com -Frontend https://xxx.streamlit.app
# 只部署了前端（直连模式）时，省略 -Backend 即可：
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_check.ps1 -Frontend https://xxx.streamlit.app
param(
    [string]$Backend = "",
    [Parameter(Mandatory = $true)][string]$Frontend
)

$ErrorActionPreference = "Continue"
$Backend = $Backend.TrimEnd("/")
$Frontend = $Frontend.TrimEnd("/")
$script:failed = 0

function Test-Endpoint {
    param([string]$Name, [string]$Url, [string]$Method = "GET", [string]$Body = "")
    try {
        $params = @{ Uri = $Url; TimeoutSec = 30; UseBasicParsing = $true; Method = $Method }
        if ($Body) {
            $params["Body"] = $Body
            $params["ContentType"] = "application/json"
        }
        $response = Invoke-WebRequest @params
        Write-Host ("  OK   {0,-14} {1} ({2})" -f $Name, $Url, $response.StatusCode) -ForegroundColor Green
        return $response
    } catch {
        Write-Host ("  FAIL {0,-14} {1} -> {2}" -f $Name, $Url, $_.Exception.Message) -ForegroundColor Red
        $script:failed++
        return $null
    }
}

Write-Host ""
if (-not $Backend) {
    Write-Host "=== 1. 后端接口（已跳过：未提供 -Backend，按直连模式验收） ===" -ForegroundColor DarkGray
} else {
    Write-Host "=== 1. 后端接口 ===" -ForegroundColor Cyan
    $health = Test-Endpoint "health" "$Backend/api/health"
    if ($health) {
        try {
            $data = $health.Content | ConvertFrom-Json
            Write-Host ("       mode={0} status={1} ws={2}" -f $data.mode, $data.status, $data.feed.connected) -ForegroundColor DarkGray
        } catch { }
    }
    $config = Test-Endpoint "config" "$Backend/api/config"
    if ($config) {
        try {
            $data = $config.Content | ConvertFrom-Json
            Write-Host ("       strategies={0} modes={1} intervals={2}" -f ($data.strategies.key -join ","), ($data.trading_modes -join ","), ($data.intervals -join ",")) -ForegroundColor DarkGray
        } catch { }
    }
    Test-Endpoint "latest_data" "$Backend/api/latest_data?symbol=BTCUSDT&interval=1m&limit=5" | Out-Null
    Test-Endpoint "sentiment" "$Backend/api/sentiment" | Out-Null
    Test-Endpoint "news" "$Backend/api/news?limit=3" | Out-Null

    $body = '{"strategy":"grid","symbol":"BTCUSDT","interval":"1m","limit":100,"params":{"grid_count":10}}'
    Test-Endpoint "backtest" "$Backend/api/backtest" "POST" $body | Out-Null
}

Write-Host ""
Write-Host "=== 2. 前端页面 ===" -ForegroundColor Cyan
Test-Endpoint "frontend" "$Frontend" | Out-Null
Test-Endpoint "st-health" "$Frontend/healthz" | Out-Null

Write-Host ""
if ($script:failed -eq 0) {
    Write-Host "全部通过：云端部署可用。" -ForegroundColor Green
    Write-Host "打开前端： $Frontend" -ForegroundColor Green
} else {
    Write-Host ("有 {0} 项失败，请按 DEPLOY.md 第 5 节排查。" -f $script:failed) -ForegroundColor Yellow
}
