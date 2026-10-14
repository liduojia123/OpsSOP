"""修复「绕过人工确认」产生的异常已解决故障(P1 #2 的存量数据修复路径)。

异常特征:status=resolved 但人工确认字段(实际根因/处理步骤/恢复验证)为空白,
或没有对应案例。此类记录由历史缺陷(创建时直接传 status=resolved)产生,
无法继续确认解决(409)也无法编辑(400),处于死状态。

处理方式:重置回 open(处理中),清空确认字段,保留原始证据与记录,
写审计事件 fault.repair_fake_resolved,之后可走正常人工确认解决流程。

在后端容器内执行(默认只报告不修改,加 --apply 才执行):
  docker compose exec backend python scripts/repair_fake_resolved.py
  docker compose exec backend python scripts/repair_fake_resolved.py --apply
"""

import argparse
import sys

from sqlalchemy import select

sys.path.insert(0, "/srv/opssop")

from app.database import SessionLocal  # noqa: E402
from app.models import Case, Fault, FaultStatus  # noqa: E402


def find_fake_resolved(db) -> list[Fault]:
    rows = db.scalars(select(Fault).where(Fault.status == FaultStatus.resolved)).all()
    result = []
    for fault in rows:
        blank_confirm = not (
            fault.actual_root_cause.strip()
            and fault.handling_steps.strip()
            and fault.recovery_verification.strip()
        )
        has_case = (
            db.scalar(select(Case.id).where(Case.fault_id == fault.id).limit(1)) is not None
        )
        if blank_confirm or not has_case:
            result.append(fault)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="修复绕过人工确认产生的假已解决故障")
    parser.add_argument("--apply", action="store_true", help="实际执行修复(默认仅报告)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        faults = find_fake_resolved(db)
        if not faults:
            print("未发现异常的已解决故障。")
            return 0
        print(f"发现 {len(faults)} 条异常的已解决故障:")
        for fault in faults:
            print(f"  - id={fault.id} title={fault.title!r} created_by={fault.created_by}")
        if not args.apply:
            print("仅报告。加 --apply 执行修复(重置为处理中,可重新人工确认解决)。")
            return 0
        from app.models import AuditEvent

        for fault in faults:
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
                    detail={"reason": "created as resolved without manual confirmation"},
                )
            )
        db.commit()
        print(f"已修复 {len(faults)} 条记录(重置为处理中),请走正常人工确认解决流程。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
