"""实体可见性规则(单点定义,列表/详情/时间线/AI 工具共用)。

- 故障:运维只看本人,审核/管理员看全部;
- 案例:运维可看本人创建或已通过;审核/管理员看全部;
- SOP:运维可看本人创建或已正式发布(含历史/废弃);审核/管理员看全部。
放宽任何一条都会造成越权,改动前需同步检查查询过滤。
"""

from __future__ import annotations

from app.models import Case, CaseStatus, Fault, Role, Sop, SopStatus, User

OFFICIAL_SOP_STATUSES = (SopStatus.published, SopStatus.superseded, SopStatus.deprecated)
OFFICIAL_CASE_STATUSES = (CaseStatus.approved,)


def can_view_fault(user: User, fault: Fault) -> bool:
    if user.role == Role.operator:
        return fault.created_by == user.id
    return True


def can_view_case(user: User, case: Case) -> bool:
    if user.role == Role.operator:
        return case.created_by == user.id or case.status in OFFICIAL_CASE_STATUSES
    return True


def can_view_sop(user: User, sop: Sop) -> bool:
    if user.role == Role.operator:
        return sop.created_by == user.id or sop.status in OFFICIAL_SOP_STATUSES
    return True