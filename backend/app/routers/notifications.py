"""站内通知:本人未读提醒、标记已读。

通知只按当前用户读取,不提供跨用户查询;管理员审计走 /api/audit。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Notification, User, utcnow
from app.schemas import (
    NotificationCountOut,
    NotificationOut,
    NotificationReadAllOut,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _unread_filter(user: User):
    return (Notification.user_id == user.id, Notification.read_at.is_(None))


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[NotificationOut]:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = db.scalars(stmt.order_by(Notification.created_at.desc()).limit(limit)).all()
    return [NotificationOut.model_validate(row) for row in rows]


@router.get("/unread-count", response_model=NotificationCountOut)
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationCountOut:
    count = db.scalar(
        select(func.count(Notification.id)).where(*_unread_filter(user))
    ) or 0
    return NotificationCountOut(count=count)


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationOut:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "通知不存在")
    if row.read_at is None:
        row.read_at = utcnow()
        db.commit()
    return NotificationOut.model_validate(row)


@router.post("/read-all", response_model=NotificationReadAllOut)
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationReadAllOut:
    rows = db.scalars(
        select(Notification).where(*_unread_filter(user))
    ).all()
    now = utcnow()
    for row in rows:
        row.read_at = now
    if rows:
        db.commit()
    return NotificationReadAllOut(updated=len(rows))