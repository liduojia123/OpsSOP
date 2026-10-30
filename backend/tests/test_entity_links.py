"""实体链路:故障 ↔ 案例 ↔ SOP 的关联摘要、权限裁剪与工具读取。"""

from uuid import uuid4

from app.models import (
    Case,
    CaseStatus,
    Fault,
    FaultRecord,
    FaultStatus,
    RecordKind,
    Role,
    Sop,
    SopStatus,
)
from tests.conftest import make_user


def _fault(db, user, title="磁盘写满", status=FaultStatus.open) -> Fault:
    row = Fault(title=title, description="根分区 100%", created_by=user.id, status=status)
    db.add(row)
    db.commit()
    return row


def _case(db, fault, user, status=CaseStatus.pending_review) -> Case:
    row = Case(fault_id=fault.id, title=fault.title, status=status, created_by=user.id)
    db.add(row)
    db.commit()
    return row


def _sop(db, case, user, status=SopStatus.draft, title="磁盘处置 SOP") -> Sop:
    row = Sop(
        doc_id=str(uuid4()),
        version=1,
        title=title,
        status=status,
        source_case_id=case.id,
        created_by=user.id,
        tags=[],
    )
    db.add(row)
    db.commit()
    return row


def test_fault_detail_includes_related_case(client, db, operator, op_headers):
    fault = _fault(db, operator)
    case = _case(db, fault, operator)

    detail = client.get(f"/api/faults/{fault.id}", headers=op_headers).json()
    assert detail["related_case"]["id"] == case.id
    assert detail["related_case"]["title"] == fault.title
    assert detail["related_case"]["status"] == "pending_review"


def test_resolve_returns_case_link(client, db, operator, op_headers):
    fault = _fault(db, operator)
    resp = client.post(
        f"/api/faults/{fault.id}/resolve",
        json={
            "actual_root_cause": "日志占满根分区",
            "handling_steps": "清理旧日志",
            "recovery_verification": "df -h 恢复至 60%",
        },
        headers=op_headers,
    )
    assert resp.status_code == 200, resp.text
    related = resp.json()["related_case"]
    assert related["id"] > 0
    assert related["status"] == "pending_review"

    # 重新读取详情也保持一致
    detail = client.get(f"/api/faults/{fault.id}", headers=op_headers).json()
    assert detail["related_case"]["id"] == related["id"]


def test_case_detail_links_source_fault_and_sops(client, db, operator, op_headers):
    fault = _fault(db, operator)
    case = _case(db, fault, operator)
    sop = _sop(db, case, operator, status=SopStatus.published)

    detail = client.get(f"/api/cases/{case.id}", headers=op_headers).json()
    assert detail["source_fault"]["id"] == fault.id
    assert detail["source_fault"]["title"] == fault.title
    assert [row["id"] for row in detail["related_sops"]] == [sop.id]
    assert detail["related_sops"][0]["status"] == "published"


def test_sop_detail_links_source_case(client, db, operator, op_headers):
    fault = _fault(db, operator)
    case = _case(db, fault, operator)
    sop = _sop(db, case, operator)

    detail = client.get(f"/api/sops/{sop.id}", headers=op_headers).json()
    assert detail["source_case"]["id"] == case.id
    assert detail["source_case"]["title"] == case.title


def test_case_detail_trims_unviewable_fault_and_draft_sop(client, db, op_headers):
    # 另一位运维的已通过案例:本人可见案例,但来源故障与未发布 SOP 仍不可见
    other = make_user(db, "entity_other", Role.operator)
    other_fault = _fault(db, other, title="他人的故障")
    other_case = _case(db, other_fault, other, status=CaseStatus.approved)
    draft = _sop(db, other_case, other, status=SopStatus.draft, title="他人草稿")
    published = _sop(db, other_case, other, status=SopStatus.published, title="已发布 SOP")

    detail = client.get(f"/api/cases/{other_case.id}", headers=op_headers).json()
    assert detail["source_fault"] is None
    ids = [row["id"] for row in detail["related_sops"]]
    assert published.id in ids
    assert draft.id not in ids


def test_get_fault_tool_returns_detail_records_and_links(db, operator, reviewer):
    from app.ai.tools import ToolExecutor
    from app.config import Settings

    fault = _fault(db, operator)
    db.add(
        FaultRecord(
            fault_id=fault.id,
            kind=RecordKind.check_result,
            content="df -h 显示 / 使用率 100%",
            created_by=operator.id,
        )
    )
    db.commit()
    case = _case(db, fault, operator)
    sop = _sop(db, case, operator, status=SopStatus.published)

    settings = Settings(
        secret_key="unit-test-secret-key-0123456789",
        ai_mode="cloud",
        ai_base_url="https://api.example.com/v1",
        ai_model="test-model",
    )
    executor = ToolExecutor(db=db, user=operator, settings=settings, conversation_id=1)
    outcome = executor.execute("get_fault", {"fault_id": fault.id})
    assert outcome.status == "ok"
    assert fault.title in outcome.content
    assert "根分区 100%" in outcome.content
    assert "df -h 显示" in outcome.content
    assert f"链接=/faults/{fault.id}" in outcome.content
    assert f"链接=/cases/{case.id}" in outcome.content
    assert f"链接=/sops/{sop.id}" in outcome.content

    # 无权查看他人故障:工具直接拒绝,不泄露内容
    reviewer_executor = ToolExecutor(
        db=db, user=reviewer, settings=settings, conversation_id=1
    )
    assert reviewer_executor.execute("get_fault", {"fault_id": fault.id}).status == "ok"

    stranger = make_user(db, "entity_stranger", Role.operator)
    denied = ToolExecutor(
        db=db, user=stranger, settings=settings, conversation_id=1
    ).execute("get_fault", {"fault_id": fault.id})
    assert denied.status == "denied"
    assert "无权查看" in denied.content