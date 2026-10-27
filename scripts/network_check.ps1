# 网络自检：排查"换电脑/手机打不开前端"的常见原因
# 用法：powershell -ExecutionPolicy Bypass -File scripts\network_check.ps1
$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "=== 1. 本机 IPv4（其它电脑要用这一行的地址） ===" -ForegroundColor Cyan
try {
    Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Format-Table IPAddress, InterfaceAlias -AutoSize
} catch {
    ipconfig | Select-String "IPv4"
}

$lanIp = "127.0.0.1"
try {
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1).IPAddress
} catch { }

Write-Host "=== 2. 端口监听（必须是 0.0.0.0，127.0.0.1 表示只有本机能连） ===" -ForegroundColor Cyan
foreach ($port in @(8000, 8501)) {
    $lines = netstat -ano | Select-String ":$port\s"
    if (-not $lines) {
        Write-Host "端口 $port 未监听：服务没启动，或被换端口" -ForegroundColor Yellow
    } else {
        $lines | Select-Object -First 3 | ForEach-Object { Write-Host $_.Line }
    }
}

Write-Host "=== 3. 防火墙入站规则（没有 8000/8501 规则则其它电脑连不上） ===" -ForegroundColor Cyan
$rules = netsh advfirewall firewall show rule name=all dir=in 2>$null | Select-String "8000|8501"
if (-not $rules) {
    Write-Host "未找到 8000/8501 的入站放行规则。" -ForegroundColor Yellow
    Write-Host "请用管理员 PowerShell 运行： scripts\allow_firewall.ps1" -ForegroundColor Yellow
} else {
    $rules | Select-Object -First 6 | ForEach-Object { Write-Host $_.Line }
}

Write-Host "=== 4. 建议访问地址 ===" -ForegroundColor Cyan
Write-Host "  前端： http://${lanIp}:8501"
Write-Host "  后端： http://${lanIp}:8000/docs"
Write-Host "  本机自测： curl http://${lanIp}:8000/api/health" -ForegroundColor DarkGray

Write-Host "=== 5. 仍然打不开？按顺序排查 ===" -ForegroundColor Cyan
Write-Host "  a) 两台设备是否同一网段（前两段 IP 相同，如 10.247.x.x）"
Write-Host "  b) 是否开着 VPN / 代理（先关掉）"
Write-Host "  c) 校园网/公共 Wi-Fi 常有 AP 隔离（设备之间互访被禁）→ 用手机热点，或直接云端部署"
Write-Host "  d) 前端侧边栏的『后端地址』必须是 http://${lanIp}:8000，不能是 127.0.0.1"
