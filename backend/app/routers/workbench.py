from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import (
    Case,
    CaseStatus,
    Category,
    Fault,
    FaultStatus,
    Role,
    Sop,
    SopStatus,
    User,
)
from app.schemas import CaseOut, CategoryStat, FaultOut, SopOut, WorkbenchOut

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


@router.get("", response_model=WorkbenchOut)
def workbench(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WorkbenchOut:
    # 本人待处理故障
    my_faults = db.scalars(
        select(Fault)
        .where(Fault.created_by == user.id, Fault.status.in_([FaultStatus.draft, FaultStatus.open]))
        .order_by(Fault.updated_at.desc())
    ).all()

    # 待审核任务(仅审核/管理员)
    if user.role in (Role.reviewer, Role.admin):
        pending_cases = db.scalars(
            select(Case).where(Case.status == CaseStatus.pending_review).order_by(Case.updated_at)
        ).all()
        pending_sops = db.scalars(
            select(Sop).where(Sop.status == SopStatus.submitted).order_by(Sop.updated_at)
        ).all()
    else:
        pending_cases = []
        pending_sops = []

    # 我被退回的材料
    my_returned_cases = db.scalars(
        select(Case)
        .where(Case.created_by == user.id, Case.status == CaseStatus.returned)
        .order_by(Case.updated_at.desc())
    ).all()
    my_returned_sops = db.scalars(
        select(Sop)
        .where(Sop.created_by == user.id, Sop.status == SopStatus.returned)
        .order_by(Sop.updated_at.desc())
    ).all()

    # 最近更新的有效 SOP(仅取前 8 条展示;总数单独统计)
    recent_sops = db.scalars(
        select(Sop)
        .where(Sop.status == SopStatus.published)
        .order_by(Sop.published_at.desc().nullslast())
        .limit(8)
    ).all()
    published_sop_count = (
        db.scalar(select(func.count(Sop.id)).where(Sop.status == SopStatus.published)) or 0
    )

    # 分类统计(按本人故障,若为审核/管理则按全部故障)
    if user.role in (Role.reviewer, Role.admin):
        stats_rows = db.execute(
            select(Category.id, Category.name, func.count(Fault.id))
            .outerjoin(Fault, Fault.category_id == Category.id)
            .group_by(Category.id, Category.name)
            .order_by(Category.sort_order)
        ).all()
    else:
        stats_rows = db.execute(
            select(Category.id, Category.name, func.count(Fault.id))
            .outerjoin(
                Fault,
                (Fault.category_id == Category.id) & (Fault.created_by == user.id),
            )
            .group_by(Category.id, Category.name)
            .order_by(Category.sort_order)
        ).all()

    return WorkbenchOut(
        my_open_faults=[FaultOut.model_validate(f) for f in my_faults],
        pending_case_reviews=[CaseOut.model_validate(c) for c in pending_cases],
        pending_sop_reviews=[SopOut.model_validate(s) for s in pending_sops],
        recent_sops=[SopOut.model_validate(s) for s in recent_sops],
        published_sop_count=published_sop_count,
        category_stats=[
            CategoryStat(category_id=cid, category_name=name, count=count)
            for cid, name, count in stats_rows
        ],
        my_returned_cases=[CaseOut.model_validate(c) for c in my_returned_cases],
        my_returned_sops=[SopOut.model_validate(s) for s in my_returned_sops],
    )
