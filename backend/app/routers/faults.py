from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.concurrency import advance_version
from app.deps import get_current_user, record_audit, require_operator
from app.models import Case, CaseStatus, Fault, FaultRecord, FaultStatus, RecordKind, Role, User, utcnow
from app.schemas import (
    FaultCreate,
    FaultDetailOut,
    FaultOut,
    FaultRecordCreate,
    FaultRecordOut,
    FaultUpdate,
    ResolveFaultRequest,
)

router = APIRouter(prefix="/api/faults", tags=["faults"])


def _visible_faults(user: User):
    """查询过滤:运维人员只看自己的故障;审核/管理可见全部。"""
    if user.role == Role.operator:
        return Fault.created_by == user.id
    return True


def _can_view_fault(user: User, fault: Fault) -> bool:
    if user.role == Role.operator:
        return fault.created_by == user.id
    return True


def _get_writable_fault(db: Session, fault_id: int, user: User) -> Fault:
    fault = db.scalar(select(Fault).where(Fault.id == fault_id).with_for_update().execution_options(populate_existing=True))
    if fault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "故障不存在")
    if user.role == Role.operator and fault.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能处理自己创建的故障")
    return fault


@router.get("", response_model=list[FaultOut])
def list_faults(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = Query(default=None, description="标题/描述/日志关键词"),
    status_filter: FaultStatus | None = Query(default=None, alias="status"),
    category_id: int | None = None,
    service: str | None = None,
    environment: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
) -> list[FaultOut]:
    stmt = select(Fault).where(_visible_faults(user))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Fault.title.ilike(like),
                Fault.description.ilike(like),
                Fault.error_logs.ilike(like),
                Fault.service.ilike(like),
            )
        )
    if status_filter is not None:
        stmt = stmt.where(Fault.status == status_filter)
    if category_id is not None:
        stmt = stmt.where(Fault.category_id == category_id)
    if service:
        stmt = stmt.where(Fault.service == service)
    if environment:
        stmt = stmt.where(Fault.environment == environment)
    if occurred_from is not None:
        stmt = stmt.where(Fault.occurred_at >= occurred_from)
    if occurred_to is not None:
        stmt = stmt.where(Fault.occurred_at <= occurred_to)
    stmt = stmt.order_by(Fault.updated_at.desc())
    return [FaultOut.model_validate(f) for f in db.scalars(stmt).all()]


@router.post("", response_model=FaultOut, status_code=status.HTTP_201_CREATED)
def create_fault(
    body: FaultCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> FaultOut:
    fault = Fault(**body.model_dump(), created_by=user.id)
    db.add(fault)
    db.flush()
    record_audit(db, user, "fault.create", "fault", fault.id, {"title": fault.title})
    db.commit()
    return FaultOut.model_validate(fault)


@router.get("/{fault_id}", response_model=FaultDetailOut)
def get_fault(
    fault_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FaultDetailOut:
    fault = db.scalar(
        select(Fault).options(selectinload(Fault.records)).where(Fault.id == fault_id)
    )
    if fault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "故障不存在")
    if not _can_view_fault(user, fault):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该故障")
    return FaultDetailOut.model_validate(fault)


@router.patch("/{fault_id}", response_model=FaultOut)
def update_fault(
    fault_id: int,
    body: FaultUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> FaultOut:
    fault = _get_writable_fault(db, fault_id, user)
    if fault.status == FaultStatus.resolved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "已解决的故障不可再编辑")
    data = body.model_dump(exclude_unset=True)
    # 状态流转只允许 draft <-> open;resolved 只能通过 /resolve 人工确认
    if "status" in data and data["status"] not in (FaultStatus.draft, FaultStatus.open):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "状态只能在草稿/处理中之间切换")
    for key, value in data.items():
        setattr(fault, key, value)
    record_audit(db, user, "fault.update", "fault", fault.id, {"fields": list(data.keys())})
    db.commit()
    return FaultOut.model_validate(fault)


