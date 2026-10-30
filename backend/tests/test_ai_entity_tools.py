"""案例/SOP 详情工具与统一检索工具:权限裁剪、链接与类型筛选。"""

from uuid import uuid4

from app.ai.tools import ToolExecutor
from app.config import Settings
from app.models import (
    Case,
    CaseStatus,
    Fault,
    FaultStatus,
    MonitoredServer,
    Role,
    Sop,
    SopStatus,
)
from tests.conftest import make_user


def _settings() -> Settings:
    return Settings(
        secret_key="unit-test-secret-key-0123456789",
        ai_mode="cloud",
        ai_base_url="https://api.example.com/v1",
        ai_model="test-model",
    )


def _executor(db, user) -> ToolExecutor:
    return ToolExecutor(db=db, user=user, settings=_settings(), conversation_id=1)


def _fixtures(db, owner, other):
    own_fault = Fault(
        title="订单服务 504", service="order-service", status=FaultStatus.open, created_by=owner.id
    )
    other_fault = Fault(
        title="支付服务超时", service="payment-service", status=FaultStatus.open, created_by=other.id
    )
    db.add_all([own_fault, other_fault])
    db.flush()
    own_case = Case(
        fault_id=own_fault.id,
        title="订单超时案例",
        service="order-service",
        confirmed_root_cause="线程池耗尽",
        handling_steps="扩容",
        recovery_verification="错误率归零",
        status=CaseStatus.pending_review,
        created_by=owner.id,
    )
    approved_case = Case(
        fault_id=other_fault.id,
        title="支付超时已审案例",
        service="payment-service",
        confirmed_root_cause="下游限流",
        handling_steps="调整限流阈值",
        recovery_verification="成功率恢复",
        status=CaseStatus.approved,
        created_by=other.id,
    )
    db.add_all([own_case, approved_case])
    db.flush()
    own_sop = Sop(
        doc_id=str(uuid4()),
        version=1,
        title="订单超时草稿",
        symptom="504",
        check_steps="检查线程池",
        handling_steps="扩容",
        recovery_verification="观察错误率",
        status=SopStatus.draft,
        created_by=owner.id,
    )
    published_sop = Sop(
        doc_id=str(uuid4()),
        version=1,
        title="支付超时处理规范",
        symptom="支付超时",
        check_steps="检查限流",
        handling_steps="调整阈值",
        recovery_verification="成功率恢复",
        status=SopStatus.published,
        created_by=other.id,
    )
    db.add_all([own_sop, published_sop])
    db.add(
        MonitoredServer(
            name="order-node-1",
            host="10.0.0.7",
            username="ops",
            service_names=["order-service"],
        )
    )
    db.commit()
    return own_fault, other_fault, own_case, approved_case, own_sop, published_sop


def test_specs_include_entity_tools(db, operator):
    names = {spec["function"]["name"] for spec in _executor(db, operator).specs()}
    assert {"get_case", "get_sop", "search_all"} <= names


def test_get_case_and_sop_respect_visibility(db, operator):
    other = make_user(db, "op_entity_other", Role.operator)
    _, _, own_case, approved_case, own_sop, published_sop = _fixtures(db, operator, other)
    executor = _executor(db, operator)

    own = executor.execute("get_case", {"case_id": own_case.id})
    assert own.status == "ok"
    assert "人工确认根因: 线程池耗尽" in own.content
    assert f"链接=/cases/{own_case.id}" in own.content

    # 已通过案例对运维可见,即使不是创建者
    approved = executor.execute("get_case", {"case_id": approved_case.id})
    assert approved.status == "ok"
    assert approved_case.title in approved.content

    # 他人的待审案例、草稿 SOP 不可见
    other_pending = Case(
        fault_id=_fixtures_fault(db, other),
        title="他人待审案例",
        status=CaseStatus.pending_review,
        created_by=other.id,
    )
    db.add(other_pending)
    db.commit()
    denied = executor.execute("get_case", {"case_id": other_pending.id})
    assert denied.status == "denied"
    assert "无权" in denied.content

    own_draft = executor.execute("get_sop", {"sop_id": own_sop.id})
    assert own_draft.status == "ok" and "订单超时草稿" in own_draft.content
    published = executor.execute("get_sop", {"sop_id": published_sop.id})
    assert published.status == "ok" and "支付超时处理规范" in published.content

    denied_sop = executor.execute(
        "get_sop",
        {"sop_id": _hidden_sop(db, other).id},
    )
    assert denied_sop.status == "denied"


def _fixtures_fault(db, user) -> int:
    fault = Fault(title="他人故障来源", created_by=user.id)
    db.add(fault)
    db.flush()
    return fault.id


def _hidden_sop(db, user) -> Sop:
    sop = Sop(
        doc_id=str(uuid4()),
        version=1,
        title="他人未发布草稿",
        status=SopStatus.draft,
        created_by=user.id,
    )
    db.add(sop)
    db.commit()
    return sop


def test_search_all_groups_permissions_and_links(db, operator):
    other = make_user(db, "op_search_other", Role.operator)
    _fixtures(db, operator, other)
    executor = _executor(db, operator)

    result = executor.execute("search_all", {"query": "超时"})
    assert result.status == "ok"
    assert "统一检索: 超时" in result.content
    assert "[案例]" in result.content and "[SOP]" in result.content
    assert "链接=/cases/" in result.content
    # 他人的已通过案例、已发布 SOP 可见;他人未解决的故障不可见
    assert "支付超时已审案例" in result.content
    assert "支付超时处理规范" in result.content
    assert "支付服务超时" not in result.content

    typed = executor.execute("search_all", {"query": "order", "types": ["server"]})
    assert typed.status == "ok"
    assert "[服务器]" in typed.content
    assert "order-node-1" in typed.content
    assert "链接=/monitoring" in typed.content
    assert "[案例]" not in typed.content and "[SOP]" not in typed.content
    assert "[故障]" not in typed.content

    empty = executor.execute("search_all", {"query": "   "})
    assert empty.status == "denied"