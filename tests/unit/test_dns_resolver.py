"""测试专网 DNS 解析的三级回退（外部调用全部 Mock）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spider_gd.infrastructure.network import dns_resolver
from spider_gd.infrastructure.network.dns_resolver import DnsResolver


@pytest.mark.unit
def test_resolve_via_wifi_dns_success(mocker, tmp_path: Path):
    mocker.patch.object(dns_resolver, "get_wifi_dns", return_value=["10.0.0.1"])
    mocker.patch.object(dns_resolver, "resolve_via_dns", return_value=["36.137.0.12"])
    resolver = DnsResolver("pm.gd.csg.cn", tmp_path / "cache.yaml")
    assert resolver.resolve() == ["36.137.0.12"]
    # 解析成功应写入缓存
    cached = yaml.safe_load((tmp_path / "cache.yaml").read_text(encoding="utf-8"))
    assert cached["cached_ips"] == ["36.137.0.12"]
    assert resolver.cached_ips == ["36.137.0.12"]


@pytest.mark.unit
def test_resolve_via_dns_deduplicates_addresses(mocker):
    out = "\n".join([
        "Server:  10.0.0.1",
        "Address:  10.0.0.1",
        "Name: pm.gd.csg.cn",
        "Address:  36.137.0.12",
        "Address:  36.137.0.12",
    ])
    mocker.patch.object(
        dns_resolver.subprocess,
        "run",
        return_value=type("R", (), {"stdout": out})(),
    )
    assert dns_resolver.resolve_via_dns("pm.gd.csg.cn", "10.0.0.1") == ["36.137.0.12"]


@pytest.mark.unit
def test_resolve_falls_back_to_cache(mocker, tmp_path: Path):
    cache = tmp_path / "cache.yaml"
    cache.write_text(yaml.safe_dump({"cached_ips": ["1.2.3.4"]}), encoding="utf-8")
    mocker.patch.object(dns_resolver, "get_wifi_dns", return_value=[])
    mocker.patch.object(dns_resolver.subprocess, "run", side_effect=OSError("no ping"))
    resolver = DnsResolver("pm.gd.csg.cn", cache)
    assert resolver.resolve() == ["1.2.3.4"]


@pytest.mark.unit
def test_resolve_raises_when_all_fail(mocker, tmp_path: Path):
    mocker.patch.object(dns_resolver, "get_wifi_dns", return_value=[])
    mocker.patch.object(dns_resolver.subprocess, "run", side_effect=OSError("no ping"))
    resolver = DnsResolver("pm.gd.csg.cn", tmp_path / "cache.yaml")
    with pytest.raises(RuntimeError, match="无法解析"):
        resolver.resolve()
