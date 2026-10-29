"""AI 实时工具:Prometheus 查询、SSH 只读模板、调用预算与工具循环。"""

import json

import pytest

from app.ai.llm import LLMError, LLMRequest, LLMResult
from app.ai.schema import AnalysisReport, EvidenceItem
from app.ai.service import verify_report
from app.ai.tool_loop import run_tool_loop
from app.ai.tools import MAX_CALLS_PER_ANALYSIS, ToolExecutor, ToolOutcome
from app.config import Settings
from app.models import Fault, MonitoredServer
from app.monitoring.prometheus import QueryResult, Series


def _settings() -> Settings:
    return Settings(
        secret_key="unit-test-secret-key-0123456789",
        ai_mode="cloud",
        ai_base_url="https://api.example.com/v1",
        ai_model="test-model",
    )


class _FakeProm:
    def query_range(self, expr, minutes=60, step=60):
        return QueryResult(
            series=[Series(labels={"instance": "192.168.196.10:9100"}, points=[(1, 2.0), (2, 3.0)])]
        )


def _executor(db, operator, fault_id=1) -> ToolExecutor:
    return ToolExecutor(db=db, user=operator, settings=_settings(), fault_id=fault_id)


def test_prometheus_tool_produces_evidence(db, operator, monkeypatch):
    monkeypatch.setattr("app.ai.tools.resolve_client", lambda db: _FakeProm())
    executor = _executor(db, operator)

    outcome = executor.execute("prometheus_query", json.dumps({"query": "up", "minutes": 5}))

    assert outcome.status == "ok"
    assert outcome.evidence_id == "tool:1"
    assert "[evidence: tool:1]" in outcome.content
    assert "last=3" in outcome.content
    assert executor.invocations[0]["tool"] == "prometheus_query"
    assert executor.evidence()[0][0] == "tool:1"


def test_prometheus_tool_reports_missing_config(db, operator, monkeypatch):
    monkeypatch.setattr("app.ai.tools.resolve_client", lambda db: None)
    executor = _executor(db, operator)
    outcome = executor.execute("prometheus_query", {"query": "up"})
    assert outcome.status == "error"
    assert "未启用" in outcome.content


def test_tool_budget_is_enforced(db, operator, monkeypatch):
    monkeypatch.setattr("app.ai.tools.resolve_client", lambda db: _FakeProm())
    executor = _executor(db, operator)
    for _ in range(MAX_CALLS_PER_ANALYSIS):
        assert executor.execute("prometheus_query", {"query": "up"}).status == "ok"
    denied = executor.execute("prometheus_query", {"query": "up"})
    assert denied.status == "denied"
    assert executor.used == MAX_CALLS_PER_ANALYSIS


def test_unknown_tool_is_denied(db, operator):
    executor = _executor(db, operator)
    outcome = executor.execute("shell", {"command": "rm -rf /"})
    assert outcome.status == "denied"
    assert "未知工具" in outcome.content


def test_ssh_tool_uses_allowlisted_template(db, operator, monkeypatch):
    server = MonitoredServer(
        name="ubuntu3",
        host="192.168.196.30",
        port=22,
        username="ubuntu1",
        private_key_encrypted="enc",
        host_key_fingerprint="SHA256:x",
    )
    db.add(server)
    db.commit()
    monkeypatch.setattr(
        "app.ai.tools.execute_server_template",
        lambda db, server, template, args, settings: {
            "stdout": "up 3 days, load 0.10",
            "exit_status": 0,
            "truncated": False,
            "latency_ms": 15,
        },
    )
    executor = _executor(db, operator)
    outcome = executor.execute("ssh_run", {"server": "ubuntu3", "template": "uptime"})
    assert outcome.status == "ok"
    assert "up 3 days" in outcome.content
    assert "退出码: 0" in outcome.content


def test_ssh_tool_rejects_unknown_server(db, operator):
    executor = _executor(db, operator)
    outcome = executor.execute("ssh_run", {"server": "nope", "template": "uptime"})
    assert outcome.status == "denied"


def test_ssh_tool_propagates_template_error(db, operator, monkeypatch):
    server = MonitoredServer(
        name="s",
        host="10.0.0.7",
        port=22,
        username="u",
        private_key_encrypted="enc",
        host_key_fingerprint="SHA256:x",
    )
    db.add(server)
    db.commit()
    from app.monitoring.templates import TemplateError

    monkeypatch.setattr(
        "app.ai.tools.execute_server_template",
        lambda *a, **k: (_ for _ in ()).throw(TemplateError("参数 unit 格式不合法")),
    )
    executor = _executor(db, operator)
    outcome = executor.execute("ssh_run", {"server": "s", "template": "service_status", "args": {"unit": "x; rm"}})
    assert outcome.status == "error"
    assert "格式不合法" in outcome.content


