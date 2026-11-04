"""双网络连通性验证（专网可达 + 外网可达 + 路由正确）。

实现 :class:`spider_gd.interfaces.network.INetworkValidator`。
"""

from __future__ import annotations

from loguru import logger

from spider_gd.infrastructure.network._powershell import run_ps, run_ps_json
from spider_gd.interfaces.network import ValidateReport


class NetworkValidator:
    def validate(
        self,
        target_domain: str,
        target_ips: list[str],
        *,
        private_iface: str = "",
        public_probe_host: str = "www.baidu.com",
        public_probe_port: int = 443,
    ) -> ValidateReport:
        # 1. 专网（443 端口）：有动态解析 IP 时优先测 IP，避免再次走系统 DNS。
        private_targets = target_ips or [target_domain]
        private_ok = False
        for target in private_targets:
            private_out = run_ps(f"(Test-NetConnection {target} -Port 443).TcpTestSucceeded")
            if private_out.strip().lower() == "true":
                private_ok = True
                break

        # 2. 外网：验证公网默认出口仍可访问 HTTPS。
        public_out = run_ps(
            f"(Test-NetConnection {public_probe_host} -Port {public_probe_port}).TcpTestSucceeded"
        )
        public_ok = public_out.strip().lower() == "true"

        # 3. 专网 /32 路由是否存在，且可选校验落在指定 U-Key/WiFi 网卡上。
        routes = run_ps_json(
            "Get-NetRoute | Select-Object DestinationPrefix,NextHop,InterfaceAlias,RouteMetric,ifIndex"
        )
        routes_ok = True
        if target_ips:
            for ip in target_ips:
                prefix = f"{ip}/32"
                matches = [r for r in routes if r.get("DestinationPrefix") == prefix]
                if private_iface:
                    matches = [r for r in matches if r.get("InterfaceAlias") == private_iface]
                if not matches:
                    routes_ok = False
                    break

        report = ValidateReport(
            public_ok=public_ok,
            private_ok=private_ok,
            routes_ok=routes_ok,
        )
        logger.info(
            "验证结果 public={} private={} routes={} passed={}",
            public_ok,
            private_ok,
            routes_ok,
            report.passed,
        )
        return report
