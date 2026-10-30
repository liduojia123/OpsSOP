"""站内通知写入:审核流转、告警转故障等事件通知当事人与审核人。

- 只写通知数据,不提交事务;由调用方与业务变更同一事务提交;
- 去重发送对象,避免同一人收到重复提醒;
- 不看正文权限:通知正文只放标题/状态等简短信息,跳转后仍由页面校验权限。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Notification, Role, User


def reviewer_ids(db: Session) -> list[int]:
    """当前可审核的账号:启用的审核人员与管理员。"""
    return list(
        db.scalars(
            select(User.id)
            .where(
                User.is_active.is_(True),
                User.role.in_([Role.reviewer, Role.admin]),
            )
            .order_by(User.id)
        ).all()
    )


def notify(
    db: Session,
    user_ids: Iterable[int | None],
    *,
    kind: str,
    title: str,
    body: str = "",
    link: str = "",
    entity_type: str = "",
    entity_id: int | None = None,
) -> int:
    """给指定账号写通知,返回写入条数(同一账号只写一条)。"""
    created = 0
    seen: set[int] = set()
    for user_id in user_ids:
        if user_id is None or user_id in seen:
            continue
        seen.add(user_id)
        db.add(
            Notification(
                user_id=user_id,
                kind=kind[:32],
                title=title[:255],
                body=(body or "")[:2000],
                link=link[:255],
                entity_type=entity_type[:16],
                entity_id=entity_id,
            )
        )
        created += 1
    return created


def notify_reviewers(
    db: Session,
    *,
    kind: str,
    title: str,
    body: str = "",
    link: str = "",
    entity_type: str = "",
    entity_id: int | None = None,
) -> int:
    return notify(
        db,
        reviewer_ids(db),
        kind=kind,
        title=title,
        body=body,
        link=link,
        entity_type=entity_type,
        entity_id=entity_id,
    )