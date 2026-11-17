"""判断证书私钥现在能不能用，不弹 PIN。

这台 Linux 环境不会去读 Windows 证书库。函数在非 Windows 上返回 None，
表示「没检查」，调用方不能把缓存证书当成已插入。
"""

from __future__ import annotations

import sys


def silent_thumbprints() -> set[str] | None:
    """返回当前能静默打开私钥的证书指纹。

    使用 ``CryptAcquireCertificatePrivateKey`` 的静默标志。需要界面或 PIN 时
    视为不可用，不去试 PIN。非 Windows，或探测本身失败时返回 None。

    Returns:
        set[str] | None: 小写指纹集合。没检查时为 None。

    Examples:
        >>> silent_thumbprints() is None or isinstance(silent_thumbprints(), set)
        True
    """
    if sys.platform != "win32":
        return None
    try:
        return _silent_thumbprints_windows()
    except Exception:
        return None


def _silent_thumbprints_windows() -> set[str]:
    """在 Windows 上静默探测。探测失败交给调用方变成 None。"""
    import json

    from spider_gd.infrastructure.network.usb_devices import _command_text, _powershell

    script = r"""
$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Security.Cryptography.X509Certificates;
public static class SilentKey {
  [DllImport("crypt32.dll", SetLastError=true)]
  public static extern bool CryptAcquireCertificatePrivateKey(
    IntPtr pCert, uint dwFlags, IntPtr pvParameters, out IntPtr phCryptProvOrNCryptKey,
    out uint pdwKeySpec, out bool pfCallerFreeProvOrNCryptKey);
  [DllImport("ncrypt.dll")]
  public static extern int NCryptFreeObject(IntPtr hObject);
  public static bool CanUse(X509Certificate2 cert) {
    IntPtr handle; uint spec; bool freeCaller;
    const uint SILENT = 0x40; const uint ALLOW_NCRYPT = 0x10000;
    bool ok = CryptAcquireCertificatePrivateKey(cert.Handle, SILENT | ALLOW_NCRYPT,
      IntPtr.Zero, out handle, out spec, out freeCaller);
    if (ok && freeCaller && handle != IntPtr.Zero) NCryptFreeObject(handle);
    return ok;
  }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue | Out-Null
$found = @()
Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.NotAfter -gt (Get-Date) } | ForEach-Object {
  if ([SilentKey]::CanUse($_)) { $found += $_.Thumbprint.ToLower() }
}
$found | ConvertTo-Json -Compress
"""
    ok, text = _command_text(_powershell(script), 8.0, "证书可用性")
    if not ok or not text.strip() or text == "（没有输出）":
        return set()
    data = json.loads(text)
    rows = data if isinstance(data, list) else [data]
    return {str(item).lower() for item in rows if str(item).strip()}
