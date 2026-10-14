import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, record_audit, require_operator, require_reviewer
from app.models import (
    FeedbackKind,
    Role,
    Sop,
    SopFeedback,
    SopStatus,
    User,
    utcnow,
)
from app.schemas import (
    ReviewRequest,
    SopCreate,
    SopFeedbackCreate,
    SopFeedbackOut,
    SopOut,
    SopUpdate,
)

router = APIRouter(prefix="/api/sops", tags=["sops"])

# 各状态下的编辑权限:草稿/退回可编辑;已提交/已发布/历史/废弃不可直接编辑
EDITABLE_STATUSES = {SopStatus.draft, SopStatus.returned}


def _sop_visible(user: User):
    """查询过滤:正式(已发布/历史)对所有登录用户可读;草稿类仅创建者与审核权限可见。"""
    if user.role == Role.operator:
        from sqlalchemy import or_

        return or_(
            Sop.created_by == user.id,
            Sop.status.in_([SopStatus.published, SopStatus.superseded, SopStatus.deprecated]),
        )
    return True


def _can_view_sop(user: User, sop: Sop) -> bool:
    if user.role == Role.operator:
        return sop.created_by == user.id or sop.status in (
            SopStatus.published,
            SopStatus.superseded,
            SopStatus.deprecated,
        )
    return True


def _get_editable_sop(db: Session, sop_id: int, user: User) -> Sop:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if user.role == Role.operator and sop.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能编辑自己创建的 SOP")
    if sop.status not in EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前状态不可编辑")
    return sop


def _missing_sop_content(sop: Sop) -> list[str]:
    """提交/发布共用的完整性校验。"""
    return [
        name
        for name, value in [
            ("排查步骤", sop.check_steps),
            ("处理方法", sop.handling_steps),
            ("恢复验证", sop.recovery_verification),
        ]
        if not value.strip()
    ]


@router.get("", response_model=list[SopOut])
def list_sops(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = Query(default=None, description="标题/现象/步骤关键词"),
    status_filter: SopStatus | None = Query(default=None, alias="status"),
    category_id: int | None = None,
    doc_id: str | None = None,
) -> list[SopOut]:
    stmt = select(Sop).where(_sop_visible(user))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Sop.title.ilike(like))
            | (Sop.symptom.ilike(like))
            | (Sop.check_steps.ilike(like))
            | (Sop.handling_steps.ilike(like))
        )
    if status_filter is not None:
        stmt = stmt.where(Sop.status == status_filter)
    if category_id is not None:
        stmt = stmt.where(Sop.category_id == category_id)
    if doc_id is not None:
        stmt = stmt.where(Sop.doc_id == doc_id)
    stmt = stmt.order_by(Sop.updated_at.desc())
    return [SopOut.model_validate(s) for s in db.scalars(stmt).all()]


