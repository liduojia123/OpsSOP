"""每日维护任务：定时备份与保留清理。

仅在生产模式启动（单实例）。首次启动若已过当天备份时间且当天还没有备份，
60 秒后补跑一次；之后每天在 BACKUP_HOUR 指定的本地时间执行。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta

from app.backup import BackupError, list_backups, run_backup
from app.config import Settings
from app.database import SessionLocal
from app.deps import record_audit
from app.retention import prune_audit_events, prune_login_attempts

logger = logging.getLogger("opssop.maintenance")


def next_run_at(hour: int, now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def seconds_until(hour: int, now: datetime | None = None) -> float:
    current = now or datetime.now()
    return max(1.0, (next_run_at(hour, current) - current).total_seconds())


def _has_backup_today(settings: Settings) -> bool:
    try:
        items = list_backups(settings)
    except BackupError:
        return False
    today = datetime.now().date()
    for item in items:
        value = item.get("created_at")
        if not value:
            continue
        try:
            created = datetime.fromisoformat(str(value)).astimezone()
        except ValueError:
            continue
        if created.date() == today:
            return True
    return False


def run_daily_maintenance(settings: Settings) -> dict:
    results: dict[str, object] = {}
    db = SessionLocal()
    try:
        if settings.backup_enabled:
            try:
                backup = run_backup(settings, "scheduled", db)
                record_audit(
                    db,
                    None,
                    "backup.created",
                    "backup",
                    backup["name"],
                    {"size_bytes": backup["size_bytes"], "pruned": backup["pruned"]},
                )
                db.commit()
                results["backup"] = "ok"
            except Exception as exc:  # noqa: BLE001 - 定时任务失败要留审计且继续清理
                db.rollback()
                logger.exception("定时备份失败")
                record_audit(
                    db,
                    None,
                    "backup.failed",
                    "backup",
                    "",
                    {"reason": f"{type(exc).__name__}: {str(exc)[:200]}"},
                )
                db.commit()
                results["backup"] = "failed"
        audit_deleted = prune_audit_events(db, settings.audit_retention_days)
        login_deleted = prune_login_attempts(db)
        record_audit(
            db,
            None,
            "maintenance.retention",
            "system",
            "",
            {"audit_deleted": audit_deleted, "login_attempts_deleted": login_deleted},
        )
        db.commit()
        results["audit_deleted"] = audit_deleted
        results["login_attempts_deleted"] = login_deleted
        logger.info("每日维护完成: %s", results)
        return results
    finally:
        db.close()


async def maintenance_loop(settings: Settings) -> None:
    try:
        if (
            datetime.now().hour >= settings.backup_hour
            and settings.backup_enabled
            and not _has_backup_today(settings)
        ):
            await asyncio.sleep(60)
            await asyncio.to_thread(run_daily_maintenance, settings)
        while True:
            delay = seconds_until(settings.backup_hour)
            logger.info("下次维护任务在 %.0f 秒后", delay)
            await asyncio.sleep(delay)
            await asyncio.to_thread(run_daily_maintenance, settings)
    except asyncio.CancelledError:
        with suppress(Exception):
            logger.info("维护任务已停止")
        raise

