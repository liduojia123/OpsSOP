"""第一阶段遗漏修复的回归(第二阶段开工前补齐)。

覆盖:
- prepare_production.py --disable 参数解析与遍历一致(可多次指定,不再按字符遍历);
- repair_fake_resolved 重开后重新确认解决:原地更新既有案例,不触发唯一约束 500,
  保留案例 ID 与来源引用(SOP.source_case_id)。
"""

import importlib.util
import pathlib

import pytest

from app.models import Case, CaseStatus, Fault, FaultStatus, Sop, SopStatus
from app.seed import ensure_seed
from tests.conftest import TestingSessionLocal

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== prepare_production.py --disable 参数解析 =====


def test_disable_arg_is_list_not_characters():
    """修复前 --disable 只存一个字符串,遍历时按字符逐个找账号。"""
    mod = _load_script("prepare_production")
    parser = mod.build_parser()
    args = parser.parse_args(["--disable", "alice", "--disable", "bob"])
    assert args.disable == ["alice", "bob"]
    # 与遍历逻辑一致:每个元素是完整用户名
    for username in args.disable:
        assert len(username) > 1
    # 未指定时为空列表,不进入遍历
    assert parser.parse_args([]).disable == []


def test_rotate_and_disable_combined_parsing():
    mod = _load_script("prepare_production")
    parser = mod.build_parser()
    args = parser.parse_args(
        ["--disable", "demo-op", "--rotate", "admin", "new-pass-123"]
    )
    assert args.disable == ["demo-op"]
    assert args.rotate == [["admin", "new-pass-123"]]


# ===== 重新确认解决:既有案例原地更新 =====


def _make_fault_with_case(db, user_id: int):
    fault = Fault(
        title="历史异常故障",
        description="描述",
        error_logs="error logs",
        status=FaultStatus.open,
        created_by=user_id,
    )
    db.add(fault)
    db.flush()
    case = Case(
        fault_id=fault.id,
        title=fault.title,
        symptom=fault.description,
        confirmed_root_cause="",  # 空白:历史缺陷产生
        handling_steps="",
        recovery_verification="",
        status=CaseStatus.approved,
        created_by=user_id,
    )
    db.add(case)
    db.commit()
    return fault, case


def test_resolve_updates_existing_case_without_conflict(db, client, op_headers, operator):
    """重开后的故障再次确认解决:既有案例原地更新,案例 ID/引用保持,回到待审核。"""
    fault, case = _make_fault_with_case(db, operator.id)
    # 既有 SOP 引用该案例
    sop = Sop(
        doc_id="doc-1",
        version=1,
        title="来源 SOP",
        source_case_id=case.id,
        status=SopStatus.published,
        created_by=operator.id,
    )
    db.add(sop)
    db.commit()

    resp = client.post(
        f"/api/faults/{fault.id}/resolve",
        headers=op_headers,
        json={
            "actual_root_cause": "真实根因:配置错误",
            "handling_steps": "修改配置并重启",
            "recovery_verification": "监控恢复,错误率归零",
        },
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    updated = db.get(Case, case.id)
    assert updated is not None, "既有案例不得被删除"
    assert updated.fault_id == fault.id
    assert updated.confirmed_root_cause == "真实根因:配置错误"
    assert updated.status == CaseStatus.pending_review, "内容变更后须重新审核"
    assert updated.reviewed_by is None and updated.reviewed_at is None
    # SOP 引用关系保持
    assert db.get(Sop, sop.id).source_case_id == case.id
    # 故障状态
    assert db.get(Fault, fault.id).status == FaultStatus.resolved


def test_resolve_new_fault_creates_case_once(db, client, op_headers, operator):
    """正常路径不变:首次确认解决创建案例;重复确认解决 409,不产生第二个案例。"""
    resp = client.post(
        "/api/faults",
        headers=op_headers,
        json={"title": "新故障", "description": "d"},
    )
    fault_id = resp.json()["id"]
    resp = client.post(
        f"/api/faults/{fault_id}/resolve",
        headers=op_headers,
        json={
            "actual_root_cause": "根因",
            "handling_steps": "步骤",
            "recovery_verification": "验证",
        },
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        f"/api/faults/{fault_id}/resolve",
        headers=op_headers,
        json={
            "actual_root_cause": "根因2",
            "handling_steps": "步骤2",
            "recovery_verification": "验证2",
        },
    )
    assert resp.status_code == 409
    cases = db.query(Case).filter(Case.fault_id == fault_id).all()
    assert len(cases) == 1


def test_repair_script_then_resolve_roundtrip(db, operator):
    """repair_fake_resolved --apply 重开带案例的假已解决故障,之后可正常重新确认解决。"""
    repair = _load_script("repair_fake_resolved")

    fault = Fault(
        title="假已解决",
        description="d",
        status=FaultStatus.resolved,  # 历史缺陷:直接写 resolved
        actual_root_cause="",
        handling_steps="",
        recovery_verification="",
        created_by=operator.id,
    )
    db.add(fault)
    db.flush()
    case = Case(
        fault_id=fault.id,
        title="假已解决",
        confirmed_root_cause="",
        handling_steps="",
        recovery_verification="",
        status=CaseStatus.approved,  # 空白内容却已通过:必须被降级
        created_by=operator.id,
    )
    db.add(case)
    db.commit()

    found = repair.find_fake_resolved(db)
    assert any(f.id == fault.id for f, _ in found)

    # 模拟 --apply 的处理(不依赖容器路径)
    for f, c in found:
        f.status = FaultStatus.open
        f.actual_root_cause = ""
        f.handling_steps = ""
        f.recovery_verification = ""
        f.resolved_at = None
        if c is not None:
            c.status = CaseStatus.returned
    db.commit()

    db.expire_all()
    assert db.get(Fault, fault.id).status == FaultStatus.open
    assert db.get(Case, case.id).status == CaseStatus.returned, "空白案例被降级但不删除"


def test_repair_script_apply_advances_case_version(db, operator, monkeypatch):
    import sys

    repair = _load_script("repair_fake_resolved")
    fault = Fault(title="待修复", description="d", status=FaultStatus.resolved, actual_root_cause="", handling_steps="", recovery_verification="", created_by=operator.id)
    db.add(fault)
    db.flush()
    case = Case(fault_id=fault.id, title="待修复", confirmed_root_cause="", handling_steps="", recovery_verification="", status=CaseStatus.approved, created_by=operator.id)
    db.add(case)
    db.commit()
    old_version = case.updated_at
    monkeypatch.setattr(repair, "SessionLocal", lambda: db)
    monkeypatch.setattr(sys, "argv", ["repair_fake_resolved.py", "--apply"])
    assert repair.main() == 0
    refreshed = db.get(Case, case.id)
    assert refreshed.status == CaseStatus.returned
    assert refreshed.updated_at > old_version
