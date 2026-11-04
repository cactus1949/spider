#!/usr/bin/env python3
"""双 WiFi 网络模块冒烟测试脚本。

在 Windows 上以管理员身份运行，逐步验证网络模块的核心功能：
    - 环境探测（适配器 / 路由 / DNS）
    - 网络诊断快照（NetSnapshot + diagnose）
    - DNS 解析测试（DnsResolver）
    - 路由操控测试（删除默认路由 + 添加 /32 精确路由）
    - 连通性验证（NetworkValidator）
    - 清理恢复

用法：
    python tools/smoke_test_network.py
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 模块导入路径
# 脚本在 tools/ 下，需要能导入 src/spider_gd 包。
# 优先依赖 pip install -e . 安装；回退到手动添加 src/ 到 sys.path。
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent          # tools/
_PROJECT_ROOT = _SCRIPT_DIR.parent                     # 项目根
_SRC_DIR = _PROJECT_ROOT / "src"

if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from spider_gd.infrastructure.network._powershell import run_ps, run_ps_json
    from spider_gd.infrastructure.network.diagnoser import NetSnapshot, diagnose
    from spider_gd.infrastructure.network.dns_resolver import DnsResolver, get_wifi_dns
    from spider_gd.infrastructure.network.network_validator import NetworkValidator
    from spider_gd.infrastructure.network.route_writer import is_admin
except ImportError as exc:
    print(f"[FATAL] 模块导入失败: {exc}")
    print("请确保已安装项目: pip install -e .")
    sys.exit(1)


# ========================== ANSI 颜色 ==========================

def _supports_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:  # Windows 10+ 需要开启 VT100
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(
                kernel32.GetStdHandle(-11), 7,
            )
        except Exception:
            pass
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_ANSI = _supports_ansi()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text

def _green(t: str) -> str:   return _c("32", t)
def _red(t: str) -> str:     return _c("31", t)
def _yellow(t: str) -> str:  return _c("33", t)
def _cyan(t: str) -> str:    return _c("36", t)
def _bold(t: str) -> str:    return _c("1", t)


# ========================== 工具函数 ==========================

def _sep(title: str = "") -> None:
    w = 70
    print()
    if title:
        line = f" {title} "
        pad = w - len(line)
        print(_bold(f"{'=' * (pad // 2)}{line}{'=' * (pad - pad // 2)}"))
    else:
        print(_bold("=" * w))


def _run_ps_raw(cmd: str, timeout: int = 15) -> tuple[str, str, int]:
    """执行 PowerShell 命令，返回 (stdout, stderr, returncode)。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except FileNotFoundError:
        return "", "powershell not found", -1


def _is_admin() -> bool:
    try:
        return is_admin()
    except Exception:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
        except Exception:
            return False