@router.get("/{sop_id}", response_model=SopOut)
def get_sop(
    sop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SopOut:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if not _can_view_sop(user, sop):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该 SOP")
    return SopOut.model_validate(sop)


@router.get("/{sop_id}/history", response_model=list[SopOut])
def sop_history(
    sop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SopOut]:
    """同文档版本历史:入口记录与每条历史记录都执行可见性过滤,
    防止通过公开旧版本探测他人未发布草稿。"""
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if not _can_view_sop(user, sop):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该 SOP")
    rows = db.scalars(
        select(Sop).where(Sop.doc_id == sop.doc_id).order_by(Sop.version.desc())
    ).all()
    return [SopOut.model_validate(s) for s in rows if _can_view_sop(user, s)]


@router.post("", response_model=SopOut, status_code=status.HTTP_201_CREATED)
def create_sop(
    body: SopCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    sop = Sop(
        doc_id=str(uuid.uuid4()),
        version=1,
        **body.model_dump(),
        status=SopStatus.draft,
        created_by=user.id,
    )
    db.add(sop)
    db.flush()
    record_audit(db, user, "sop.create", "sop", sop.id, {"title": sop.title})
    db.commit()
    return SopOut.model_validate(sop)


@router.patch("/{sop_id}", response_model=SopOut)
def update_sop(
    sop_id: int,
    body: SopUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    sop = _get_editable_sop(db, sop_id, user)
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(sop, key, value)
    record_audit(db, user, "sop.update", "sop", sop.id, {"fields": list(data.keys())})
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/submit", response_model=SopOut)
def submit_sop(
    sop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    sop = _get_editable_sop(db, sop_id, user)
    missing = _missing_sop_content(sop)
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"缺少必填内容: {', '.join(missing)}")
    sop.status = SopStatus.submitted
    record_audit(db, user, "sop.submit", "sop", sop.id)
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/approve", response_model=SopOut)
def approve_sop(
    sop_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> SopOut:
    """发布:只允许从待审核(submitted)发布;退回状态须作者修改后重新提交。
    同一 doc_id 旧的已发布版本自动转为历史版本;新版本在此之前不生效。"""
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if sop.status != SopStatus.submitted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有待审核状态可发布;退回后须重新提交")
    # 审核时复核完整性:防止退回后清空内容再直接发布
    missing = _missing_sop_content(sop)
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"内容不完整,不能发布: {', '.join(missing)}")

    old_published = db.scalars(
        select(Sop).where(
            Sop.doc_id == sop.doc_id,
            Sop.status == SopStatus.published,
            Sop.id != sop.id,
        )
    ).all()
    for old in old_published:
        old.status = SopStatus.superseded

    sop.status = SopStatus.published
    sop.published_at = utcnow()
    sop.review_comment = body.comment
    sop.reviewed_by = reviewer.id
    sop.reviewed_at = utcnow()
    record_audit(
        db,
        reviewer,
        "sop.publish",
        "sop",
        sop.id,
        {"doc_id": sop.doc_id, "version": sop.version, "superseded": [o.id for o in old_published]},
    )
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/return", response_model=SopOut)
def return_sop(
    sop_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> SopOut:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if sop.status != SopStatus.submitted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有待审核状态可退回")
    if not body.comment.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "退回必须填写审核意见")
    sop.status = SopStatus.returned
    sop.review_comment = body.comment
    sop.reviewed_by = reviewer.id
    sop.reviewed_at = utcnow()
    record_audit(db, reviewer, "sop.return", "sop", sop.id, {"comment": body.comment[:200]})
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/deprecate", response_model=SopOut)
def deprecate_sop(
    sop_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> SopOut:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if sop.status not in (SopStatus.published, SopStatus.superseded):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有已发布/历史版本可废弃")
    sop.status = SopStatus.deprecated
    sop.review_comment = body.comment
    record_audit(db, reviewer, "sop.deprecate", "sop", sop.id, {"comment": body.comment[:200]})
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/new-version", response_model=SopOut, status_code=status.HTTP_201_CREATED)
def new_sop_version(
    sop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    """修改已发布 SOP:产生新草稿版本;旧版本保持有效直至新版本通过审核发布。"""
    src = db.get(Sop, sop_id)
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if src.status not in (SopStatus.published, SopStatus.superseded):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有已发布/历史版本可派生新版本")

    open_draft = db.scalar(
        select(Sop).where(
            Sop.doc_id == src.doc_id,
            Sop.status.in_([SopStatus.draft, SopStatus.submitted, SopStatus.returned]),
        )
    )
    if open_draft:
        raise HTTPException(status.HTTP_409_CONFLICT, f"该文档已有未发布的新版本(#{open_draft.version}),请先处理")

    max_version = max(
        s.version
        for s in db.scalars(select(Sop).where(Sop.doc_id == src.doc_id)).all()
    )
    sop = Sop(
        doc_id=src.doc_id,
        version=max_version + 1,
        title=src.title,
        category_id=src.category_id,
        tags=list(src.tags or []),
        applicable_env=src.applicable_env,
        symptom=src.symptom,
        preconditions=src.preconditions,
        check_steps=src.check_steps,
        handling_steps=src.handling_steps,
        recovery_verification=src.recovery_verification,
        rollback_steps=src.rollback_steps,
        notes=src.notes,
        source_case_id=src.source_case_id,
        maintainer_id=user.id,
        status=SopStatus.draft,
        created_by=user.id,
    )
    db.add(sop)
    db.flush()
    record_audit(db, user, "sop.new_version", "sop", sop.id, {"from_version": src.version})
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/feedback", response_model=SopFeedbackOut, status_code=status.HTTP_201_CREATED)
def sop_feedback(
    sop_id: int,
    body: SopFeedbackCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SopFeedbackOut:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if not _can_view_sop(user, sop):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该 SOP")
    fb = SopFeedback(sop_id=sop.id, kind=body.kind, comment=body.comment, created_by=user.id)
    db.add(fb)
    db.flush()
    record_audit(db, user, "sop.feedback", "sop", sop.id, {"kind": body.kind.value})
    db.commit()
    return SopFeedbackOut.model_validate(fb)


@router.get("/{sop_id}/feedback", response_model=list[SopFeedbackOut])
def list_sop_feedback(
    sop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SopFeedbackOut]:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if not _can_view_sop(user, sop):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该 SOP")
    rows = db.scalars(
        select(SopFeedback).where(SopFeedback.sop_id == sop_id).order_by(SopFeedback.id.desc())
    ).all()
    return [SopFeedbackOut.model_validate(f) for f in rows]
