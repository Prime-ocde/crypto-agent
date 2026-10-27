# Windows 一键启动前端（支持同网段其它电脑访问）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_frontend.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Test-Path ".venv\Scripts\Activate.ps1") {
    Write-Host "[start] 激活虚拟环境 .venv" -ForegroundColor Cyan
    & ".venv\Scripts\Activate.ps1"
}

$lanIp = "127.0.0.1"
try {
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1).IPAddress
} catch { }

# 默认后端地址用 LAN IP：本机与其它电脑都能访问
$env:BACKEND_BASE_URL = "http://${lanIp}:8000"
Write-Host "[start] 默认后端：$env:BACKEND_BASE_URL" -ForegroundColor Cyan
Write-Host "[start] 本机打开： http://localhost:8501" -ForegroundColor Green
Write-Host "[start] 其它电脑： http://${lanIp}:8501" -ForegroundColor Yellow
Write-Host "[start] 打不开先跑： powershell -ExecutionPolicy Bypass -File scripts\network_check.ps1" -ForegroundColor DarkYellow

python -m streamlit run frontend/app.py --server.address 0.0.0.0 --server.port 8501
