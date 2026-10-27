# 教室/本机一键启动（支持同网段其它电脑访问）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Test-Path ".venv\Scripts\Activate.ps1") {
    & ".venv\Scripts\Activate.ps1"
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

function Get-LanIp {
    try {
        $ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } |
            Select-Object -First 1).IPv4Address.IPAddress
        if ($ip) { return $ip }
    } catch { }
    try {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
            Select-Object -First 1).IPAddress
        if ($ip) { return $ip }
    } catch { }
    return "127.0.0.1"
}

$lanIp = Get-LanIp

Write-Host ""
Write-Host "本机局域网 IP： $lanIp" -ForegroundColor Cyan
Write-Host "同一 Wi-Fi 的其它电脑/手机请访问： http://${lanIp}:8501" -ForegroundColor Yellow
Write-Host "打不开时先跑： powershell -ExecutionPolicy Bypass -File scripts\network_check.ps1" -ForegroundColor DarkYellow
Write-Host ""

# 关键 1：后端必须绑 0.0.0.0，否则别的电脑只能打开页面、拿不到行情
Write-Host "[demo] 启动后端（0.0.0.0:8000，新窗口）…" -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"

Write-Host "[demo] 等待后端就绪（按 LAN IP 探活）…" -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://${lanIp}:8000/api/health" -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        Start-Sleep -Milliseconds 750
    }
}

if ($ready) {
    Write-Host "[demo] 后端 OK（已绑定 0.0.0.0，其它电脑可访问）" -ForegroundColor Green
} else {
    Write-Host "[demo] 后端未就绪：前端仍会以离线 Mock 模式启动（断网也别慌）" -ForegroundColor Yellow
}

# 关键 2：前端默认后端地址指向 LAN IP，而不是 127.0.0.1
$env:BACKEND_BASE_URL = "http://${lanIp}:8000"
Write-Host "[demo] 启动前端（0.0.0.0:8501），默认后端 = $env:BACKEND_BASE_URL" -ForegroundColor Green
python -m streamlit run frontend/app.py --server.address 0.0.0.0 --server.port 8501
