# 放行前端/后端端口（需要管理员权限）
# 用法：右键 PowerShell「以管理员身份运行」，然后执行：
#   powershell -ExecutionPolicy Bypass -File scripts\allow_firewall.ps1
#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$rules = @(
    @{ Name = "crypto-grid-backend";  Port = 8000; Note = "FastAPI 后端" },
    @{ Name = "crypto-grid-frontend"; Port = 8501; Note = "Streamlit 前端" }
)

foreach ($rule in $rules) {
    netsh advfirewall firewall delete rule name="$($rule.Name)" 2>$null | Out-Null
    netsh advfirewall firewall add rule `
        name="$($rule.Name)" dir=in action=allow protocol=TCP `
        localport=$($rule.Port) profile=any | Out-Null
    Write-Host "已放行 TCP $($rule.Port)（$($rule.Note)）" -ForegroundColor Green
}

Write-Host ""
Write-Host "现在从其它电脑访问： http://<本机局域网IP>:8501" -ForegroundColor Cyan
Write-Host "查看本机 IP： ipconfig  （或运行 scripts\network_check.ps1）" -ForegroundColor DarkGray