def test_persist_writes_invocation_rows(db, operator, monkeypatch):
    fault = Fault(title="t", created_by=operator.id)
    db.add(fault)
    db.commit()
    monkeypatch.setattr("app.ai.tools.resolve_client", lambda db: _FakeProm())
    executor = _executor(db, operator, fault.id)
    executor.execute("prometheus_query", {"query": "up"})
    executor.persist(analysis_id=None)  # analysis_id 允许为空(失败分析也有调用记录)
    from app.models import AiToolInvocation

    row = db.query(AiToolInvocation).one()
    assert row.tool == "prometheus_query"
    assert row.fault_id == fault.id


# ===== 工具循环 =====

class _ScriptedAdapter:
    def __init__(self, results):
        self.results = list(results)
        self.requests: list[LLMRequest] = []

    def chat(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeExecutor:
    def __init__(self):
        self.invocations = []
        self.used = 0

    def execute(self, name, arguments):
        self.used += 1
        self.invocations.append({"tool": name})
        return ToolOutcome(content="[evidence: tool:1] cpu 91%", evidence_id="tool:1")


def _tool_call(call_id="c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "prometheus_query", "arguments": '{"query":"up"}'},
    }


def test_tool_loop_executes_tools_then_returns_text():
    adapter = _ScriptedAdapter(
        [
            LLMResult(text="", usage={"total_tokens": 10}, model="m", latency_ms=5, tool_calls=[_tool_call()]),
            LLMResult(text='{"summary":"ok"}', usage={"total_tokens": 20}, model="m", latency_ms=7),
        ]
    )
    executor = _FakeExecutor()
    loop = run_tool_loop(adapter, [], executor, max_output_tokens=512)
    assert loop.text == '{"summary":"ok"}'
    assert loop.used_tools is True
    assert loop.call_count == 2
    assert loop.usages == [{"total_tokens": 10}, {"total_tokens": 20}]
    roles = [m.role for m in loop.messages]
    assert "tool" in roles
    assert adapter.requests[0].tools is not None
    assert adapter.requests[1].tools is not None


def test_tool_loop_marks_unsupported_on_bad_request():
    adapter = _ScriptedAdapter([LLMError("bad_request", "tools not supported")])
    loop = run_tool_loop(adapter, [], _FakeExecutor(), max_output_tokens=512)
    assert loop.unsupported
    assert loop.text == ""


def test_tool_loop_forces_conclusion_after_max_rounds():
    adapter = _ScriptedAdapter(
        [
            LLMResult(text="", usage=None, model="m", latency_ms=1, tool_calls=[_tool_call("a")]),
            LLMResult(text="", usage=None, model="m", latency_ms=1, tool_calls=[_tool_call("b")]),
            LLMResult(text="", usage=None, model="m", latency_ms=1, tool_calls=[_tool_call("c")]),
            LLMResult(text='{"summary":"done"}', usage=None, model="m", latency_ms=1),
        ]
    )
    loop = run_tool_loop(adapter, [], _FakeExecutor(), max_output_tokens=512, max_rounds=3)
    assert loop.text == '{"summary":"done"}'
    assert len(adapter.requests) == 4
    # 收尾调用保留 tools 定义但显式 tool_choice="none"(部分厂商拒绝无 tools 的 tool 消息)
    assert adapter.requests[-1].tools is not None
    assert adapter.requests[-1].tool_choice == "none"


# ===== 证据校验 =====

def test_verify_report_accepts_tool_evidence():
    report = AnalysisReport(
        summary="s",
        observed_facts=[{"evidence_ids": ["tool:1"], "fact": "cpu 91%"}],
        hypotheses=[],
        missing_information=[],
        next_checks=[],
        references=[],
        insufficient_evidence=False,
    )
    model, reasons = verify_report(
        report,
        [EvidenceItem(evidence_id="tool:1", source="tool:prometheus_query", text="cpu 91%")],
        [],
        [],
    )
    assert reasons == []
    assert len(model.observed_facts) == 1


def test_verify_report_drops_unknown_tool_evidence():
    report = AnalysisReport(
        summary="s",
        observed_facts=[{"evidence_ids": ["tool:99"], "fact": "编造"}],
        hypotheses=[],
        missing_information=[],
        next_checks=[],
        references=[],
        insufficient_evidence=False,
    )
    model, reasons = verify_report(report, [], [], [])
    assert model.observed_facts == []
    assert reasons