@router.post("/{fault_id}/records", response_model=FaultRecordOut, status_code=status.HTTP_201_CREATED)
def add_record(
    fault_id: int,
    body: FaultRecordCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> FaultRecordOut:
    fault = _get_writable_fault(db, fault_id, user)
    if fault.status == FaultStatus.resolved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "已解决的故障不可再追加记录")
    record = FaultRecord(fault_id=fault.id, kind=body.kind, content=body.content, created_by=user.id)
    db.add(record)
    db.flush()
    record_audit(db, user, "fault.record_add", "fault", fault.id, {"kind": body.kind.value})
    db.commit()
    return FaultRecordOut.model_validate(record)


@router.post("/{fault_id}/resolve", response_model=FaultDetailOut)
def resolve_fault(
    fault_id: int,
    body: ResolveFaultRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> FaultDetailOut:
    """人工确认解决:填写实际根因/处理步骤/恢复验证,并生成待审核案例。
    未确认的推测(AI 候选原因)不会进入这些字段。

    历史异常数据修复路径(如 repair_fake_resolved 重开后重新确认):
    若该故障已有关联案例,则原地更新既有案例并重新进入待审核,
    保留案例 ID 与来源引用(SOP.source_case_id 等),不删除关联业务数据。
    """
    fault = _get_writable_fault(db, fault_id, user)
    if fault.status == FaultStatus.resolved:
        raise HTTPException(status.HTTP_409_CONFLICT, "该故障已确认解决")
    if not fault.title.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少标题,无法确认解决")

    fault.status = FaultStatus.resolved
    fault.actual_root_cause = body.actual_root_cause
    fault.handling_steps = body.handling_steps
    fault.recovery_verification = body.recovery_verification
    fault.resolved_at = utcnow()

    existing_case = db.scalar(select(Case).where(Case.fault_id == fault.id).with_for_update().execution_options(populate_existing=True))
    if existing_case is not None:
        # 原地更新既有案例:内容变更后需重新审核,案例 ID 不变
        existing_case.title = fault.title
        existing_case.symptom = fault.description
        existing_case.environment = fault.environment
        existing_case.service = fault.service
        existing_case.component_version = fault.component_version
        existing_case.category_id = fault.category_id
        existing_case.tags = list(fault.tags or [])
        existing_case.confirmed_root_cause = body.actual_root_cause
        existing_case.handling_steps = body.handling_steps
        existing_case.recovery_verification = body.recovery_verification
        existing_case.status = CaseStatus.pending_review
        advance_version(existing_case)
        existing_case.review_comment = ""
        existing_case.reviewed_by = None
        existing_case.reviewed_at = None
        case = existing_case
        audit_action = "fault.resolve_case_updated"
    else:
        case = Case(
            fault_id=fault.id,
            title=fault.title,
            symptom=fault.description,
            environment=fault.environment,
            service=fault.service,
            component_version=fault.component_version,
            category_id=fault.category_id,
            tags=list(fault.tags or []),
            confirmed_root_cause=body.actual_root_cause,
            handling_steps=body.handling_steps,
            recovery_verification=body.recovery_verification,
            status=CaseStatus.pending_review,
            created_by=user.id,
        )
        db.add(case)
        audit_action = "fault.resolve"
    db.flush()
    record_audit(
        db,
        user,
        audit_action,
        "fault",
        fault.id,
        {"case_id": case.id, "root_cause": body.actual_root_cause[:200]},
    )
    db.commit()

    fault = db.scalar(
        select(Fault).options(selectinload(Fault.records)).where(Fault.id == fault_id)
    )
    return FaultDetailOut.model_validate(fault)


@router.get("/{fault_id}/records", response_model=list[FaultRecordOut])
def list_records(
    fault_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[FaultRecordOut]:
    fault = db.get(Fault, fault_id)
    if fault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "故障不存在")
    if not _can_view_fault(user, fault):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该故障")
    records = db.scalars(
        select(FaultRecord).where(FaultRecord.fault_id == fault_id).order_by(FaultRecord.id)
    ).all()
    return [FaultRecordOut.model_validate(r) for r in records]