def _confirm(prompt: str = "继续? [Y/n]: ") -> bool:
    try:
        ans = input(f"  {_cyan(prompt)}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("", "y", "yes")


def _badge(ok: bool, label: str = "") -> str:
    tag = _green("[ PASS ]") if ok else _red("[ FAIL ]")
    return f"  {tag}  {label}" if label else tag


def _quick_ping(host: str, timeout_ms: int = 3000) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


# ========================== 网络信息查询 ==========================

def _list_adapters() -> list[dict]:
    return run_ps_json(
        "Get-NetAdapter | Select-Object Name,Status,InterfaceMetric,ifIndex,LinkSpeed"
    )


def _list_default_routes() -> list[dict]:
    return run_ps_json(
        'Get-NetRoute | Where-Object {$_.DestinationPrefix -eq "0.0.0.0/0"} '
        "| Select-Object DestinationPrefix,NextHop,InterfaceAlias,RouteMetric,ifIndex"
    )


def _list_all_routes() -> list[dict]:
    return run_ps_json(
        "Get-NetRoute -AddressFamily IPv4 "
        "| Select-Object DestinationPrefix,NextHop,InterfaceAlias,RouteMetric,ifIndex"
    )


def _list_dns() -> list[dict]:
    return run_ps_json(
        "Get-DnsClientServerAddress -AddressFamily IPv4 "
        '| Select-Object InterfaceAlias,@{n="Servers";e={$_.ServerAddresses -join ","}}'
    )


def _get_adapter_ip(iface: str) -> str:
    out, _, _ = _run_ps_raw(
        f'(Get-NetIPConfiguration -InterfaceAlias "{iface}" '
        '-ErrorAction SilentlyContinue).IPv4Address.IPAddress'
    )
    return out


def _print_adapters(adapters: list[dict]) -> None:
    if not adapters:
        print("  (未检测到网络适配器)")
        return
    print(f"  {'名称':<20} {'状态':<10} {'Metric':<8} {'ifIndex':<8} {'速率'}")
    print(f"  {'-'*18}  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*12}")
    for a in adapters:
        name = a.get("Name", "?")
        status = a.get("Status", "?")
        s_disp = _green(status) if status == "Up" else _red(status)
        print(
            f"  {name:<20} {s_disp:<19} {str(a.get('InterfaceMetric','')):<8} "
            f"{str(a.get('ifIndex','')):<8} {a.get('LinkSpeed','')}"
        )


def _print_routes(routes: list[dict], title: str = "默认路由") -> None:
    if not routes:
        print(f"  ({title}: 无)")
        return
    print(f"  [{title}]")
    print(f"  {'接口':<20} {'下一跳':<18} {'Metric':<8} {'ifIndex'}")
    print(f"  {'-'*18}  {'-'*16}  {'-'*6}  {'-'*6}")
    for r in routes:
        print(
            f"  {r.get('InterfaceAlias','?'):<20} {r.get('NextHop','?'):<18} "
            f"{str(r.get('RouteMetric','')):<8} {r.get('ifIndex','')}"
        )


def _print_dns(dns_list: list[dict]) -> None:
    if not dns_list:
        print("  (无 DNS 信息)")
        return
    print(f"  {'接口':<20} {'DNS 服务器'}")
    print(f"  {'-'*18}  {'-'*30}")
    for d in dns_list:
        name = d.get("InterfaceAlias", "?")
        servers = d.get("Servers", "")
        print(f"  {name:<20} {servers}")


# ========================== Step 1: 环境探测 ==========================

def step1_detect() -> tuple[str, str] | None:
    _sep("Step 1: 环境探测")
    print("  列出所有网络适配器、当前路由表、DNS 配置\n")

    # 适配器
    print(_bold("  [网络适配器]"))
    adapters = _list_adapters()
    _print_adapters(adapters)

    # 适配器 IP
    up_names = [a.get("Name", "") for a in adapters if a.get("Status") == "Up"]
    if up_names:
        print()
        print(_bold("  [适配器 IP 地址]"))
        for name in up_names:
            ip = _get_adapter_ip(name)
            print(f"  {name}: {ip or '(无 IPv4)'}")

    # 路由
    print()
    _print_routes(_list_default_routes(), "默认路由")

    # DNS
    print()
    print(_bold("  [DNS 配置]"))
    _print_dns(_list_dns())

    # 让用户选择
    print(f"\n  {_yellow('请选择两个网卡的角色：')}")
    try:
        ext = input(f"  {_cyan('外网（互联网）适配器名称 [WLAN]: ')}").strip() or "WLAN"
        prv = input(f"  {_cyan('专网（U-Key）适配器名称 [WLAN 2]: ')}").strip() or "WLAN 2"
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    ext_ip = _get_adapter_ip(ext)
    prv_ip = _get_adapter_ip(prv)

    print(f"\n  外网适配器: {_bold(ext)}  IP={ext_ip or '?'}")
    print(f"  专网适配器: {_bold(prv)}  IP={prv_ip or '?'}")

    if not _confirm("确认选择? [Y/n]: "):
        return None

    return ext, prv


# ========================== Step 2: 网络诊断快照 ==========================

def step2_diagnose(ext_iface: str, prv_iface: str) -> None:
    _sep("Step 2: 网络诊断快照")
    print("  调用 NetSnapshot.take() 获取当前网络快照\n")

    snap = NetSnapshot.take()

    print(_bold(f"  快照时间: {snap.timestamp}"))
    _print_routes(snap.default_routes, "默认路由快照")
    print()
    print(_bold("  [适配器快照]"))
    for a in snap.adapters:
        print(f"  {a.get('Name','?'):<20} {a.get('Status','?')}")
    print()
    print(_bold("  [DNS 快照]"))
    for d in snap.dns_servers:
        name = d.get("InterfaceAlias", "?")
        servers = d.get("Servers", "")
        if servers:
            print(f"  {name:<20} {servers}")

    # 前后快照诊断
    print(f"\n  等待 3 秒后拍摄第二份快照进行诊断对比 ...")
    time.sleep(3)
    snap2 = NetSnapshot.take()

    report = diagnose(snap, snap2, wifi_iface=prv_iface)
    print(f"\n  诊断结论: {_bold(report.conclusion)}")
    if report.findings:
        print(f"  发现:")
        for f in report.findings:
            print(f"    - {f}")
    print(f"  建议动作:")
    for a in report.fix_actions:
        print(f"    - {a}")

    print(_badge(True, "网络诊断快照"))


# ========================== Step 3: DNS 解析测试 ==========================

def step3_dns(ext_iface: str, prv_iface: str) -> None:
    _sep("Step 3: DNS 解析测试")

    test_domain = "baidu.com"
    print(f"  解析域名: {_bold(test_domain)}\n")

    # WiFi DNS
    print(f"  [{ext_iface} DNS 服务器]")
    ext_dns = get_wifi_dns(ext_iface)
    for d in ext_dns:
        print(f"    {d}")
    if not ext_dns:
        print("    (无)")

    print(f"\n  [{prv_iface} DNS 服务器]")
    prv_dns = get_wifi_dns(prv_iface)
    for d in prv_dns:
        print(f"    {d}")
    if not prv_dns:
        print("    (无)")

    # DnsResolver 解析公网域名
    cache_path = Path(tempfile.gettempdir()) / "smoke_dns_cache.yaml"
    print(f"\n  使用 DnsResolver 解析 {test_domain}（走 {ext_iface} DNS）...")

    resolver = DnsResolver(test_domain, cache_path, wifi_iface=ext_iface)
    ok = False
    try:
        ips = resolver.resolve()
        ok = bool(ips)
        print(f"  解析结果: {_green(str(ips)) if ok else _red('失败')}")
        print(f"  缓存文件: {cache_path}")
    except Exception as exc:
        print(f"  {_red(f'解析失败: {exc}')}")

    print(_badge(ok, f"DNS 解析 {test_domain}"))


# ========================== Step 4: 路由操控测试 ==========================

def _backup_default_route(iface: str) -> dict | None:
    """备份指定接口的默认路由信息。"""
    for r in _list_default_routes():
        if r.get("InterfaceAlias") == iface:
            return dict(r)
    return None


def _delete_default_route(iface: str) -> bool:
    out, err, rc = _run_ps_raw(
        f'Get-NetRoute -InterfaceAlias "{iface}" '
        '-DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue '
        "| Remove-NetRoute -Confirm:$false"
    )
    return rc == 0


def _add_route(prefix: str, iface: str, next_hop: str, metric: int = 1) -> bool:
    _, _, rc = _run_ps_raw(
        f'New-NetRoute -DestinationPrefix "{prefix}" '
        f'-InterfaceAlias "{iface}" -NextHop "{next_hop}" '
        f"-RouteMetric {metric} -PolicyStore ActiveStore"
    )
    return rc == 0


def _remove_route(prefix: str) -> bool:
    _, _, rc = _run_ps_raw(
        f'Get-NetRoute -DestinationPrefix "{prefix}" '
        "-ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false"
    )
    return rc == 0


def _restore_default_route(iface: str, backup: dict) -> bool:
    nh = backup.get("NextHop", "")
    metric = backup.get("RouteMetric", 25)
    return _add_route("0.0.0.0/0", iface, nh, int(metric))


def _route_exists(prefix: str) -> bool:
    routes = _list_all_routes()
    return any(r.get("DestinationPrefix") == prefix for r in routes)


def step4_route_test(ext_iface: str, prv_iface: str) -> bool:
    _sep("Step 4: 路由操控测试（核心）")

    # 获取专网网关
    prv_gw_out, _, _ = _run_ps_raw(
        f'(Get-NetIPConfiguration -InterfaceAlias "{prv_iface}" '
        '-ErrorAction SilentlyContinue).IPv4DefaultGateway.NextHop'
    )
    prv_gw = prv_gw_out.strip() if prv_gw_out else ""

    test_target = "8.8.8.8"
    test_prefix = f"{test_target}/32"

    print(f"  外网适配器: {ext_iface}")
    print(f"  专网适配器: {prv_iface}  (网关: {prv_gw or '?'})")
    print(f"  测试 /32 目标: {test_target}\n")

    _print_routes(_list_default_routes(), "当前默认路由")

    print(f"\n  {_yellow('即将执行以下操作：')}")
    print(f"    1. 备份 {_bold(prv_iface)} 的默认路由")
    print(f"    2. 删除 {prv_iface} 的默认路由")
    print(f"    3. 验证外网仍通（ping baidu.com）")
    print(f"    4. 添加 {test_prefix} 精确路由到 {prv_iface}")
    print(f"    5. 验证路由表变化")
    print(f"    6. 验证外网仍通")
    print()
    if not _confirm("确认执行路由操控? [Y/n]: "):
        print("  跳过路由操控测试")
        return True

    # 备份
    backup = _backup_default_route(prv_iface)
    if backup:
        print(f"  已备份 {prv_iface} 默认路由: "
              f"NextHop={backup.get('NextHop')}, Metric={backup.get('RouteMetric')}")
    else:
        print(f"  {_yellow(f'{prv_iface} 无默认路由，无需备份')}")

    deleted = False
    added_prefix: str | None = None
    all_ok = True

    try:
        # 2. 删除专网默认路由
        print(f"\n  正在删除 {prv_iface} 默认路由 ...")
        if backup:
            if _delete_default_route(prv_iface):
                deleted = True
                print(f"  {_green('已删除')}")
            else:
                print(f"  {_red('删除失败')}")
                all_ok = False
        else:
            print(f"  (无默认路由，跳过)")

        time.sleep(2)

        # 3. 验证外网通
        print(f"\n  验证外网连通性 (ping baidu.com) ...")
        ext_ok = _quick_ping("baidu.com")
        print(_badge(ext_ok, "外网 ping baidu.com"))
        if not ext_ok:
            all_ok = False
            print(f"  {_red('警告: 外网不通！建议立即回滚')}")
            if _confirm("回滚默认路由? [Y/n]: "):
                if backup and deleted:
                    if _restore_default_route(prv_iface, backup):
                        print(f"  {_green('已回滚')}")
                        deleted = False
                return False

        # 4. 添加 /32 精确路由
        if prv_gw:
            print(f"\n  添加精确路由: {test_prefix} -> {prv_iface} (via {prv_gw}) ...")
            if _add_route(test_prefix, prv_iface, prv_gw):
                added_prefix = test_prefix
                print(f"  {_green('添加成功')}")
            else:
                print(f"  {_red('添加失败')}")
                all_ok = False
        else:
            print(f"  {_yellow(f'{prv_iface} 无网关，跳过 /32 路由添加')}")

        time.sleep(1)

        # 5. 验证路由表
        print(f"\n  验证路由表变化 ...")
        print(_bold("  [操作后默认路由]"))
        _print_routes(_list_default_routes(), "当前默认路由")

        if added_prefix:
            exists = _route_exists(added_prefix)
            print(_badge(exists, f"/32 路由 {added_prefix} 存在"))
            if not exists:
                all_ok = False

        # 6. 验证外网仍通
        print()
        ext_ok2 = _quick_ping("baidu.com")
        print(_badge(ext_ok2, "外网 ping baidu.com（路由操控后）"))
        if not ext_ok2:
            all_ok = False

    finally:
        # 清理
        print(f"\n  {_yellow('--- 清理阶段 ---')}")
        if added_prefix:
            print(f"  删除测试路由 {added_prefix} ...")
            if _remove_route(added_prefix):
                print(f"  {_green('已删除')}")
            else:
                print(f"  {_red('删除失败，请手动清理')}")

        if backup and deleted:
            print(f"  恢复 {prv_iface} 默认路由 ...")
            # 检查是否已存在
            has_default = any(
                r.get("InterfaceAlias") == prv_iface
                for r in _list_default_routes()
            )
            if not has_default:
                if _restore_default_route(prv_iface, backup):
                    print(f"  {_green('已恢复')}")
                else:
                    print(f"  {_red('恢复失败，请手动添加')}")
            else:
                print(f"  (默认路由已存在，跳过)")

        time.sleep(1)
        print(f"\n  {_bold('[恢复后默认路由]')}")
        _print_routes(_list_default_routes(), "当前默认路由")

        # 最终验证
        print()
        e1 = _quick_ping("baidu.com")
        print(_badge(e1, "外网 baidu.com"))
        e2 = _quick_ping(prv_gw) if prv_gw else True
        if prv_gw:
            print(_badge(e2, f"专网网关 {prv_gw}"))

    print(_badge(all_ok, "路由操控测试"))
    return all_ok


# ========================== Step 5: 连通性验证 ==========================

def step5_validate(ext_iface: str, prv_iface: str) -> bool:
    _sep("Step 5: 连通性验证")
    print("  使用 NetworkValidator 验证双网络状态\n")

    test_domain = "baidu.com"
    test_ips: list[str] = []

    validator = NetworkValidator()
    try:
        report = validator.validate(test_domain, test_ips)
        print(_badge(report.public_ok, f"公网连通 ({test_domain})"))
        print(_badge(report.private_ok, "专网连通 (443 端口)"))
        print(_badge(report.routes_ok, "专网 /32 路由"))
        print(f"\n  综合结果: {_badge(report.passed)}")
        return report.passed
    except Exception as exc:
        print(f"  {_red(f'验证异常: {exc}')}")
        return False


# ========================== Step 6: 清理恢复 ==========================

def step6_cleanup(ext_iface: str, prv_iface: str) -> None:
    _sep("Step 6: 清理恢复")
    print("  确保所有路由修改已清理，网络恢复原始状态\n")

    # 检查多余默认路由
    routes = _list_default_routes()
    prv_defaults = [
        r for r in routes if r.get("InterfaceAlias") == prv_iface
    ]
    if len(prv_defaults) > 1:
        print(f"  {_yellow(f'{prv_iface} 存在 {len(prv_defaults)} 条默认路由，清理多余的 ...')}")
        for r in prv_defaults[1:]:
            prefix = r.get("DestinationPrefix", "0.0.0.0/0")
            if _remove_route(prefix):
                nh = r.get('NextHop', '?')
                print(f"  {_green(f'已删除: {prefix} via {nh}')}")

    # 最终状态
    print(_bold("  [最终适配器状态]"))
    _print_adapters(_list_adapters())
    print()
    _print_routes(_list_default_routes(), "最终默认路由")
    print()
    print(_bold("  [最终 DNS]"))
    _print_dns(_list_dns())

    print()
    e1 = _quick_ping("baidu.com")
    print(_badge(e1, "外网 baidu.com"))

    prv_ip = _get_adapter_ip(prv_iface)
    if prv_ip:
        e2 = _quick_ping(prv_ip, 2000)
        print(_badge(e2, f"专网适配器 IP {prv_ip}"))

    print(_badge(True, "清理恢复完成"))


# ========================== 主流程 ==========================

def main() -> None:
    # 管理员检查
    if not _is_admin():
        print(_red("  [ERROR] 此脚本需要管理员权限运行！"))
        print("  请右键「以管理员身份运行」命令行，然后执行:")
        print(f"    python {_SCRIPT_DIR / 'smoke_test_network.py'}")
        sys.exit(1)

    _sep("双 WiFi 网络模块冒烟测试")
    print(f"  项目: {_PROJECT_ROOT}")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  权限: {_green('管理员')}")

    results: dict[str, bool] = {}

    # Step 1
    adapter_pair = step1_detect()
    if adapter_pair is None:
        print(_red("\n  用户取消或未选择适配器，退出。"))
        sys.exit(0)
    ext_iface, prv_iface = adapter_pair
    results["Step 1: 环境探测"] = True

    # Step 2
    try:
        step2_diagnose(ext_iface, prv_iface)
        results["Step 2: 网络诊断快照"] = True
    except Exception as exc:
        print(f"  {_red(f'Step 2 异常: {exc}')}")
        results["Step 2: 网络诊断快照"] = False

    # Step 3
    try:
        step3_dns(ext_iface, prv_iface)
        results["Step 3: DNS 解析测试"] = True
    except Exception as exc:
        print(f"  {_red(f'Step 3 异常: {exc}')}")
        results["Step 3: DNS 解析测试"] = False

    # Step 4
    try:
        ok = step4_route_test(ext_iface, prv_iface)
        results["Step 4: 路由操控测试"] = ok
    except Exception as exc:
        print(f"  {_red(f'Step 4 异常: {exc}')}")
        results["Step 4: 路由操控测试"] = False

    # Step 5
    try:
        ok = step5_validate(ext_iface, prv_iface)
        results["Step 5: 连通性验证"] = ok
    except Exception as exc:
        print(f"  {_red(f'Step 5 异常: {exc}')}")
        results["Step 5: 连通性验证"] = False

    # Step 6 (always)
    try:
        step6_cleanup(ext_iface, prv_iface)
        results["Step 6: 清理恢复"] = True
    except Exception as exc:
        print(f"  {_red(f'Step 6 异常: {exc}')}")
        results["Step 6: 清理恢复"] = False

    # 汇总
    _sep("测试汇总报告")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        print(_badge(ok, name))
    print(f"\n  总计: {_green(str(passed))}/{total} 通过")
    if passed == total:
        print(f"  {_green(_bold('ALL PASSED ✓'))}")
    else:
        print(f"  {_red(_bold(f'{total - passed} FAILED ✗'))}")
    _sep()


if __name__ == "__main__":
    main()

