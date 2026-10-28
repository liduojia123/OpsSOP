from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_reviewer
from app.models import AuditEvent, User
from app.schemas import AuditEventOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


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
