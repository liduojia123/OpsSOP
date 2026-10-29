"""维护循环异常韧性:单轮失败(备份/审计清理抛异常)不能终止每日任务。"""

import asyncio
import logging
from contextlib import suppress

from app import maintenance as maintenance_mod
from app.config import Settings


def _settings(**overrides) -> Settings:
    values = {"backup_hour": 2, "backup_enabled": False}
    values.update(overrides)
    return Settings(**values)


def test_run_maintenance_round_swallows_failure(monkeypatch, caplog):
    def boom(settings):
        raise RuntimeError("audit cleanup exploded")

    monkeypatch.setattr(maintenance_mod, "run_daily_maintenance", boom)
    with caplog.at_level(logging.ERROR, logger="opssop.maintenance"):
        asyncio.run(maintenance_mod.run_maintenance_round(_settings()))
    assert any("维护任务失败" in record.getMessage() for record in caplog.records)


def test_maintenance_loop_survives_round_failure(monkeypatch):
    calls = {"count": 0}

    def flaky_run(settings):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("first round fails")

    monkeypatch.setattr(maintenance_mod, "run_daily_maintenance", flaky_run)
    # 缩短到下一轮的等待,真实 asyncio 睡眠即可,不 patch 全局 asyncio
    monkeypatch.setattr(maintenance_mod, "seconds_until", lambda hour: 0.02)

    async def scenario() -> int:
        task = asyncio.create_task(maintenance_mod.maintenance_loop(_settings()))
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if calls["count"] >= 3:
                    break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return calls["count"]

    rounds = asyncio.run(scenario())
    # 第一轮抛异常后循环没有退出,后续轮次仍被调度
    assert rounds >= 3
