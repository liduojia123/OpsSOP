import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import can_view_sop as _can_view_sop
from app.database import get_db
from app.concurrency import advance_version, require_current_version
from app.deps import get_current_user, record_audit, require_operator, require_reviewer
from app.models import (
    Case,
    CaseStatus,
    FeedbackKind,
    Role,
    Sop,
    SopFeedback,
    SopStatus,
    User,
    utcnow,
)
from app.notifications import notify, notify_reviewers
from app.schemas import (
    LinkedCaseOut,
    ReviewRequest,
    SopCreate,
    SopDetailOut,
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


def _lock_sop_document(db: Session, sop_id: int) -> Sop:
    doc_id = db.scalar(select(Sop.doc_id).where(Sop.id == sop_id))
    if doc_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    rows = db.scalars(select(Sop).where(Sop.doc_id == doc_id).order_by(Sop.id).with_for_update().execution_options(populate_existing=True)).all()
    return next(s for s in rows if s.id == sop_id)


def _get_editable_sop(db: Session, sop_id: int, user: User, expected: str | None) -> Sop:
    sop = _lock_sop_document(db, sop_id)
    if user.role == Role.operator and sop.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能编辑自己创建的 SOP")
    require_current_version(expected, sop.updated_at)
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
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = Query(default=None, description="标题/现象/步骤关键词"),
    status_filter: SopStatus | None = Query(default=None, alias="status"),
    category_id: int | None = None,
    doc_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=1000, description="最多返回条数"),
    offset: int = Query(default=0, ge=0, description="跳过条数(配合 X-Total-Count 翻页)"),
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
    # X-Total-Count 为过滤后的总数;返回体仍是列表,超过 limit 的部分需按 offset 翻页
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    response.headers["X-Total-Count"] = str(total)
    stmt = stmt.order_by(Sop.updated_at.desc()).offset(offset).limit(limit)
    return [SopOut.model_validate(s) for s in db.scalars(stmt).all()]


