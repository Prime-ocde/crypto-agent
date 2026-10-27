# Windows 一键启动后端（绑定 0.0.0.0，允许同网段其它电脑访问）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_backend.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Test-Path ".venv\Scripts\Activate.ps1") {
    Write-Host "[start] 激活虚拟环境 .venv" -ForegroundColor Cyan
    & ".venv\Scripts\Activate.ps1"
}

if (-not (Test-Path ".env")) {
    Write-Host "[start] 未找到 .env，自动从 .env.example 复制" -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
}

$lanIp = "127.0.0.1"
try {
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1).IPAddress
} catch { }

Write-Host "[start] 启动 FastAPI（0.0.0.0:8000）" -ForegroundColor Green
Write-Host "[start] 本机文档： http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host "[start] 其它电脑： http://${lanIp}:8000/docs" -ForegroundColor Yellow
Write-Host "[start] 若其它电脑连不上，管理员运行 scripts\allow_firewall.ps1 放行 8000/8501" -ForegroundColor DarkYellow

python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
