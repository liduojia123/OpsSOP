"""修复「绕过人工确认」产生的异常已解决故障(P1 #2 的存量数据修复路径)。

异常特征:status=resolved 但人工确认字段(实际根因/处理步骤/恢复验证)为空白,
或没有对应案例。此类记录由历史缺陷(创建时直接传 status=resolved)产生,
无法继续确认解决(409)也无法编辑(400),处于死状态。

处理方式(保留既有业务数据,不删除关联记录):
- 故障:重置回 open(处理中),清空确认字段,保留原始证据与追加记录;
- 既有案例:不删除、不改 ID(SOP.source_case_id 等引用关系保持有效)。
  - 案例人工确认内容同样空白:转为「退回」并写明修复原因,不再作为正式诊断依据;
  - 案例内容完整:保持原状态不动,仅在报告中列出;
- 写审计事件 fault.repair_fake_resolved / case.repair_demote,之后走正常人工确认
  解决流程。重新确认解决时后端原地更新既有案例并重新送审(fault.resolve_case_updated),
  不会触发案例唯一约束错误。

在后端容器内执行(默认只报告不修改,加 --apply 才执行):
  docker compose exec backend python scripts/repair_fake_resolved.py
  docker compose exec backend python scripts/repair_fake_resolved.py --apply
"""

import argparse
import sys

from sqlalchemy import select

sys.path.insert(0, "/srv/opssop")

from app.database import SessionLocal  # noqa: E402
from app.concurrency import advance_version  # noqa: E402
from app.models import AuditEvent, Case, CaseStatus, Fault, FaultStatus  # noqa: E402


def _blank(value: str) -> bool:
    return not value.strip()


def find_fake_resolved(db) -> list[tuple[Fault, Case | None]]:
    rows = db.scalars(select(Fault).where(Fault.status == FaultStatus.resolved)).all()
    result: list[tuple[Fault, Case | None]] = []
    for fault in rows:
        case = db.scalar(select(Case).where(Case.fault_id == fault.id))
        blank_confirm = (
            _blank(fault.actual_root_cause)
            and _blank(fault.handling_steps)
            and _blank(fault.recovery_verification)
        )
        if blank_confirm or case is None:
            result.append((fault, case))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="修复绕过人工确认产生的假已解决故障")
    parser.add_argument("--apply", action="store_true", help="实际执行修复(默认仅报告)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        found = find_fake_resolved(db)
        if not found:
            print("未发现异常的已解决故障。")
            return 0
        print(f"发现 {len(found)} 条异常的已解决故障:")
        for fault, case in found:
            case_note = f" case={case.id}(status={case.status.value})" if case else " 无关联案例"
            print(f"  - id={fault.id} title={fault.title!r} created_by={fault.created_by}{case_note}")
        if not args.apply:
            print("仅报告。加 --apply 执行修复(重置为处理中,可重新人工确认解决)。")
            return 0

        repaired = 0
        for fault, case in found:
            # 与正常故障确认按相同顺序持锁，重新核对，避免覆盖并发中的人工修复。
            fault = db.scalar(select(Fault).where(Fault.id == fault.id).with_for_update().execution_options(populate_existing=True))
            if fault is None or fault.status != FaultStatus.resolved:
                continue
            case = db.scalar(select(Case).where(Case.fault_id == fault.id).with_for_update().execution_options(populate_existing=True))
            blank_confirm = _blank(fault.actual_root_cause) and _blank(fault.handling_steps) and _blank(fault.recovery_verification)
            if not blank_confirm and case is not None:
                continue
            fault.status = FaultStatus.open
            fault.actual_root_cause = ""
            fault.handling_steps = ""
            fault.recovery_verification = ""
            fault.resolved_at = None
            db.add(
                AuditEvent(
                    actor_id=None,
                    action="fault.repair_fake_resolved",
                    target_type="fault",
                    target_id=str(fault.id),
                    detail={
                        "reason": "created as resolved without manual confirmation",
                        "case_id": case.id if case else None,
                    },
                )
            )
            if case is not None:
                case_blank = (
                    _blank(case.confirmed_root_cause)
                    and _blank(case.handling_steps)
                    and _blank(case.recovery_verification)
                )
                if case_blank:
                    # 内容空白的案例不再是正式依据;保留记录与引用,退回待重新确认
                    case.status = CaseStatus.returned
                    advance_version(case)
                    case.review_comment = (
                        "历史异常数据修复:关联故障绕过人工确认,案例内容为空白,"
                        "已退回。重新确认解决后将原地更新并重新送审。"
                    )
                    db.add(
                        AuditEvent(
                            actor_id=None,
                            action="case.repair_demote",
                            target_type="case",
                            target_id=str(case.id),
                            detail={"fault_id": fault.id, "reason": "blank confirmation content"},
                        )
                    )
            repaired += 1
        db.commit()
        print(
            f"已修复 {repaired} 条记录(重置为处理中)。"
            "有关联案例的故障重新确认解决时将原地更新既有案例并重新送审;"
            "请走正常人工确认解决流程。"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
