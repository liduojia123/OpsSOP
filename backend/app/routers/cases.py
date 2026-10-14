from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, record_audit, require_operator, require_reviewer
from app.models import Case, CaseStatus, Role, Sop, SopStatus, User, utcnow
from app.schemas import CaseOut, CaseUpdate, ReviewRequest, SopOut

router = APIRouter(prefix="/api/cases", tags=["cases"])


def _case_visible(user: User):
    """查询过滤:已通过案例对所有登录用户可读;待审核/退回仅创建者与审核权限可见。"""
    if user.role == Role.operator:
        return or_(Case.created_by == user.id, Case.status == CaseStatus.approved)
    return True


def _can_view_case(user: User, case: Case) -> bool:
    if user.role == Role.operator:
        return case.created_by == user.id or case.status == CaseStatus.approved
    return True


def _get_editable_case(db: Session, case_id: int, user: User) -> Case:
    """仅退回(returned)状态可编辑:待审核内容是审核快照,不能无声变更。"""
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "案例不存在")
    if user.role == Role.operator and case.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能修改自己创建的案例")
    if case.status == CaseStatus.approved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "已通过的案例不可修改")
    if case.status == CaseStatus.pending_review:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "待审核案例已锁定审核快照,不可修改;如需修改请等待退回")
    return case


def _missing_case_fields(case: Case) -> list[str]:
    """提交/审批共用:人工确认字段完整性校验。"""
    return [
        name
        for name, value in [
            ("人工确认根因", case.confirmed_root_cause),
            ("处理步骤", case.handling_steps),
            ("恢复验证", case.recovery_verification),
        ]
        if not value.strip()
    ]


@router.get("", response_model=list[CaseOut])
def list_cases(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = Query(default=None, description="标题/现象/根因关键词"),
    status_filter: CaseStatus | None = Query(default=None, alias="status"),
    category_id: int | None = None,
) -> list[CaseOut]:
    stmt = select(Case).where(_case_visible(user))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Case.title.ilike(like),
                Case.symptom.ilike(like),
                Case.confirmed_root_cause.ilike(like),
                Case.service.ilike(like),
            )
        )
    if status_filter is not None:
        stmt = stmt.where(Case.status == status_filter)
    if category_id is not None:
        stmt = stmt.where(Case.category_id == category_id)
    stmt = stmt.order_by(Case.updated_at.desc())
    return [CaseOut.model_validate(c) for c in db.scalars(stmt).all()]


@router.get("/{case_id}", response_model=CaseOut)
def get_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "案例不存在")
    if not _can_view_case(user, case):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该案例")
    return CaseOut.model_validate(case)


@router.patch("/{case_id}", response_model=CaseOut)
def update_case(
    case_id: int,
    body: CaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> CaseOut:
    case = _get_editable_case(db, case_id, user)
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(case, key, value)
    record_audit(db, user, "case.update", "case", case.id, {"fields": list(data.keys())})
    db.commit()
    return CaseOut.model_validate(case)


@router.post("/{case_id}/submit", response_model=CaseOut)
def submit_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> CaseOut:
    """退回后修改完成,重新提交审核。"""
    case = _get_editable_case(db, case_id, user)
    if case.status != CaseStatus.returned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有退回状态可重新提交审核")
    missing = _missing_case_fields(case)
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"缺少人工确认内容: {', '.join(missing)}")
    case.status = CaseStatus.pending_review
    record_audit(db, user, "case.submit", "case", case.id)
    db.commit()
    return CaseOut.model_validate(case)


@router.post("/{case_id}/approve", response_model=CaseOut)
def approve_case(
    case_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> CaseOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "案例不存在")
    if case.status != CaseStatus.pending_review:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有待审核案例可通过审核")
    # 审批时复核全部人工确认字段
    missing = _missing_case_fields(case)
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"人工确认内容不完整,不能通过: {', '.join(missing)}")
    case.status = CaseStatus.approved
    case.review_comment = body.comment
    case.reviewed_by = reviewer.id
    case.reviewed_at = utcnow()
    record_audit(db, reviewer, "case.approve", "case", case.id, {"comment": body.comment[:200]})
    db.commit()
    return CaseOut.model_validate(case)


@router.post("/{case_id}/return", response_model=CaseOut)
def return_case(
    case_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> CaseOut:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "案例不存在")
    if case.status != CaseStatus.pending_review:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有待审核案例可退回")
    if not body.comment.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "退回必须填写审核意见")
    case.status = CaseStatus.returned
    case.review_comment = body.comment
    case.reviewed_by = reviewer.id
    case.reviewed_at = utcnow()
    record_audit(db, reviewer, "case.return", "case", case.id, {"comment": body.comment[:200]})
    db.commit()
    return CaseOut.model_validate(case)


@router.post("/{case_id}/generate-sop", response_model=SopOut, status_code=status.HTTP_201_CREATED)
def generate_sop_draft(
    case_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    """人工主动从已通过案例生成 SOP 草稿(字段模板拷贝,不含 AI 生成,不自动执行)。"""
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "案例不存在")
    if case.status != CaseStatus.approved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有已通过案例可生成 SOP 草稿")
    if user.role == Role.operator and case.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能从自己提交的案例生成草稿")

    import uuid

    sop = Sop(
        doc_id=str(uuid.uuid4()),
        version=1,
        title=f"[草稿] {case.title}",
        category_id=case.category_id,
        tags=list(case.tags or []),
        applicable_env=f"环境: {case.environment}\n服务: {case.service}\n组件版本: {case.component_version}",
        symptom=case.symptom,
        preconditions="【待补】请补充前置条件",
        check_steps="【待补】请补充排查步骤",
        handling_steps=case.handling_steps,
        recovery_verification=case.recovery_verification,
        rollback_steps="【待补】请补充回滚方式",
        notes="【待补】请补充注意事项",
        source_case_id=case.id,
        maintainer_id=user.id,
        status=SopStatus.draft,
        created_by=user.id,
    )
    db.add(sop)
    db.flush()
    record_audit(db, user, "sop.generate_from_case", "sop", sop.id, {"case_id": case.id})
    db.commit()
    return SopOut.model_validate(sop)
