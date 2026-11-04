"""验证所有冻结接口可导入（链路0 验收项）。"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_all_interfaces_importable():
    from spider_gd import interfaces

    expected = {
        "ICaptchaSolver",
        "ISessionProvider",
        "ILoginFlow",
        "IApiFetcher",
        "ITaskStateStore",
        "ILocalLanding",
        "IScheduler",
        "IAlertGateway",
        "IAgentAdapter",
        "IUsbMonitor",
        "IRouteManager",
        "INetworkValidator",
        "IHealthChecker",
    }
    assert expected.issubset(set(interfaces.__all__))


@pytest.mark.unit
def test_cli_help_lists_subcommands(capsys: pytest.CaptureFixture[str]):
    from spider_gd.__main__ import main

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    for cmd in (
        "crawl",
        "network",
        "schedule",
        "alert",
        "health",
        "agent",
        "delivery-test",
        "delivery-acceptance",
        "delivery-send",
        "delivery-doctor",
        "delivery-retry",
        "delivery-outbox",
        "delivery-verify",
    ):
        assert cmd in out
