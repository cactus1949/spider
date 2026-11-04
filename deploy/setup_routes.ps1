<#
.SYNOPSIS
  双网卡路由配置：专网走 U-Key WiFi，外网走有线/4G。
.DESCRIPTION
  删除 WiFi 抢占的默认路由，为交易中心专网 IP 添加 /32 精确路由。
  必须以管理员身份运行。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup_routes.ps1 -WifiIface "Wi-Fi" -WanIface "以太网" -TargetIPs "36.137.0.12","36.137.0.13"
#>
param(
    [string]$WifiIface = "Wi-Fi",
    [string]$WanIface  = "以太网",
    [string[]]$TargetIPs = @()
)

$ErrorActionPreference = "Stop"

# 取 WiFi 网关
$wifiCfg = Get-NetIPConfiguration -InterfaceAlias $WifiIface
$wifiGw  = $wifiCfg.IPv4DefaultGateway.NextHop
if (-not $wifiGw) { throw "WiFi 网关获取失败，确认 U-Key 已连接" }
Write-Host "[INFO] WiFi 网关 = $wifiGw"

# 删除 WiFi 默认路由（防止抢占外网）
Get-NetRoute -InterfaceAlias $WifiIface -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Remove-NetRoute -InputObject $_ -Confirm:$false
        Write-Host "[OK ] 删除 WiFi 默认路由"
    }

# 添加专网 /32 路由
foreach ($ip in $TargetIPs) {
    $existed = Get-NetRoute -DestinationPrefix "$ip/32" -ErrorAction SilentlyContinue
    if ($existed) { Remove-NetRoute -InputObject $existed -Confirm:$false }
    New-NetRoute -DestinationPrefix "$ip/32" -InterfaceAlias $WifiIface -NextHop $wifiGw `
        -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
    Write-Host "[OK ] 添加专网路由 $ip/32 via $wifiGw"
}

# 校验外网默认路由仍在
$wanRoute = Get-NetRoute -InterfaceAlias $WanIface -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue
if (-not $wanRoute) { Write-Warning "外网默认路由缺失，请检查 $WanIface"; exit 2 }
Write-Host "[OK ] 外网默认路由存在 via $($wanRoute.NextHop)"
exit 0
