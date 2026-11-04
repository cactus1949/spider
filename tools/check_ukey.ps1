# 现场检测：外部 USB 口上的设备和证书，看多把 UKey 能不能分开。
# 在插着钥匙的 Windows 电脑上执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_ukey.ps1
# 同目录会写出 ukey-report.txt，把这个文件发回来即可。
$ErrorActionPreference = "Continue"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Test-LocationSerial([string]$Text) {
    return $Text -match '^\d+&[0-9A-F]+&\d+&\d+$'
}

$lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$Text) { $lines.Add($Text) | Out-Null; Write-Host $Text }

Add-Line "=== 外部 USB 设备 ==="
$devices = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object {
    $_.InstanceId -match '^(USB|HID|USBSTOR)\\'
})
$ukeys = @()
foreach ($device in $devices) {
    $name = [string]$device.FriendlyName
    $id = [string]$device.InstanceId
    if ($name -match '根集线器|主机控制器|Host Controller|Root Hub|集线器|USB Hub|Bluetooth|蓝牙|Camera|摄像头|FaceTime|复合设备|USB Composite Device') {
        continue
    }
    $tail = ($id -split '\\')[-1]
    $serial = if (Test-LocationSerial $tail) { "" } else { $tail }
    $isUkey = $id -match 'VID_096E'
    if (-not $isUkey -and $id -like 'HID\*') { continue }
    $kind = if ($isUkey) { "UKey" } else { "USB" }
    Add-Line ("{0}`t{1}`t序列号={2}`t{3}" -f $kind, $name, ($(if ($serial) { $serial } else { "无" })), $id)
    if ($isUkey) { $ukeys += [pscustomobject]@{ Name = $name; Serial = $serial; Id = $id } }
}

Add-Line ""
Add-Line "=== 未过期证书（颁发给 / 单位 / 截止日期 / 颁发者）==="
$orgs = New-Object System.Collections.Generic.List[string]
Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
    Where-Object { $_.NotAfter -gt (Get-Date) } |
    ForEach-Object {
        $subject = [string]$_.Subject
        $org = ""
        if ($subject -match '(?:^|,\s*)O=([^,]+)') { $org = $Matches[1].Trim() }
        if ($org -and -not $orgs.Contains($org)) { $orgs.Add($org) }
        Add-Line ("颁发给: {0}" -f $subject)
        Add-Line ("单位: {0}" -f ($(if ($org) { $org } else { "无" })))
        Add-Line ("截止日期: {0}" -f $_.NotAfter.ToString("yyyy-MM-dd"))
        Add-Line ("颁发者: {0}" -f $_.Issuer)
        Add-Line ("指纹: {0}" -f $_.Thumbprint)
        Add-Line "---"
    }

Add-Line ""
Add-Line "=== 结论 ==="
$realSerials = @($ukeys | Where-Object { $_.Serial } | ForEach-Object { $_.Serial } | Select-Object -Unique)
if ($ukeys.Count -eq 0) {
    Add-Line "没有发现飞天 UKey。请把钥匙插在外部 USB 口上再运行。"
} elseif ($ukeys.Count -ge 2 -and $realSerials.Count -eq $ukeys.Count) {
    Add-Line ("可以区分。{0} 把 UKey 的序列号各不相同：{1}" -f $ukeys.Count, ($realSerials -join "、"))
} elseif ($orgs.Count -ge 2) {
    Add-Line ("可以按证书单位区分：{0}" -f ($orgs -join "；"))
} elseif ($ukeys.Count -lt 2) {
    Add-Line "目前只看到一把 UKey。请把要对比的钥匙同时插上再运行一次。"
} else {
    Add-Line "看到多条飞天设备，但没有各不相同的序列号，证书单位也没有分开。这些编号可能只是插口位置。"
}

$report = Join-Path (Get-Location) "ukey-report.txt"
[System.IO.File]::WriteAllLines($report, $lines, [System.Text.UTF8Encoding]::new($false))
Add-Line ""
Add-Line ("报告已写入: {0}" -f $report)
