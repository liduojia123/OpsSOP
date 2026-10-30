from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import can_view_case, can_view_fault, can_view_sop
from app.database import get_db
from app.deps import get_current_user, require_reviewer
from app.models import AuditEvent, Case, Fault, Sop, User
from app.schemas import AuditEventOut, AuditTimelineOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/entity", response_model=list[AuditTimelineOut])
def entity_timeline(
    entity_type: str = Query(pattern="^(fault|case|sop)$"),
    entity_id: int = Query(ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AuditTimelineOut]:
    """单个实体的操作时间线:先校验实体可见性,再返回该实体的审计动作。

    失败/无权情况返回 404/403,避免通过时间线探测他人记录是否存在。
    """
    model = {"fault": Fault, "case": Case, "sop": Sop}[entity_type]
    entity = db.get(model, entity_id)
    if entity is None:
        raise HTTPException(404, "对象不存在")
    allowed = {
        "fault": lambda: can_view_fault(user, entity),
        "case": lambda: can_view_case(user, entity),
        "sop": lambda: can_view_sop(user, entity),
    }[entity_type]()
    if not allowed:
        raise HTTPException(403, "无权查看该对象的操作记录")

    rows = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.target_type == entity_type,
            AuditEvent.target_id == str(entity_id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(limit)
    ).all()
    names = {
        row.id: row.display_name
        for row in db.scalars(
            select(User).where(User.id.in_({r.actor_id for r in rows if r.actor_id}))
        )
    }
    return [
        AuditTimelineOut(
            id=row.id,
            action=row.action,
            actor_name=names.get(row.actor_id or 0, "系统"),
            detail=row.detail or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("", response_model=list[AuditEventOut])
def list_audit_events(
    db: Session = Depends(get_db),
    _: User = Depends(require_reviewer),
    action: str | None = Query(default=None),
    actor_id: int | None = Query(default=None, ge=1),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEventOut]:
    if (start and start.utcoffset() is None) or (end and end.utcoffset() is None):
        raise HTTPException(400, "时间必须包含时区")
    if start and end and start >= end:
        raise HTTPException(400, "开始时间必须早于结束时间")
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if start is not None:
        stmt = stmt.where(AuditEvent.created_at >= start)
    if end is not None:
        stmt = stmt.where(AuditEvent.created_at < end)
    if before_id is not None:
        stmt = stmt.where(AuditEvent.id < before_id)
    return [AuditEventOut.model_validate(e) for e in db.scalars(stmt).all()]
