"""对话业务数据工具:故障清单、待办汇总、权限范围与端到端工具调用。"""

import json
from uuid import uuid4

from sqlalchemy import select

from app.ai import chat_service
from app.ai.llm import LLMResult
from app.ai.providers import UserAiConfig
from app.ai.tools import ToolExecutor
from app.config import Settings
from app.models import (
    AiToolInvocation,
    Case,
    CaseStatus,
    Fault,
    FaultStatus,
    MonitoringConfig,
    Sop,
    SopStatus,
)


def _settings() -> Settings:
    return Settings(
        secret_key="unit-test-secret-key-0123456789",
        ai_mode="cloud",
        ai_base_url="https://api.example.com/v1",
        ai_model="test-model",
    )


def _chat_executor(db, user) -> ToolExecutor:
    return ToolExecutor(db=db, user=user, settings=_settings(), conversation_id=1)


def _fault(db, user, title: str, status: FaultStatus) -> Fault:
    row = Fault(title=title, created_by=user.id, status=status)
    db.add(row)
    db.commit()
    return row


def _create_conversation(client, headers) -> int:
    resp = client.post("/api/chat/conversations", json={"title": "业务数据工具"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_list_faults_respects_operator_scope(db, operator, reviewer):
    _fault(db, operator, "我的磁盘告警", FaultStatus.open)
    _fault(db, reviewer, "他人的数据库告警", FaultStatus.open)
    _fault(db, operator, "已恢复故障", FaultStatus.resolved)

    op = _chat_executor(db, operator).execute(
        "list_faults", {"status": "unresolved", "limit": 20}
    )
    assert op.status == "ok"
    assert "我的磁盘告警" in op.content
    assert "他人的数据库告警" not in op.content
    assert "已恢复故障" not in op.content

    rev = _chat_executor(db, reviewer).execute("list_faults", {"status": "unresolved"})
    assert "他人的数据库告警" in rev.content
    assert "我的磁盘告警" in rev.content  # 审核/管理员可见全部


def test_list_faults_status_keyword_and_limit(db, operator):
    _fault(db, operator, "Nginx 502 错误", FaultStatus.open)
    _fault(db, operator, "MySQL 慢查询", FaultStatus.draft)
    _fault(db, operator, "磁盘写满", FaultStatus.resolved)

    executor = _chat_executor(db, operator)
    resolved = executor.execute("list_faults", {"status": "resolved"})
    assert "磁盘写满" in resolved.content
    assert "Nginx 502 错误" not in resolved.content

    keyword = executor.execute("list_faults", {"status": "all", "keyword": "Nginx"})
    assert "Nginx 502 错误" in keyword.content
    assert "MySQL 慢查询" not in keyword.content

    limited = executor.execute("list_faults", {"status": "all", "limit": 1})
    assert "共 3 条" in limited.content
    assert sum(1 for line in limited.content.splitlines() if line.startswith("#")) == 1


def test_list_todos_operator_and_reviewer(db, operator, reviewer):
    fault = _fault(db, operator, "待处理故障", FaultStatus.open)
    source = _fault(db, operator, "被退回案例的来源故障", FaultStatus.resolved)
    db.add_all(
        [
            Case(
                fault_id=fault.id,
                title="待审核案例",
                status=CaseStatus.pending_review,
                created_by=operator.id,
            ),
            Case(
                fault_id=source.id,
                title="被退回案例",
                status=CaseStatus.returned,
                created_by=operator.id,
            ),
            Sop(
                doc_id=str(uuid4()),
                version=1,
                title="待审核 SOP",
                status=SopStatus.submitted,
                created_by=operator.id,
                tags=[],
            ),
        ]
    )
    db.commit()

    op = _chat_executor(db, operator).execute("list_todos", {"limit": 10})
    assert "我的未解决故障: 1 条" in op.content
    assert "待我审核的案例" not in op.content
    assert "被退回给我的案例: 1 条" in op.content
    assert "被退回案例" in op.content

    rev = _chat_executor(db, reviewer).execute("list_todos", {"limit": 10})
    assert "待我审核的案例: 1 条" in rev.content
    assert "待我审核的 SOP: 1 条" in rev.content
    assert "待审核案例" in rev.content


def test_specs_depend_on_context_and_monitoring_config(db, operator):
    analysis = ToolExecutor(db=db, user=operator, settings=_settings(), fault_id=1)
    chat = _chat_executor(db, operator)

    assert analysis.specs() == []
    assert {spec["function"]["name"] for spec in chat.specs()} == {
        "list_faults",
        "list_todos",
        "get_fault",
        "get_case",
        "get_sop",
        "search_all",
    }

    db.add(MonitoringConfig(id=1, enabled=True, prometheus_url="http://127.0.0.1:9090"))
    db.commit()
    assert "prometheus_query" in {spec["function"]["name"] for spec in chat.specs()}
    analysis_names = {spec["function"]["name"] for spec in analysis.specs()}
    assert "prometheus_query" in analysis_names
    assert "list_faults" not in analysis_names

    # 分析场景不允许调用业务数据工具,即使模型臆造
    denied = analysis.execute("list_faults", {})
    assert denied.status == "denied"
    assert "未知工具" in denied.content


class _ToolCallingAdapter:
    """第一轮调用 list_faults,第二轮返回基于结果的回答。"""

    def __init__(self):
        self.calls = 0

    def chat(self, request):
        self.calls += 1
        if self.calls == 1:
            names = {spec["function"]["name"] for spec in (request.tools or [])}
            assert "list_faults" in names
            assert "list_todos" in names
            return LLMResult(
                text="",
                usage={"total_tokens": 5},
                model="fake",
                latency_ms=2,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "list_faults",
                            "arguments": '{"status":"unresolved"}',
                        },
                    }
                ],
            )
        return LLMResult(
            text=json.dumps(
                {
                    "answer": "当前有 1 个未解决故障:Nginx 502 错误。",
                    "citations": [],
                    "insufficient_evidence": False,
                    "refusal": False,
                    "suggested_actions": [],
                },
                ensure_ascii=False,
            ),
            usage={"total_tokens": 20},
            model="fake",
            latency_ms=3,
        )


def test_chat_calls_business_tool_and_persists_invocation(
    client, db, operator, op_headers, monkeypatch
):
    _fault(db, operator, "Nginx 502 错误", FaultStatus.open)
    adapter = _ToolCallingAdapter()
    monkeypatch.setattr(
        chat_service,
        "resolve_effective_ai",
        lambda db, user, settings: UserAiConfig(
            provider="deepseek",
            base_url="https://api.example.com/v1",
            model="fake-model",
            api_key="sk-fake",
        ),
    )
    monkeypatch.setattr(
        chat_service, "build_adapter", lambda settings, user_config=None: adapter
    )

    conversation_id = _create_conversation(client, op_headers)
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "列出所有待解决的问题"},
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    assistant = resp.json()["assistant_message"]
    assert assistant["status"] == "ok"
    assert "Nginx 502 错误" in assistant["content"]
    assert assistant["tool_calls"][0]["tool"] == "list_faults"
    assert adapter.calls == 2

    invocation = db.scalar(
        select(AiToolInvocation).where(AiToolInvocation.conversation_id == conversation_id)
    )
    assert invocation is not None
    assert invocation.tool == "list_faults"
    assert invocation.status == "ok"
    assert "Nginx 502 错误" in invocation.result_excerpt