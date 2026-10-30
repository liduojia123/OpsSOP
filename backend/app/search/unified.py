"""统一检索:故障、案例、SOP 的跨实体搜索。

权限与各列表页一致,不在检索层放宽:
- 运维:故障仅本人;案例 = 本人创建或已通过;SOP = 本人创建或已发布/历史/废弃;
- 审核/管理员:全部可见。
只匹配标题、描述/症状、服务/适用环境等摘要字段,不返回日志正文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Case,
    CaseStatus,
    Fault,
    MonitoredServer,
    Role,
    Sop,
    SopStatus,
    User,
)

MAX_QUERY_CHARS = 100
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
TYPES = ("fault", "case", "sop", "server")


@dataclass
class UnifiedHit:
    type: str
    id: int
    title: str
    status: str
    subtitle: str
    link: str
    updated_at: datetime


@dataclass
class UnifiedGroup:
    type: str
    total: int
    items: list[UnifiedHit] = field(default_factory=list)
    truncated: bool = False


def _status_text(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _subtitle(*parts: str, limit: int = 70) -> str:
    return " · ".join(part.strip() for part in parts if part and part.strip())[:limit]


def _group(
    db: Session,
    type_name: str,
    stmt,
    order_col,
    limit: int,
    build,
) -> UnifiedGroup:
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(order_col.desc()).limit(limit)))
    items = [build(row) for row in rows]
    return UnifiedGroup(type=type_name, total=total, items=items, truncated=total > len(items))


def search_all(
    db: Session,
    user: User,
    query: str,
    types: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[UnifiedGroup]:
    query = (query or "").strip()[:MAX_QUERY_CHARS]
    selected = [name for name in (types or TYPES) if name in TYPES] or list(TYPES)
    limit = max(1, min(int(limit), MAX_LIMIT))
    if not query:
        return [UnifiedGroup(type=name, total=0) for name in selected]

    like = f"%{query}%"
    groups: list[UnifiedGroup] = []

    if "fault" in selected:
        stmt = select(Fault).where(
            or_(
                Fault.title.ilike(like),
                Fault.description.ilike(like),
                Fault.service.ilike(like),
            )
        )
        if user.role == Role.operator:
            stmt = stmt.where(Fault.created_by == user.id)
        groups.append(
            _group(
                db,
                "fault",
                stmt,
                Fault.updated_at,
                limit,
                lambda row: UnifiedHit(
                    type="fault",
                    id=row.id,
                    title=row.title,
                    status=_status_text(row.status),
                    subtitle=_subtitle(row.service, row.environment),
                    link=f"/faults/{row.id}",
                    updated_at=row.updated_at,
                ),
            )
        )

    if "case" in selected:
        stmt = select(Case).where(
            or_(
                Case.title.ilike(like),
                Case.symptom.ilike(like),
                Case.service.ilike(like),
            )
        )
        if user.role == Role.operator:
            stmt = stmt.where(
                or_(Case.created_by == user.id, Case.status == CaseStatus.approved)
            )
        groups.append(
            _group(
                db,
                "case",
                stmt,
                Case.updated_at,
                limit,
                lambda row: UnifiedHit(
                    type="case",
                    id=row.id,
                    title=row.title,
                    status=_status_text(row.status),
                    subtitle=_subtitle(row.symptom or row.service),
                    link=f"/cases/{row.id}",
                    updated_at=row.updated_at,
                ),
            )
        )

    if "sop" in selected:
        stmt = select(Sop).where(
            or_(
                Sop.title.ilike(like),
                Sop.symptom.ilike(like),
                Sop.applicable_env.ilike(like),
            )
        )
        if user.role == Role.operator:
            stmt = stmt.where(
                or_(
                    Sop.created_by == user.id,
                    Sop.status.in_(
                        [SopStatus.published, SopStatus.superseded, SopStatus.deprecated]
                    ),
                )
            )
        groups.append(
            _group(
                db,
                "sop",
                stmt,
                Sop.updated_at,
                limit,
                lambda row: UnifiedHit(
                    type="sop",
                    id=row.id,
                    title=row.title,
                    status=_status_text(row.status),
                    subtitle=_subtitle(f"v{row.version}", row.symptom),
                    link=f"/sops/{row.id}",
                    updated_at=row.updated_at,
                ),
            )
        )

    if "server" in selected:
        stmt = select(MonitoredServer).where(
            or_(
                MonitoredServer.name.ilike(like),
                MonitoredServer.host.ilike(like),
                MonitoredServer.notes.ilike(like),
                cast(MonitoredServer.service_names, Text).ilike(like),
            )
        )
        if user.role == Role.operator:
            stmt = stmt.where(MonitoredServer.enabled.is_(True))
        groups.append(
            _group(
                db,
                "server",
                stmt,
                MonitoredServer.updated_at,
                limit,
                lambda row: UnifiedHit(
                    type="server",
                    id=row.id,
                    title=row.name,
                    status="enabled" if row.enabled else "disabled",
                    subtitle=_subtitle(
                        row.host,
                        "、".join(row.service_names or []),
                        row.prometheus_instance,
                    ),
                    link="/monitoring",
                    updated_at=row.updated_at,
                ),
            )
        )

    return groups