@router.get("/{sop_id}", response_model=SopDetailOut)
def get_sop(
    sop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SopDetailOut:
    sop = db.get(Sop, sop_id)
    if sop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SOP 不存在")
    if not _can_view_sop(user, sop):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该 SOP")
    out = SopDetailOut.model_validate(sop)
    if sop.source_case_id:
        case = db.get(Case, sop.source_case_id)
        # 来源案例按案例页权限裁剪:运维只能看到自己创建或已通过的案例
        if case is not None and (
            user.role != Role.operator
            or case.created_by == user.id
            or case.status == CaseStatus.approved
        ):
            out.source_case = LinkedCaseOut.model_validate(case)
    return out


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
    expected: str | None = Header(default=None, alias="X-Expected-Updated-At"),
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    sop = _get_editable_sop(db, sop_id, user, expected)
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(sop, key, value)
    advance_version(sop)
    record_audit(db, user, "sop.update", "sop", sop.id, {"fields": list(data.keys())})
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/submit", response_model=SopOut)
def submit_sop(
    sop_id: int,
    expected: str | None = Header(default=None, alias="X-Expected-Updated-At"),
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    sop = _get_editable_sop(db, sop_id, user, expected)
    missing = _missing_sop_content(sop)
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"缺少必填内容: {', '.join(missing)}")
    sop.status = SopStatus.submitted
    advance_version(sop)
    record_audit(db, user, "sop.submit", "sop", sop.id)
    notify_reviewers(
        db,
        kind="sop.submitted",
        title=f"SOP #{sop.id} v{sop.version} 待审核：{sop.title}",
        body="SOP 等待发布审核，请确认排查/处理/恢复步骤完整。",
        link=f"/sops/{sop.id}",
        entity_type="sop",
        entity_id=sop.id,
    )
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/approve", response_model=SopOut)
def approve_sop(
    sop_id: int,
    body: ReviewRequest,
    expected: str | None = Header(default=None, alias="X-Expected-Updated-At"),
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> SopOut:
    """发布:只允许从待审核(submitted)发布;退回状态须作者修改后重新提交。
    同一 doc_id 旧的已发布版本自动转为历史版本;新版本在此之前不生效。"""
    sop = _lock_sop_document(db, sop_id)
    require_current_version(expected, sop.updated_at)
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
        advance_version(old)

    sop.status = SopStatus.published
    advance_version(sop)
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
    notify(
        db,
        [sop.created_by, sop.maintainer_id],
        kind="sop.published",
        title=f"SOP #{sop.id} v{sop.version} 已发布：{sop.title}",
        body=body.comment[:500] or "SOP 已发布，可被检索与引用。",
        link=f"/sops/{sop.id}",
        entity_type="sop",
        entity_id=sop.id,
    )
    # 回写来源案例/故障:知识资产发布后在来源时间线上可追溯,并通知案例作者
    if sop.source_case_id:
        source_case = db.get(Case, sop.source_case_id)
        if source_case is not None:
            record_audit(
                db,
                reviewer,
                "case.sop_published",
                "case",
                source_case.id,
                {"sop_id": sop.id, "version": sop.version},
            )
            record_audit(
                db,
                reviewer,
                "fault.sop_published",
                "fault",
                source_case.fault_id,
                {"sop_id": sop.id, "version": sop.version, "case_id": source_case.id},
            )
            notify(
                db,
                [source_case.created_by],
                kind="case.sop_published",
                title=f"案例 #{source_case.id} 衍生的 SOP 已发布",
                body=f"SOP #{sop.id} v{sop.version}：{sop.title}",
                link=f"/sops/{sop.id}",
                entity_type="case",
                entity_id=source_case.id,
            )
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/return", response_model=SopOut)
def return_sop(
    sop_id: int,
    body: ReviewRequest,
    expected: str | None = Header(default=None, alias="X-Expected-Updated-At"),
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> SopOut:
    sop = _lock_sop_document(db, sop_id)
    require_current_version(expected, sop.updated_at)
    if sop.status != SopStatus.submitted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有待审核状态可退回")
    if not body.comment.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "退回必须填写审核意见")
    sop.status = SopStatus.returned
    advance_version(sop)
    sop.review_comment = body.comment
    sop.reviewed_by = reviewer.id
    sop.reviewed_at = utcnow()
    record_audit(db, reviewer, "sop.return", "sop", sop.id, {"comment": body.comment[:200]})
    notify(
        db,
        [sop.created_by],
        kind="sop.returned",
        title=f"SOP #{sop.id} v{sop.version} 被退回：{sop.title}",
        body=f"审核意见：{body.comment[:500]}",
        link=f"/sops/{sop.id}",
        entity_type="sop",
        entity_id=sop.id,
    )
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/deprecate", response_model=SopOut)
def deprecate_sop(
    sop_id: int,
    body: ReviewRequest,
    expected: str | None = Header(default=None, alias="X-Expected-Updated-At"),
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_reviewer),
) -> SopOut:
    sop = _lock_sop_document(db, sop_id)
    require_current_version(expected, sop.updated_at)
    if sop.status not in (SopStatus.published, SopStatus.superseded):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有已发布/历史版本可废弃")
    sop.status = SopStatus.deprecated
    advance_version(sop)
    sop.review_comment = body.comment
    record_audit(db, reviewer, "sop.deprecate", "sop", sop.id, {"comment": body.comment[:200]})
    notify(
        db,
        [sop.created_by, sop.maintainer_id],
        kind="sop.deprecated",
        title=f"SOP #{sop.id} v{sop.version} 已废弃：{sop.title}",
        body=body.comment[:500] or "该 SOP 已废弃，不再作为有效知识引用。",
        link=f"/sops/{sop.id}",
        entity_type="sop",
        entity_id=sop.id,
    )
    db.commit()
    return SopOut.model_validate(sop)


@router.post("/{sop_id}/new-version", response_model=SopOut, status_code=status.HTTP_201_CREATED)
def new_sop_version(
    sop_id: int,
    expected: str | None = Header(default=None, alias="X-Expected-Updated-At"),
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    """修改已发布 SOP:产生新草稿版本;旧版本保持有效直至新版本通过审核发布。"""
    src = _lock_sop_document(db, sop_id)
    if not _can_view_sop(user, src):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该 SOP")
    require_current_version(expected, src.updated_at)
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
