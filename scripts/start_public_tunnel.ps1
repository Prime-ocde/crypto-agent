# 临时公网演示：通过 SSH 反向隧道把本机 8501 暴露到公网（无账号、60 分钟有效）
# 前置：先启动前后端（scripts\start_demo.ps1）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_public_tunnel.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$health = 0
try {
    $health = (Invoke-WebRequest -Uri "http://127.0.0.1:8501/healthz" -TimeoutSec 3 -UseBasicParsing).StatusCode
} catch { }

if ($health -ne 200) {
    Write-Host "前端没有在 8501 上运行。" -ForegroundColor Yellow
    Write-Host "请先执行： powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "正在建立公网隧道（Pinggy 免费版，60 分钟后过期）…" -ForegroundColor Cyan
Write-Host "连上后会打印 2 个 https 地址，任选其一分享给同学/老师。" -ForegroundColor Cyan
Write-Host "首次打开需要点一下页面上的『Enter site』（免费隧道的中转提示）。" -ForegroundColor DarkYellow
Write-Host ""

ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null `
    -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes `
    -R0:127.0.0.1:8501 a.pinggy.io
