"""数据保留：清理过期审计记录与登录尝试。

刻意不删除故障、案例、SOP 等业务知识数据；30 天保留只应用于审计/登录
记录与备份文件。若未来需要清理业务数据，应实现归档与显式确认，而不是
在定时任务里直接删除。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import AuditEvent, LoginAttempt, Notification


def prune_audit_events(db: Session, retention_days: int, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    result = db.execute(delete(AuditEvent).where(AuditEvent.created_at < cutoff))
    db.commit()
    return result.rowcount or 0


def prune_login_attempts(db: Session, older_than_minutes: int = 60) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    result = db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
    db.commit()
    return result.rowcount or 0


def prune_notifications(db: Session, retention_days: int = 90, now: datetime | None = None) -> int:
    """清理已读且超过保留期的站内通知;未读通知始终保留,避免静默丢提醒。"""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    result = db.execute(
        delete(Notification).where(
            Notification.read_at.is_not(None),
            Notification.created_at < cutoff,
        )
    )
    db.commit()
    return result.rowcount or 0
