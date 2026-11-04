"""测试自动补漏规划与防雪崩限制。"""

from __future__ import annotations

import pytest

from spider_gd.core.scheduler.backfill import BackfillPolicy, plan_backfill


@pytest.mark.unit
def test_plan_caps_items_per_run():
    policy = BackfillPolicy(max_items_per_run=2, min_interval_sec=0)
    plan = plan_backfill(
        indicator="日清算",
        present_dates=[],
        start="2026-01-01",
        end="2026-01-05",
        policy=policy,
        last_run_ts=None,
        now_ts=1000.0,
    )
    # 5 个缺口，限额 2
    assert plan.total_gaps == 5
    assert plan.dates == ["2026-01-01", "2026-01-02"]


@pytest.mark.unit
def test_plan_respects_cooldown():
    policy = BackfillPolicy(max_items_per_run=5, min_interval_sec=3600)
    plan = plan_backfill(
        indicator="日清算",
        present_dates=[],
        start="2026-01-01",
        end="2026-01-03",
        policy=policy,
        last_run_ts=1000.0,
        now_ts=1000.0 + 60,  # 仅过 60s < 3600s
    )
    assert plan.skipped_cooldown is True
    assert plan.dates == []


@pytest.mark.unit
def test_plan_after_cooldown_runs():
    policy = BackfillPolicy(max_items_per_run=5, min_interval_sec=3600)
    plan = plan_backfill(
        indicator="日清算",
        present_dates=["2026-01-02"],
        start="2026-01-01",
        end="2026-01-03",
        policy=policy,
        last_run_ts=1000.0,
        now_ts=1000.0 + 4000,  # 超过冷却
    )
    assert plan.skipped_cooldown is False
    assert plan.dates == ["2026-01-01", "2026-01-03"]
