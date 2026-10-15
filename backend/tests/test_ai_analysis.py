"""AI 分析与知识检索测试(第二阶段)。

覆盖:权限(越权 403)、空输入 400、demo 标注、缓存复用、历史倒序、
模型失败不丢数据、引用校验(无效引用移除+不完整标注)、检索过滤(草稿/未发布不进入)、
限流 429、配置版本返回。不在此测试中调用外部模型接口(真实联调用 scripts/smoke_llm.py)。
"""

import json

import pytest

from app.ai import service as ai_service
from app.ai.llm import LLMError, LLMRequest, LLMResult
from app.ai.schema import AnalysisReport
from app.config import Settings


# ---- 夹具辅助 ----


def _create_fault(client, headers, **overrides):
    payload = {
        "title": "Pod 频繁 OOMKilled",
        "description": "checkout 服务 pod 被 OOMKilled 重启",
        "environment": "prod",
        "service": "checkout",
        "component_version": "1.8.2",
        "error_logs": "Reason: OOMKilled",
        "status": "open",
    }
    payload.update(overrides)
    resp = client.post("/api/faults", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _reset_ai_state():
    """清理进程内限流/缓存状态,避免测试互相干扰。"""
    with ai_service._rate_lock:
        ai_service._rate_buckets.clear()
        ai_service._inflight.clear()
    with ai_service._cache_lock:
        ai_service._cache.clear()
    yield
    with ai_service._rate_lock:
        ai_service._rate_buckets.clear()
        ai_service._inflight.clear()
    with ai_service._cache_lock:
        ai_service._cache.clear()


def _demo_settings() -> Settings:
    return Settings(ai_mode="demo")


def _use_demo(monkeypatch):
    # ai 路由与 system 路由各自 import get_settings,两处都要打桩
    monkeypatch.setattr("app.routers.ai.get_settings", _demo_settings)
    monkeypatch.setattr("app.routers.system.get_settings", _demo_settings)


# ---- 触发分析(demo) ----


def test_analyze_demo_marks_and_persists(client, op_headers, monkeypatch):
    """demo 分析:标注 is_demo、summary 含演示标注、usage 为 null(不虚构用量)。"""
    _use_demo(monkeypatch)
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={"trigger_type": "manual"},
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    analysis = body["analysis"]
    assert analysis["is_demo"] is True
    assert analysis["status"] == "success"
    assert "演示数据" in analysis["report"]["summary"]
    assert analysis["usage"] is None  # demo 不虚构 token 用量
    assert analysis["trigger_type"] == "manual"
    assert analysis["input_hash"]
    assert body["reused"] is False

    # 故障本身不被分析修改(AI 不自动解决故障)
    fault_after = client.get(f"/api/faults/{fault['id']}", headers=op_headers).json()
    assert fault_after["status"] == "open"
    assert fault_after["actual_root_cause"] == ""


def test_analyze_with_unsaved_overlay(client, op_headers, monkeypatch):
    """未保存表单覆盖参与分析,但不回写故障字段。"""
    _use_demo(monkeypatch)
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={
            "trigger_type": "auto",
            "input": {"error_logs": "新增日志: readiness probe timeout", "title": "改了标题"},
        },
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["analysis"]["trigger_type"] == "auto"
    # 不回写:故障仍是保存前内容
    fault_after = client.get(f"/api/faults/{fault['id']}", headers=op_headers).json()
    assert fault_after["title"] == "Pod 频繁 OOMKilled"
    assert "readiness" not in fault_after["error_logs"]


def test_analyze_empty_input_rejected(client, op_headers, monkeypatch):
    """空输入返回 400,不落库。"""
    _use_demo(monkeypatch)
    fault = _create_fault(
        client, op_headers, title=" ", description="", error_logs="", service="", environment=""
    )
    # 创建时标题不能纯空白,退一步:创建后用 overlay 全部清空
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={
            "input": {
                "title": "",
                "description": "",
                "error_logs": "",
                "recent_changes": "",
                "attempted_actions": "",
                "environment": "",
                "service": "",
                "component_version": "",
            }
        },
        headers=op_headers,
    )
    assert resp.status_code == 400, resp.text
    history = client.get(f"/api/faults/{fault['id']}/analyses", headers=op_headers).json()
    assert history == []  # 空输入不落库


def test_cache_reuse_same_input(client, op_headers, monkeypatch):
    """同输入+同知识版本+同配置版本:复用原记录,不重复落库。"""
    _use_demo(monkeypatch)
    fault = _create_fault(client, op_headers)
    r1 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers
    )
    assert r1.status_code == 201
    r2 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers
    )
    assert r2.status_code == 201
    assert r2.json()["reused"] is True
    assert r2.json()["analysis"]["id"] == r1.json()["analysis"]["id"]
    history = client.get(f"/api/faults/{fault['id']}/analyses", headers=op_headers).json()
    assert len(history) == 1  # 复用不产生重复记录


def test_history_desc_order(client, op_headers, monkeypatch):
    """分析历史倒序返回(最新在前)。"""
    _use_demo(monkeypatch)
    fault = _create_fault(client, op_headers)
    for i in range(3):
        resp = client.post(
            f"/api/faults/{fault['id']}/analyze",
            json={"use_cache": False, "input": {"description": f"描述变体 {i}"}},
            headers=op_headers,
        )
        assert resp.status_code == 201
    history = client.get(f"/api/faults/{fault['id']}/analyses", headers=op_headers).json()
    ids = [h["id"] for h in history]
    assert ids == sorted(ids, reverse=True)
    assert len(ids) == 3


# ---- 权限 ----


def test_analyze_other_operator_forbidden(client, op_headers, db, monkeypatch):
    """运维只能分析自己的故障:越权返回 403。"""
    _use_demo(monkeypatch)
    from tests.conftest import login, make_user
    from app.models import Role

    make_user(db, "op_other", Role.operator)
    other_headers = login(client, "op_other")
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={}, headers=other_headers
    )
    assert resp.status_code == 403
    resp = client.get(f"/api/faults/{fault['id']}/analyses", headers=other_headers)
    assert resp.status_code == 403


def test_reviewer_can_view_analysis(client, op_headers, rev_headers, monkeypatch):
    """审核人员可查看任意故障的分析历史(读权限)。"""
    _use_demo(monkeypatch)
    fault = _create_fault(client, op_headers)
    client.post(f"/api/faults/{fault['id']}/analyze", json={}, headers=op_headers)
    resp = client.get(f"/api/faults/{fault['id']}/analyses", headers=rev_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---- 模型失败:不丢数据、不回退预设答案 ----


class _FailingAdapter:
    def chat(self, request: LLMRequest) -> LLMResult:
        raise LLMError("timeout", "模型接口超时(测试)")


class _BadJsonAdapter:
    def chat(self, request: LLMRequest) -> LLMResult:
        return LLMResult(text="这不是 JSON", usage=None, model="stub", latency_ms=1)


def test_model_failure_persists_failed_row(client, op_headers, monkeypatch):
    """模型失败:落 failed 记录,故障输入不丢,响应不是预设答案。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings", lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m")
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: _FailingAdapter())
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "failed"
    assert analysis["report"] is None  # 失败不返回预设答案
    assert analysis["error_code"] == "timeout"
    # 故障与输入完好
    fault_after = client.get(f"/api/faults/{fault['id']}", headers=op_headers).json()
    assert fault_after["title"] == "Pod 频繁 OOMKilled"
    assert fault_after["error_logs"] == "Reason: OOMKilled"


def test_bad_json_marks_failed_not_fabricated(client, op_headers, monkeypatch):
    """模型输出非法 JSON(含有限修复重试后仍非法):failed,不编造报告。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings", lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m")
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: _BadJsonAdapter())
    fault = _create_fault(client, op_headers)
    resp = client.post(f"/api/faults/{fault['id']}/analyze", json={}, headers=op_headers)
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "failed"
    assert analysis["error_code"] == "bad_response"
    assert analysis["report"] is None
    assert analysis["call_count"] >= 1  # 有限重试后放弃


# ---- 引用与报告校验 ----


def test_verify_report_scrubs_invalid_refs():
    """无效证据 ID / 无效引用 / 未知分类:移除并标注不完整,不编造。"""
    from app.ai.schema import (
        Hypothesis,
        KnowledgeReference,
        KnowledgeSnippet,
        ObservedFact,
        EvidenceItem,
    )

    evidence = [EvidenceItem(evidence_id="desc", source="description", text="pod OOMKilled")]
    snippets = [
        KnowledgeSnippet(
            ref_id="case:1",
            source_type="case",
            source_id=1,
            version=None,
            title="历史 OOM 案例",
            snippet="limits.memory 过低",
            link="/cases/1",
            status="approved",
        )
    ]
    report = AnalysisReport(
        summary="测试",
        observed_facts=[ObservedFact(evidence_ids=["desc", "fake:99"], fact="事实")],
        suggested_category="不存在的分类",
        suggested_tags=["a", "a", "b"],
        hypotheses=[
            Hypothesis(
                title="h1",
                rationale="r",
                evidence_ids=["not-exists"],
                reference_ids=["case:1", "case:999"],
                missing_information=[],
                verification="v",
                priority="乱写的优先级",
            )
        ],
        references=[
            KnowledgeReference(ref_id="case:1", source_type="case", source_id=1),
            KnowledgeReference(ref_id="sop:5", source_type="sop", source_id=5, version=1),
        ],
        insufficient_evidence=False,
    )
    cleaned, reasons = ai_service.verify_report(report, evidence, snippets, ["Linux"])

    assert "fake:99" not in cleaned.observed_facts[0].evidence_ids
    assert "not-exists" not in cleaned.hypotheses[0].evidence_ids
    assert "case:999" not in cleaned.hypotheses[0].reference_ids
    assert cleaned.hypotheses[0].priority in ("优先排查", "次优先", "证据不足")
    assert cleaned.suggested_category is None
    assert cleaned.suggested_tags == ["a", "b"]
    ref_ids = [r.ref_id for r in cleaned.references]
    assert ref_ids == ["case:1"]  # sop:5 无对应有效片段,移除
    assert reasons  # 标注不完整


# ---- 限流 ----


def test_rate_limit_returns_429(client, op_headers, monkeypatch):
    """超过单用户频率限制返回 429,前端不得立即循环请求。"""
    def limited():
        return Settings(ai_mode="demo", ai_rate_limit_per_minute=2)

    monkeypatch.setattr("app.routers.ai.get_settings", limited)
    fault = _create_fault(client, op_headers)
    for i in range(2):
        resp = client.post(
            f"/api/faults/{fault['id']}/analyze",
            json={"use_cache": False, "input": {"description": f"变体 {i}"}},
            headers=op_headers,
        )
        assert resp.status_code == 201
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={"use_cache": False, "input": {"description": "变体 3"}},
        headers=op_headers,
    )
    assert resp.status_code == 429


# ---- 知识检索过滤 ----


def _publish_sop(client, op_headers, rev_headers, fault_id):
    """走完 解决故障 → 审核案例 → 生成 SOP → 提交 → 发布 流程,返回已发布 SOP。"""
    from tests.test_workflow import _resolve

    _resolve(client, op_headers, fault_id)
    cases = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    case_id = cases[0]["id"]
    # 案例审核由 reviewer 执行
    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/cases/{case_id}/generate-sop", headers=op_headers)
    assert resp.status_code in (200, 201), resp.text
    sop = resp.json()
    resp = client.post(f"/api/sops/{sop['id']}/submit", headers=op_headers)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/sops/{sop['id']}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200, resp.text
    return sop


def test_search_excludes_unpublished(client, op_headers, rev_headers, monkeypatch):
    """检索只返回已审核案例与当前有效已发布 SOP;草稿/待审/退回不进入。"""
    _use_demo(monkeypatch)
    # 1) 一个已发布 SOP + 其来源案例(approved)
    fault1 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    sop = _publish_sop(client, op_headers, rev_headers, fault1["id"])
    assert client.get(f"/api/sops/{sop['id']}", headers=op_headers).json()["status"] == "published"

    # 2) 一个未审核案例(resolved 但 pending_review)与一个草稿故障,不应进入检索
    fault2 = _create_fault(client, op_headers, title="Redis 连接超时草稿", error_logs="ERR timeout draft")
    client.post(
        f"/api/faults/{fault2['id']}/records",
        json={"kind": "check_result", "content": "redis ping fail"},
        headers=op_headers,
    )

    resp = client.get("/api/search?q=Redis 连接超时", headers=op_headers)
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    # 只允许 approved 案例或 published SOP
    for h in hits:
        assert (h["source_type"] == "case" and h["status"] == "approved") or (
            h["source_type"] == "sop" and h["status"] == "published"
        )
    assert resp.json()["knowledge_version"]

    # 无匹配关键词:返回空,不编造
    resp = client.get("/api/search?q=完全不相关的词xyzzy123", headers=op_headers)
    assert resp.json()["hits"] == []


def test_search_requires_auth(client):
    resp = client.get("/api/search?q=test")
    # HTTPBearer 缺失凭据为 403;无效令牌路径为 401(见 deps.get_current_user)
    assert resp.status_code in (401, 403)


# ---- 配置版本 ----


def test_ai_status_includes_config_version(client, op_headers, monkeypatch):
    """ai-status 返回模型配置版本(前端绑定请求/响应用),不含密钥字段。"""
    _use_demo(monkeypatch)
    resp = client.get("/api/system/ai-status", headers=op_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_mode"] == "demo"
    assert body["model_config_version"]  # 配置版本用于绑定分析请求/响应
    assert "api_key" not in body
    assert "ai_api_key" not in body


# ===== 第二阶段审查修复回归(2026-10-14)=====

# ---- P1 #1:缓存隔离(绑定故障与用户范围)----


def test_cache_not_shared_across_faults(client, op_headers, monkeypatch):
    """P1 #1:相同输入的其他故障不得命中同一缓存记录(各自落库、各自历史)。"""
    _use_demo(monkeypatch)
    f1 = _create_fault(client, op_headers)
    f2 = _create_fault(client, op_headers)  # 字段完全相同、无追加记录 → 输入哈希相同
    r1 = client.post(f"/api/faults/{f1['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    r2 = client.post(f"/api/faults/{f2['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r1.status_code == 201 and r2.status_code == 201
    body2 = r2.json()
    assert body2["reused"] is False  # 不复用其他故障的分析
    assert body2["analysis"]["fault_id"] == f2["id"]
    assert body2["analysis"]["id"] != r1.json()["analysis"]["id"]
    h1 = client.get(f"/api/faults/{f1['id']}/analyses", headers=op_headers).json()
    h2 = client.get(f"/api/faults/{f2['id']}/analyses", headers=op_headers).json()
    assert [h["fault_id"] for h in h1] == [f1["id"]]
    assert [h["fault_id"] for h in h2] == [f2["id"]]


def test_cache_not_shared_across_users(client, op_headers, db, monkeypatch):
    """P1 #1:其他用户不得经缓存拿走他人分析记录(绕过历史 403 隔离)。"""
    _use_demo(monkeypatch)
    from tests.conftest import login, make_user
    from app.models import Role

    make_user(db, "op_cache_other", Role.operator)
    other_headers = login(client, "op_cache_other")
    f1 = _create_fault(client, op_headers)
    f2 = _create_fault(client, other_headers)  # 相同字段
    r1 = client.post(f"/api/faults/{f1['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    r2 = client.post(f"/api/faults/{f2['id']}/analyze", json={"use_cache": True}, headers=other_headers)
    assert r1.status_code == 201 and r2.status_code == 201
    body2 = r2.json()
    assert body2["reused"] is False
    assert body2["analysis"]["id"] != r1.json()["analysis"]["id"]
    assert body2["analysis"]["fault_id"] == f2["id"]
    assert body2["analysis"]["created_by"] != r1.json()["analysis"]["created_by"]
    # B 自己的历史只含 B 自己的记录
    h2 = client.get(f"/api/faults/{f2['id']}/analyses", headers=other_headers).json()
    assert [h["id"] for h in h2] == [body2["analysis"]["id"]]


def test_cache_expiry_reanalyzes(client, op_headers, monkeypatch):
    """P1 #1:缓存过期后重新分析,不复用陈旧记录(缓存回收)。"""
    _use_demo(monkeypatch)
    fault = _create_fault(client, op_headers)
    r1 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r1.status_code == 201
    # 将缓存条目时间戳老化到 TTL 之外
    with ai_service._cache_lock:
        for k, (t, row_id) in list(ai_service._cache.items()):
            ai_service._cache[k] = (t - ai_service.CACHE_TTL_SECONDS - 1, row_id)
    r2 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r2.status_code == 201
    assert r2.json()["reused"] is False
    assert r2.json()["analysis"]["id"] != r1.json()["analysis"]["id"]
    history = client.get(f"/api/faults/{fault['id']}/analyses", headers=op_headers).json()
    assert len(history) == 2


# ---- P1 #2:脱敏覆盖 JSON/带引号键值 ----


def test_sanitize_json_and_quoted_credentials():
    """P1 #2:JSON 带引号键/值、含空格值、Authorization 变体都被打码。"""
    from app.ai.sanitize import REDACTED, sanitize_text

    cases = [
        # JSON 带引号键与值
        ('{"password":"合成密A","token":"合成密B","Authorization":"Bearer 合成密C"}',
         ["合成密A", "合成密B", "合成密C"]),
        # 带空格的引号值(修复前只打码到第一个空格)
        ('{"password": "空 格 密 码"}', ["空 格 密 码"]),
        ("password='单引号密值 也是密'", ["单引号密值 也是密"]),
        ('api_key="带空格的 key 值"', ["带空格的 key 值"]),
        # Authorization 头/键的常见形式
        ("Authorization: Bearer 头部密值 tail", ["头部密值", "tail"]),
        ('"Authorization": "Bearer JSON头密值"', ["JSON头密值"]),
        # 无引号键值含空格
        ("password=无引号密值 后面还有", ["无引号密值 后面还有"]),
        # 常见键名变体
        ('{"access_token":"访问密值","session_id":"会话密值"}', ["访问密值", "会话密值"]),
    ]
    for raw, secrets in cases:
        out = sanitize_text(raw)
        for s in secrets:
            assert s not in out, f"{s!r} 未脱敏: {raw!r} -> {out!r}"
        assert REDACTED in out, f"未见脱敏标记: {raw!r} -> {out!r}"


def test_sanitize_keeps_benign_text():
    """不误伤普通文本与非敏感键名。"""
    from app.ai.sanitize import sanitize_text

    out = sanitize_text("password_count 统计为 5;token_count 统计为 3;正常日志内容")
    assert "password_count 统计为 5" in out
    assert "token_count 统计为 3" in out
    assert "正常日志内容" in out


# ---- P1 #3:标题/分类名/知识标题统一脱敏(整份请求断言)----


def test_user_prompt_scrubs_all_fields():
    """P1 #3:知识标题、分类名等元数据与正文统一脱敏,不允许绕过。"""
    from app.ai.schema import EvidenceItem, KnowledgeSnippet
    from app.ai.service import AnalysisInput, _build_user_prompt

    inp = AnalysisInput(
        title="故障 password=标题A密钥",
        description="描述",
        category_name="分类 password=分类B密钥",
        service="svc",
    )
    evidence = [EvidenceItem(evidence_id="desc", source="description", text="日志 token=证据C密钥")]
    snippets = [
        KnowledgeSnippet(
            ref_id="case:1",
            source_type="case",
            source_id=1,
            version=None,
            title="案例 password=知识D密钥",
            snippet="片段内容 token=片段E密钥",
            link="/cases/1",
            status="approved",
        )
    ]
    prompt = _build_user_prompt(inp, evidence, snippets, ["Linux password=候选F密钥"])
    for secret in ["标题A密钥", "分类B密钥", "证据C密钥", "知识D密钥", "片段E密钥", "候选F密钥"]:
        assert secret not in prompt, f"{secret} 绕过了脱敏进入模型提示词"
    assert "[REDACTED]" in prompt


class _CaptureAdapter:
    """捕获完整模型请求;返回最小合法报告。"""

    def __init__(self):
        self.request = None

    def chat(self, request: LLMRequest) -> LLMResult:
        self.request = request
        return LLMResult(
            text=json.dumps(_MINIMAL_REPORT),
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model="stub",
            latency_ms=1,
        )


_MINIMAL_REPORT = {
    "summary": "摘要",
    "observed_facts": [],
    "suggested_category": None,
    "suggested_tags": [],
    "hypotheses": [],
    "missing_information": [],
    "next_checks": [],
    "references": [],
    "insufficient_evidence": True,
}


def test_model_request_boundary_scrubs_secrets(client, op_headers, adm_headers, monkeypatch):
    """P1 #2/#3:整份模型请求(所有消息)不得含合成凭据,覆盖输入字段与分类名。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)

    # 分类名带凭据(由管理员创建,进入分类候选块)
    resp = client.post(
        "/api/categories", json={"name": "分类名 password=分类G密钥"}, headers=adm_headers
    )
    assert resp.status_code in (200, 201), resp.text
    cat_id = resp.json()["id"]

    fault = _create_fault(client, op_headers, title="标题 password=标题H密钥")
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={
            "use_cache": False,
            "input": {
                "description": 'desc {"password":"描述I密钥"}',
                "error_logs": "logs Authorization: Bearer 日志J密钥",
                "recent_changes": "changes api_key=\"变更K密钥\"",
                "attempted_actions": "tried token=尝试L密钥",
                "category_id": cat_id,
            },
        },
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    assert adapter.request is not None
    full_text = "\n".join(m.content for m in adapter.request.messages)
    for secret in [
        "标题H密钥", "描述I密钥", "日志J密钥", "变更K密钥", "尝试L密钥", "分类G密钥",
    ]:
        assert secret not in full_text, f"{secret} 进入了模型请求"
    assert "[REDACTED]" in full_text


# ---- P1 #4:local 模式不继承环境代理 ----


class _FakeResp:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "model": "stub",
        }


class _FakeClient:
    """替换 httpx.Client:捕获构造参数(含 trust_env),不发真实请求。"""

    captured: dict = {}

    def __init__(self, **kwargs):
        _FakeClient.captured = dict(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        _FakeClient.captured["url"] = url
        _FakeClient.captured["body"] = json
        return _FakeResp()


def _clear_proxy_env(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize(
    "proxy_env",
    [
        {"HTTP_PROXY": "http://proxy.external.example:8080"},
        {"ALL_PROXY": "http://proxy.external.example:8080"},
        {"HTTPS_PROXY": "http://proxy.external.example:8080", "HTTP_PROXY": "http://proxy.external.example:8080"},
        # NO_PROXY 未覆盖目标时,httpx 默认会走代理:local 模式一律不继承
        {"HTTP_PROXY": "http://proxy.external.example:8080", "NO_PROXY": "other.example"},
        # NO_PROXY 覆盖目标也不依赖其正确性:local 直连
        {"HTTP_PROXY": "http://proxy.external.example:8080", "NO_PROXY": "10.1.2.3"},
    ],
)
def test_local_mode_ignores_env_proxies(monkeypatch, proxy_env):
    """P1 #4:local 直连内网目标,不把请求交给环境代理(防应用数据经外部代理中转)。"""
    from app.ai.llm import ChatMessage, LLMRequest, build_adapter

    _clear_proxy_env(monkeypatch)
    for k, v in proxy_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("app.ai.llm.httpx.Client", _FakeClient)

    settings = Settings(ai_mode="local", ai_base_url="http://10.1.2.3:8000", ai_model="m")
    adapter = build_adapter(settings)
    result = adapter.chat(LLMRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert _FakeClient.captured.get("trust_env") is False  # 不继承任何环境代理
    assert result.text  # 请求路径本身仍可用


def test_cloud_mode_keeps_env_proxy(monkeypatch):
    """cloud 模式保留部署环境显式配置的代理(属基础设施),与 local 直连策略区分。"""
    from app.ai.llm import ChatMessage, LLMRequest, build_adapter

    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal.example:3128")
    monkeypatch.setattr("app.ai.llm.httpx.Client", _FakeClient)

    settings = Settings(ai_mode="cloud", ai_base_url="https://api.example.com/v1", ai_model="m")
    adapter = build_adapter(settings)
    result = adapter.chat(LLMRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert _FakeClient.captured.get("trust_env") is True
    assert result.text


def test_json_mode_only_when_requested(monkeypatch):
    """json_object 按请求开启:DeepSeek 等服务在提示词不含 "json" 时拒绝 json_object(400)。

    capability 只表示服务支持,不应强制所有请求携带 response_format。
    """
    from app.ai.llm import ChatMessage, LLMRequest, build_adapter

    _clear_proxy_env(monkeypatch)
    monkeypatch.setattr("app.ai.llm.httpx.Client", _FakeClient)
    settings = Settings(
        ai_mode="cloud", ai_base_url="https://api.example.com/v1", ai_model="m", ai_supports_json_mode=True
    )
    adapter = build_adapter(settings)
    msgs = [ChatMessage(role="user", content="hi")]

    adapter.chat(LLMRequest(messages=msgs, json_mode=True))
    assert _FakeClient.captured["body"].get("response_format") == {"type": "json_object"}

    adapter.chat(LLMRequest(messages=msgs, json_mode=False))
    assert "response_format" not in _FakeClient.captured["body"]

    # 服务不支持 json_mode 时,即使请求开了也不发 response_format(参数能力不匹配会被拒)
    settings_nojson = Settings(
        ai_mode="cloud", ai_base_url="https://api.example.com/v1", ai_model="m", ai_supports_json_mode=False
    )
    adapter2 = build_adapter(settings_nojson)
    adapter2.chat(LLMRequest(messages=msgs, json_mode=True))
    assert "response_format" not in _FakeClient.captured["body"]


# ---- P2 #9:模型返回后重新复核来源状态/版本 ----


class _DeprecatingAdapter:
    """chat() 期间经独立事务废弃命中 SOP,再返回引用它的合法报告(模拟调用窗口变更)。"""

    def __init__(self, sop_id: int, case_id: int):
        self.sop_id = sop_id
        self.case_id = case_id

    def chat(self, request: LLMRequest) -> LLMResult:
        from app.models import Sop, SopStatus
        from tests.conftest import TestingSessionLocal

        s = TestingSessionLocal()
        try:
            row = s.get(Sop, self.sop_id)
            row.status = SopStatus.deprecated
            s.commit()
        finally:
            s.close()
        report = {
            "summary": "调用期间知识变更",
            "observed_facts": [
                {"evidence_ids": ["desc"], "fact": "绑定证据的事实"}
            ],
            "suggested_category": None,
            "suggested_tags": [],
            "hypotheses": [
                {
                    "title": "候选",
                    "rationale": "依据",
                    "evidence_ids": ["desc"],
                    "reference_ids": [f"sop:{self.sop_id}", f"case:{self.case_id}"],
                    "missing_information": [],
                    "verification": "验证",
                    "priority": "次优先",
                }
            ],
            "missing_information": [],
            "next_checks": [],
            "references": [
                {"ref_id": f"sop:{self.sop_id}", "source_type": "sop", "source_id": self.sop_id, "version": 1, "relevance": "关联"},
                {"ref_id": f"case:{self.case_id}", "source_type": "case", "source_id": self.case_id, "version": None, "relevance": "关联"},
            ],
            "insufficient_evidence": False,
        }
        return LLMResult(text=json.dumps(report), usage=None, model="stub", latency_ms=1)


def test_references_revalidated_after_model_call(client, op_headers, rev_headers, monkeypatch):
    """P2 #9:调用期间被废弃的来源在返回前复核移除,历史展示遵循同一契约。"""
    # 1) 发布一个会被检索命中的 SOP(标题含「Redis 连接超时」)
    fault1 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    sop = _publish_sop(client, op_headers, rev_headers, fault1["id"])
    case_id = client.get("/api/cases?status=approved", headers=op_headers).json()[0]["id"]

    # 前置:该 SOP 确实会被关键词检索命中(否则谈不上「命中后失效」)
    hits = client.get("/api/search?q=Redis 连接超时", headers=op_headers).json()["hits"]
    assert any(h["source_type"] == "sop" and h["source_id"] == sop["id"] for h in hits), hits

    # 2) 分析目标故障:模型调用期间 SOP 被废弃
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr(
        "app.ai.service.build_adapter",
        lambda s: _DeprecatingAdapter(sop["id"], case_id),
    )
    fault2 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    resp = client.post(f"/api/faults/{fault2['id']}/analyze", json={"use_cache": False}, headers=op_headers)
    assert resp.status_code == 201, resp.text
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "incomplete"  # 失效引用 → 明确标注不完整
    ref_ids = [r["ref_id"] for r in analysis["report"]["references"]]
    assert f"sop:{sop['id']}" not in ref_ids  # 已废弃 SOP 不再出现
    assert f"case:{case_id}" in ref_ids  # 仍有效的来源保留
    hyp_refs = analysis["report"]["hypotheses"][0]["reference_ids"]
    assert f"sop:{sop['id']}" not in hyp_refs
    assert any("失效" in r for r in analysis["incomplete_reasons"])

    # 3) 历史接口同样不原样返回失效引用
    history = client.get(f"/api/faults/{fault2['id']}/analyses", headers=op_headers).json()
    hist_ref_ids = [r["ref_id"] for r in history[0]["report"]["references"]]
    assert f"sop:{sop['id']}" not in hist_ref_ids
    assert f"case:{case_id}" in hist_ref_ids
    assert any("失效" in r for r in history[0]["incomplete_reasons"])


# ---- P2 #10:无证据绑定的已知事实移除并标记 ----


def test_fact_without_evidence_removed():
    """P2 #10:已知事实必须绑定有效证据;无证据事实移除并标注不完整,不允许冒充事实。"""
    from app.ai.schema import AnalysisReport, EvidenceItem, ObservedFact

    evidence = [EvidenceItem(evidence_id="desc", source="description", text="DNS 解析失败")]
    report = AnalysisReport(
        summary="s",
        observed_facts=[
            ObservedFact(evidence_ids=[], fact="日志显示 CPU=100%"),  # 无证据绑定
            ObservedFact(evidence_ids=["desc"], fact="DNS 解析失败"),  # 有效绑定
        ],
        insufficient_evidence=False,
    )
    cleaned, reasons = ai_service.verify_report(report, evidence, [], [])
    assert len(cleaned.observed_facts) == 1
    assert cleaned.observed_facts[0].fact == "DNS 解析失败"
    assert reasons  # 明确标记不完整(incomplete_reasons 非空 → 状态为 incomplete)


def test_fact_without_evidence_marks_incomplete(client, op_headers, monkeypatch):
    """P2 #10:正常流程中无证据事实导致结果不完整,不得按 success 返回。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )

    class _Adapter:
        def chat(self, request: LLMRequest) -> LLMResult:
            report = dict(_MINIMAL_REPORT)
            report["observed_facts"] = [{"evidence_ids": [], "fact": "日志显示 CPU=100%"}]
            report["insufficient_evidence"] = False
            return LLMResult(text=json.dumps(report), usage=None, model="stub", latency_ms=1)

    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: _Adapter())
    fault = _create_fault(client, op_headers)
    resp = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers)
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "incomplete"
    assert analysis["report"]["observed_facts"] == []
    assert analysis["incomplete_reasons"]


# ---- P2 #11:多次调用的 usage 汇总 ----


class _UsageRepairAdapter:
    """首次非法 JSON(usage 15),修复重试返回合法 JSON(usage 按构造参数)。"""

    def __init__(self, second_usage):
        self.calls = 0
        self.second_usage = second_usage

    def chat(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                text="这不是 JSON",
                usage={"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
                model="stub",
                latency_ms=1,
            )
        return LLMResult(
            text=json.dumps(_MINIMAL_REPORT),
            usage=self.second_usage,
            model="stub",
            latency_ms=1,
        )


def test_usage_aggregated_across_repair_calls(client, op_headers, monkeypatch):
    """P2 #11:修复重试的 usage 逐次汇总(15+28=43),不把最后一次单次值当整次合计。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _UsageRepairAdapter(
        second_usage={"prompt_tokens": 10, "completion_tokens": 18, "total_tokens": 28}
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers)
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["call_count"] == 2
    assert analysis["usage"]["total_tokens"] == 43  # 15+28,而非 28
    assert analysis["usage"]["prompt_tokens"] == 15
    assert analysis["usage"]["completion_tokens"] == 28


def test_usage_unknown_when_any_call_missing(client, op_headers, monkeypatch):
    """P2 #11:任一次调用缺失 usage → 整次用量未知(null),不显示单次已知值。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _UsageRepairAdapter(second_usage=None)  # 第二次不返回 usage
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers)
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["call_count"] == 2
    assert analysis["usage"] is None  # 不得把第一次的 15 标为整次合计


# ===== 第二阶段修复复审回归(2026-10-14)=====


# ---- 复审 P1 #1:转义/二次序列化日志与引号转义值中的凭据 ----


def test_sanitize_escaped_json_and_quote_escapes():
    """常见日志形态(转义 JSON 键、引号转义值、bash 拼接、键空白/unicode 转义)不泄露凭据。"""
    from app.ai.sanitize import sanitize_text

    checks = [
        # message 内二次序列化:内层键的引号带反斜杠
        ("escaped_json_log", json.dumps({"message": json.dumps({"password": "SecretEscapedA1!"})}), "SecretEscapedA1!"),
        # 单引号值内 \' 转义:不得把转义引号当结束引号留下后半段
        ("escaped_single_quote_value", r"password='SecretEscapedB1! \'second secret\''", "SecretEscapedB1!"),
        # bash 引号拼接 'a'\'b':拼接段整体打码
        ("bash_single_quote_value", "password='SecretEscapedC1!'\\''second secret'", "SecretEscapedC1!"),
        # 键内空白
        ("json_whitespace_key", '{" password ":"SecretEscapedD1!"}', "SecretEscapedD1!"),
        # 键名 unicode 转义 password(passw + ord)
        ("json_unicode_key", '{"pass\\u0077ord":"SecretEscapedE1!"}', "SecretEscapedE1!"),
        # 转义 JSON 中的 token / Authorization 变体
        ("escaped_token", r'log {\"token\": \"SecretEscapedF1!\"}', "SecretEscapedF1!"),
        ("escaped_auth", r'{\"Authorization\": \"Bearer SecretEscapedG1!\"}', "SecretEscapedG1!"),
    ]
    for name, raw, secret in checks:
        out = sanitize_text(raw)
        assert secret not in out, f"{name} 泄露凭据: {out}"
        assert "second secret" not in out, f"{name} 泄露拼接段: {out}"
        assert "[REDACTED]" in out, f"{name} 未打码: {out}"


def test_sanitize_pem_truncated_without_end():
    """PEM 只有 BEGIN、缺 END(被截断)时保守打码到结尾,不残留密钥材料。"""
    from app.ai.sanitize import sanitize_text

    raw = "-----BEGIN PRIVATE KEY-----\n" + "PemMaterialX9+/=\n" * 40
    out = sanitize_text(raw)
    assert "PemMaterialX9+/" not in out
    assert "[REDACTED]" in out
    # 完整 PEM(含 END)仍整体打码
    full = raw + "-----END PRIVATE KEY-----"
    out2 = sanitize_text(full)
    assert "PemMaterialX9+/" not in out2
    assert out2 == "[REDACTED]"


# ---- 复审 P1 #2:先脱敏后截断(长 PEM 不得因截断绕过打码)----


def test_long_pem_redacted_before_truncation():
    """整字段先脱敏后截断;长日志/近期变更/短字段截断边界均不残留材料。"""
    from types import SimpleNamespace

    from app.ai.service import _build_user_prompt

    pem = "-----BEGIN PRIVATE KEY-----\n" + "LongPemMaterialQZ9+/=\n" * 160 + "-----END PRIVATE KEY-----"
    assert len(pem) > 3000  # 超过单条证据上限,旧实现会截掉 END 行
    # 短字段截断边界:标题 500 字符内夹带凭据
    title = "x" * 480 + " password=ShortFieldSecret!"
    inp = SimpleNamespace(
        title=title,
        category_name="",
        environment="",
        service="",
        component_version="",
        recent_changes=pem[:2100],  # 近期变更截断边界 2000
    )
    evidence = [SimpleNamespace(evidence_id="logs", source="error_logs", text=pem)]
    prompt = _build_user_prompt(inp, evidence, [], [])
    assert "LongPemMaterialQZ9+/" not in prompt, "长 PEM 材料进入了提示词"
    assert "PemMaterial" not in prompt
    assert "ShortFieldSecret!" not in prompt, "截断边界旁的凭据未打码"
    assert "[REDACTED]" in prompt


def test_model_request_boundary_escaped_logs_and_long_pem(client, op_headers, monkeypatch):
    """复审 P1 #1/#2:转义日志凭据与长 PEM 材料不得进入实际模型请求(整份消息断言)。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    pem = "-----BEGIN PRIVATE KEY-----\n" + "ReqPemMaterialQZ9+/=\n" * 160 + "-----END PRIVATE KEY-----"
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={
            "use_cache": False,
            "input": {
                "error_logs": pem
                + "\n"
                + json.dumps({"message": json.dumps({"password": "ReqSecretEscapedA1!"})}),
                "recent_changes": r"password='ReqSecretB1! \'second secret\''",
            },
        },
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    assert adapter.request is not None
    full_text = "\n".join(m.content for m in adapter.request.messages)
    for secret in [
        "ReqPemMaterialQZ9+/",
        "PemMaterial",
        "ReqSecretEscapedA1!",
        "ReqSecretB1!",
        "second secret",
    ]:
        assert secret not in full_text, f"{secret} 进入了模型请求"


# ---- 复审次要项:显式清空分类不再沿用旧分类名 ----


def test_category_clear_removes_old_name(client, op_headers, adm_headers, monkeypatch):
    """input.category_id=null 显式清空后,提示词按「未分类」处理,不再带旧分类名。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    cat = client.post("/api/categories", json={"name": "旧人工分类"}, headers=adm_headers).json()
    fault = _create_fault(client, op_headers, category_id=cat["id"])
    # 带分类时提示词含分类名
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    assert "旧人工分类" in "\n".join(m.content for m in adapter.request.messages)
    # 显式清空(前端表单 category_id=null):不得再沿用旧分类名
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={"use_cache": False, "input": {"category_id": None}},
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    full = "\n".join(m.content for m in adapter.request.messages)
    # 故障分类字段按「未分类」处理;候选分类块合法包含全部分类名,不作整体排除断言
    assert "分类: 未分类" in full, "清空分类后未按未分类处理"
    assert "分类: 旧人工分类" not in full, "清空分类后仍沿用了旧分类名"


# ---- 复审 P2 #5:缓存复用返回前实时复核来源(并发窗口)----


def test_cache_hit_revalidates_sources(client, op_headers, rev_headers, monkeypatch):
    """检索完成与缓存返回之间来源被废弃时,缓存复用返回清理后的当前视图,
    不把已失效引用当有效知识;与历史读取契约一致。"""
    from app.models import Sop, SopStatus
    from tests.conftest import TestingSessionLocal

    _use_demo(monkeypatch)
    fault1 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    sop = _publish_sop(client, op_headers, rev_headers, fault1["id"])
    case_id = client.get("/api/cases?status=approved", headers=op_headers).json()[0]["id"]
    hits = client.get("/api/search?q=Redis 连接超时", headers=op_headers).json()["hits"]
    assert any(h["source_type"] == "sop" and h["source_id"] == sop["id"] for h in hits), hits

    fault2 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    # 第一次分析:命中知识并写入缓存(报告引用 sop + case)
    resp = client.post(f"/api/faults/{fault2['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert resp.status_code == 201, resp.text
    first = resp.json()
    assert first["reused"] is False
    first_ref_ids = [r["ref_id"] for r in first["analysis"]["report"]["references"]]
    assert f"sop:{sop['id']}" in first_ref_ids

    # 第二次分析:在「检索完成之后、缓存返回之前」经独立事务废弃 SOP(并发窗口)
    orig_key = ai_service._cache_key
    calls = {"n": 0}

    def key_then_deprecate(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # 打桩在第一次请求后安装:第二次请求是打桩后的第 1 次调用
            s = TestingSessionLocal()
            try:
                row = s.get(Sop, sop["id"])
                row.status = SopStatus.deprecated
                s.commit()
            finally:
                s.close()
        return orig_key(*args, **kwargs)

    monkeypatch.setattr(ai_service, "_cache_key", key_then_deprecate)
    resp = client.post(f"/api/faults/{fault2['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reused"] is True  # 确实命中了缓存(未重新分析)
    analysis = body["analysis"]
    assert analysis["status"] == "incomplete"  # 失效引用不得按 success 返回
    ref_ids = [r["ref_id"] for r in analysis["report"]["references"]]
    assert f"sop:{sop['id']}" not in ref_ids  # 已废弃 SOP 从返回视图移除
    assert f"case:{case_id}" in ref_ids  # 仍有效来源保留
    hyp_refs = analysis["report"]["hypotheses"][0]["reference_ids"]
    assert f"sop:{sop['id']}" not in hyp_refs
    assert any("失效" in r for r in analysis["incomplete_reasons"])
    # 历史读取遵循同一契约(已废弃引用同样被移除)
    history = client.get(f"/api/faults/{fault2['id']}/analyses", headers=op_headers).json()
    for item in history:
        hist_ref_ids = [r["ref_id"] for r in (item["report"] or {}).get("references", [])]
        assert f"sop:{sop['id']}" not in hist_ref_ids


# ---- 复审 P2 #6:部分 usage 字段缺失时不把未知当 0/合计 ----


def test_usage_partial_fields_not_summed(client, op_headers, monkeypatch):
    """修复调用只返回部分字段时,缺失字段不参与汇总(不当 0),
    不得把首调用的 total 标为整次合计。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _UsageRepairAdapter(second_usage={"prompt_tokens": 10})  # 第二次只有输入
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers)
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["call_count"] == 2
    usage = analysis["usage"]
    assert usage == {"prompt_tokens": 15}  # 5+10:仅每次调用都有的字段才汇总
    assert "completion_tokens" not in usage  # 第二次输出未知:不当 0、不沿用首调用值
    assert "total_tokens" not in usage  # 不得显示 15 当合计


def test_aggregate_usage_unit():
    """_aggregate_usage 按字段判定完整性(纯函数边界)。"""
    # 全部完整:逐字段求和
    assert ai_service._aggregate_usage(
        [
            {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
            {"prompt_tokens": 10, "completion_tokens": 18, "total_tokens": 28},
        ]
    ) == {"completion_tokens": 28, "prompt_tokens": 15, "total_tokens": 43}
    # 部分字段缺失:只汇总共有字段
    assert ai_service._aggregate_usage(
        [{"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}, {"prompt_tokens": 10}]
    ) == {"prompt_tokens": 15}
    # 整份缺失/为空:未知
    assert ai_service._aggregate_usage([{"prompt_tokens": 5}, None]) is None
    assert ai_service._aggregate_usage([{"prompt_tokens": 5}, {}]) is None
    assert ai_service._aggregate_usage([]) is None


# ===== 云端冒烟增量复审回归(2026-10-14)=====


# ---- 复审 P1:嵌套 JSON 转义引号值——密码含逗号/分号/右花括号不泄露尾部 ----


def test_sanitize_nested_json_delimited_password_values():
    """复审 P1:二次序列化 JSON 的密码含 , ; } 时整体打码,尾部不残留;兄弟字段不误伤。"""
    from app.ai.sanitize import sanitize_text

    cases = [
        ("ReviewPrefix47,ReviewCommaTail47", "ReviewCommaTail47"),
        ("ReviewPrefix58;ReviewSemiTail58", "ReviewSemiTail58"),
        ("ReviewPrefix69}ReviewBraceTail69", "ReviewBraceTail69"),
    ]
    for pw, tail in cases:
        raw = json.dumps({"message": json.dumps({"password": pw, "component": "db"})})
        out = sanitize_text(raw)
        assert "ReviewPrefix" not in out, f"密码前半段未打码: {out}"
        assert tail not in out, f"密码尾部泄露: {out}"
        assert "db" in out, f"兄弟字段被误伤: {out}"


def test_model_request_boundary_nested_json_delimiter_passwords(client, op_headers, monkeypatch):
    """复审 P1:三种分隔符变体的密码(含尾部)不得进入最终模型请求(整份消息断言)。"""
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    passwords = [
        "ReviewPrefix47,ReviewCommaTail47",
        "ReviewPrefix58;ReviewSemiTail58",
        "ReviewPrefix69}ReviewBraceTail69",
    ]
    logs = "\n".join(
        json.dumps({"message": json.dumps({"password": pw, "component": "db"})}) for pw in passwords
    )
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze",
        json={"use_cache": False, "input": {"error_logs": logs}},
        headers=op_headers,
    )
    assert resp.status_code == 201, resp.text
    full_text = "\n".join(m.content for m in adapter.request.messages)
    for pw in passwords:
        for part in (pw, pw.split(",")[-1].split(";")[-1].split("}")[-1], "ReviewPrefix"):
            assert part not in full_text, f"{part} 进入了模型请求"
    assert "[REDACTED]" in full_text


# ---- 复审 P2:检索返回前来源复核不得读取旧 ORM 状态(identity map 窗口)----


def _deprecate_sop_independent(sop_id: int) -> None:
    """独立事务:废弃指定 SOP 并提交(模拟两次读取之间的并发变更)。"""
    from app.models import Sop, SopStatus
    from tests.conftest import TestingSessionLocal

    other = TestingSessionLocal()
    try:
        row = other.get(Sop, sop_id)
        row.status = SopStatus.deprecated
        other.commit()
    finally:
        other.close()


def test_finalize_hit_bypasses_stale_identity_map(db):
    """复审 P2:独立事务废弃后,同一 Session 的返回前复核必须看到新状态。

    旧实现 db.get() 返回 identity map 陈旧实例(仍为 published),失效命中未剔除。
    """
    from app.models import Role, Sop, SopStatus
    from app.search.keyword import SearchHit, _finalize_hit
    from tests.conftest import make_user

    user = make_user(db, "kw_stale_user", Role.operator)
    sop = Sop(
        doc_id="doc-stale-1",
        version=1,
        title="StaleSopTitle",
        status=SopStatus.published,
        created_by=user.id,
    )
    db.add(sop)
    db.commit()
    hit = SearchHit(
        ref_id=f"sop:{sop.id}",
        source_type="sop",
        source_id=sop.id,
        version=1,
        title="StaleSopTitle",
        snippet="s",
        link=f"/sops/{sop.id}",
        status="published",
        score=1.0,
    )
    assert _finalize_hit(hit, db, user) is not None  # 基线:未变更时保留

    # 预热 identity map:同一 Session 已加载该行(陈旧窗口的来源)
    assert db.get(Sop, sop.id).status == SopStatus.published
    _deprecate_sop_independent(sop.id)
    assert _finalize_hit(hit, db, user) is None  # 必须读到新状态并剔除


def test_finalize_hit_sees_version_replacement(db):
    """复审 P2 换版变体:独立事务换版后,旧版本命中剔除。"""
    from app.models import Role, Sop, SopStatus
    from app.search.keyword import SearchHit, _finalize_hit
    from tests.conftest import TestingSessionLocal, make_user

    user = make_user(db, "kw_ver_user", Role.operator)
    sop = Sop(
        doc_id="doc-ver-1",
        version=1,
        title="VerSopTitle",
        status=SopStatus.published,
        created_by=user.id,
    )
    db.add(sop)
    db.commit()
    hit = SearchHit(
        ref_id=f"sop:{sop.id}",
        source_type="sop",
        source_id=sop.id,
        version=1,
        title="VerSopTitle",
        snippet="s",
        link=f"/sops/{sop.id}",
        status="published",
        score=1.0,
    )
    other = TestingSessionLocal()
    try:
        row = other.get(Sop, sop.id)
        row.version = 2  # 换版(status 仍 published,当前有效版本变为 v2)
        other.commit()
    finally:
        other.close()
    assert _finalize_hit(hit, db, user) is None  # v1 命中不再是当前有效版本


def test_search_knowledge_drops_sop_deprecated_mid_search(db, monkeypatch):
    """复审 P2:首次加载与 finalize 之间独立事务废弃,直接搜索不得返回旧命中。"""
    from app.models import Role, Sop, SopStatus
    from app.search import keyword as keyword_mod
    from tests.conftest import make_user

    user = make_user(db, "kw_mid_user", Role.operator)
    sop = Sop(
        doc_id="doc-mid-1",
        version=1,
        title="MidSearchSOP 磁盘告警",
        symptom="MidSearchSOP 磁盘告警处理",
        status=SopStatus.published,
        created_by=user.id,
    )
    db.add(sop)
    db.commit()

    orig = keyword_mod._make_snippet
    fired = {"n": 0}

    def hook(text, terms):
        if fired["n"] == 0:
            fired["n"] += 1
            _deprecate_sop_independent(sop.id)  # 加载后、复核前的独立事务变更
        return orig(text, terms)

    monkeypatch.setattr(keyword_mod, "_make_snippet", hook)
    hits, _ = keyword_mod.search_knowledge(db, user, "MidSearchSOP 磁盘告警")
    assert fired["n"] == 1, "未在检索窗口内触发独立事务变更"
    assert hits == [], "窗口内废弃的命中不得返回"


def test_search_api_excludes_sop_deprecated_mid_search(client, op_headers, db, monkeypatch):
    """复审 P2:/api/search 实际路由在窗口内废弃后不得返回旧正文片段。"""
    from app.models import Role, Sop, SopStatus
    from app.search import keyword as keyword_mod
    from tests.conftest import make_user

    author = make_user(db, "kw_sop_author_api", Role.operator)
    sop = Sop(
        doc_id="doc-api-1",
        version=1,
        title="ApiStaleSOP 内存泄漏",
        symptom="ApiStaleSOP 内存泄漏排查",
        status=SopStatus.published,
        created_by=author.id,
    )
    db.add(sop)
    db.commit()

    orig = keyword_mod._make_snippet
    fired = {"n": 0}

    def hook(text, terms):
        if fired["n"] == 0:
            fired["n"] += 1
            _deprecate_sop_independent(sop.id)
        return orig(text, terms)

    monkeypatch.setattr(keyword_mod, "_make_snippet", hook)
    resp = client.get("/api/search?q=ApiStaleSOP 内存泄漏", headers=op_headers)
    assert resp.status_code == 200
    assert resp.json()["hits"] == [], "窗口内废弃的 SOP 不得出现在 /api/search 结果"


def test_model_context_excludes_sop_deprecated_mid_search(client, op_headers, db, monkeypatch):
    """复审 P2:送入模型的知识片段与 /api/search 同源,窗口内废弃后旧正文不得进入上下文。"""
    from app.models import Role, Sop, SopStatus
    from app.search import keyword as keyword_mod
    from tests.conftest import make_user

    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    adapter = _CaptureAdapter()
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)

    author = make_user(db, "kw_sop_author_ctx", Role.operator)
    sop = Sop(
        doc_id="doc-ctx-1",
        version=1,
        title="CtxStaleSOP 连接池耗尽",
        symptom="CtxStaleSOP 连接池耗尽排查步骤",
        status=SopStatus.published,
        created_by=author.id,
    )
    db.add(sop)
    db.commit()

    orig = keyword_mod._make_snippet
    fired = {"n": 0}

    def hook(text, terms):
        if fired["n"] == 0:
            fired["n"] += 1
            _deprecate_sop_independent(sop.id)
        return orig(text, terms)

    monkeypatch.setattr(keyword_mod, "_make_snippet", hook)

    fault = _create_fault(client, op_headers, title="CtxStaleSOP 连接池耗尽", error_logs="ERR pool exhausted")
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    assert fired["n"] == 1, "未在检索窗口内触发独立事务变更"
    full_text = "\n".join(m.content for m in adapter.request.messages)
    assert f"[sop:{sop.id}]" not in full_text, "窗口内废弃的旧知识片段进入了模型上下文"
    assert "知识库无匹配" in full_text, "应明示知识库无匹配,不编造来源"


# ===== 第二阶段真实联调复审回归(2026-10-15)=====
# 验收发现 P2-1:合法 JSON 的 SOP 版本格式错误(version:"v2")直接失败、未进入有限修复。


class _ScriptedAdapter:
    """按脚本顺序返回响应;记录每次请求(断言 JSON 模式与修复提示)。"""

    def __init__(self, results):
        self._results = list(results)
        self.requests: list[LLMRequest] = []

    def chat(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return self._results.pop(0)


def _report_with_ref_version(sop_id: int, version) -> dict:
    """带 SOP 引用的最小报告;version 按构造参数(可为 "v1" 字符串制造结构错误)。"""
    return {
        "summary": "摘要",
        "observed_facts": [],
        "suggested_category": None,
        "suggested_tags": [],
        "hypotheses": [],
        "missing_information": [],
        "next_checks": [],
        "references": [
            {
                "ref_id": f"sop:{sop_id}",
                "source_type": "sop",
                "source_id": sop_id,
                "version": version,
                "relevance": "关联",
            }
        ],
        "insufficient_evidence": True,
    }


def _result(text, usage):
    return LLMResult(text=text, usage=usage, model="stub", latency_ms=1)


_FULL_USAGE_A = {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}
_FULL_USAGE_B = {"prompt_tokens": 10, "completion_tokens": 18, "total_tokens": 28}


def test_schema_error_enters_repair_and_recovers(client, op_headers, rev_headers, monkeypatch):
    """P2-1 主场景:首个响应合法 JSON/version="v1"(字符串)→ 修复一次 → version=1 成功。

    共两次调用;格式修复仍使用 JSON 模式;实际 usage 正确求和;来源版本严格校验(与片段一致才保留)。
    """
    fault1 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    sop = _publish_sop(client, op_headers, rev_headers, fault1["id"])
    assert sop["version"] == 1  # 初始发布版本

    adapter = _ScriptedAdapter(
        [
            _result(json.dumps(_report_with_ref_version(sop["id"], "v1")), _FULL_USAGE_A),
            _result(json.dumps(_report_with_ref_version(sop["id"], 1)), _FULL_USAGE_B),
        ]
    )
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)

    fault2 = _create_fault(client, op_headers, title="Redis 连接超时", error_logs="ERR timeout")
    resp = client.post(
        f"/api/faults/{fault2['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "success", analysis
    assert analysis["call_count"] == 2  # 首次 + 一次修复,共两次实际调用
    assert analysis["usage"] == {"prompt_tokens": 15, "completion_tokens": 28, "total_tokens": 43}
    assert len(adapter.requests) == 2
    assert adapter.requests[0].json_mode is True
    assert adapter.requests[1].json_mode is True  # 修复请求继续声明 JSON 模式
    # 来源版本严格校验:修复后 version=1 与当前有效版本一致才保留
    refs = analysis["report"]["references"]
    assert [r["ref_id"] for r in refs] == [f"sop:{sop['id']}"]
    assert refs[0]["version"] == 1 and isinstance(refs[0]["version"], int)


def test_repair_feedback_has_field_path_and_expected_type(client, op_headers, monkeypatch):
    """P2-1:结构错误的修复反馈包含字段路径与期望类型(引导把 "v1" 改成整数)。"""
    adapter = _ScriptedAdapter(
        [
            _result(json.dumps({"summary": "摘要", "references": [
                {"ref_id": "sop:2", "source_type": "sop", "source_id": 2, "version": "v2", "relevance": ""}
            ]}), _FULL_USAGE_A),
            _result(json.dumps(_MINIMAL_REPORT), _FULL_USAGE_B),
        ]
    )
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["analysis"]["call_count"] == 2
    feedback = adapter.requests[1].messages[-1].content
    assert "references.0.version" in feedback  # 字段路径
    assert "整数" in feedback  # 期望类型
    assert "JSON" in feedback  # 修复请求声明 JSON 输出(配合 json_mode)


def test_two_schema_errors_fail_explicitly(client, op_headers, monkeypatch):
    """P2-1:连续两次结构错误明确失败(schema_mismatch),不编造报告。"""
    bad = _result(json.dumps({"summary": "摘要", "references": [
        {"ref_id": "sop:2", "source_type": "sop", "source_id": 2, "version": "v2", "relevance": ""}
    ]}), _FULL_USAGE_A)
    adapter = _ScriptedAdapter([bad, bad])
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "failed"
    assert analysis["error_code"] == "schema_mismatch"
    assert analysis["report"] is None
    assert analysis["call_count"] == 2  # 首次 + 一次修复后放弃
    assert len(adapter.requests) == 2  # 预算耗尽不再请求
    assert analysis["usage"] == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


def test_syntax_then_schema_error_shares_one_budget(client, op_headers, monkeypatch):
    """P2-1:语法错后结构错共享一次修复预算:修复用尽后结构错误不再修复,明确失败。"""
    adapter = _ScriptedAdapter(
        [
            _result("这不是 JSON", _FULL_USAGE_A),
            _result(json.dumps({"summary": "摘要", "references": [
                {"ref_id": "sop:2", "source_type": "sop", "source_id": 2, "version": "v2", "relevance": ""}
            ]}), _FULL_USAGE_B),
        ]
    )
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "failed"
    assert analysis["error_code"] == "schema_mismatch"  # 最后一次问题为结构错误
    assert analysis["call_count"] == 2  # 语法修复已用掉唯一预算,不再有第三次调用
    assert len(adapter.requests) == 2
    assert analysis["usage"] == {"prompt_tokens": 15, "completion_tokens": 28, "total_tokens": 43}


def test_schema_repair_usage_missing_stays_unknown(client, op_headers, monkeypatch):
    """P2-1:结构修复路径上任一次 usage 缺失 → 整次用量未知(null)。"""
    adapter = _ScriptedAdapter(
        [
            _result(json.dumps({"summary": ""}), _FULL_USAGE_A),  # 结构错误(空摘要)
            _result(json.dumps(_MINIMAL_REPORT), None),  # 修复成功但无 usage
        ]
    )
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: adapter)
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201
    analysis = resp.json()["analysis"]
    assert analysis["status"] == "success"
    assert analysis["call_count"] == 2
    assert analysis["usage"] is None  # 缺失不当 0,不沿用首调用值


def test_prompt_metadata_uses_integer_version():
    """P2-1:知识元数据显式整数版本(source_type=sop, source_id=2, version=2),
    不再用 "v2" 展示后缀;系统提示含 SOP "version": 2 示例并明确不能写 "v2"。"""
    from app.ai.prompts import SYSTEM_PROMPT
    from app.ai.schema import EvidenceItem, KnowledgeSnippet
    from app.ai.service import AnalysisInput, _build_user_prompt

    inp = AnalysisInput(title="t", description="d", service="svc")
    evidence = [EvidenceItem(evidence_id="desc", source="description", text="x")]
    snippets = [
        KnowledgeSnippet(
            ref_id="sop:2", source_type="sop", source_id=2, version=2,
            title="T", snippet="S", link="/sops/2", status="published",
        ),
        KnowledgeSnippet(
            ref_id="case:1", source_type="case", source_id=1, version=None,
            title="C", snippet="S", link="/cases/1", status="approved",
        ),
    ]
    prompt = _build_user_prompt(inp, evidence, snippets, [])
    assert "source_type=sop, source_id=2, version=2" in prompt
    assert "source_type=case, source_id=1, version=null" in prompt
    assert " v2" not in prompt, "不得再用展示后缀诱导模型写字符串版本"
    assert '"version": 2' in SYSTEM_PROMPT  # SOP 整数版本示例
    assert '"version": null' in SYSTEM_PROMPT  # 案例 null 示例
    assert '"v2"' in SYSTEM_PROMPT  # 明确不能写 "v2"


def test_reference_version_strict_no_coercion():
    """P2-1:version 不接受 \"v2\" 字符串(不宽松转换为可信引用);
    与后端片段版本不一致的引用移除(严格校验保持)。"""
    from pydantic import ValidationError as PydanticValidationError

    from app.ai.schema import AnalysisReport, KnowledgeReference, KnowledgeSnippet

    with pytest.raises(PydanticValidationError):
        KnowledgeReference(ref_id="sop:2", source_type="sop", source_id=2, version="v2")

    snippets = [
        KnowledgeSnippet(
            ref_id="sop:2", source_type="sop", source_id=2, version=2,
            title="T", snippet="S", link="/sops/2", status="published",
        )
    ]
    report = AnalysisReport(
        summary="s",
        insufficient_evidence=True,
        references=[
            KnowledgeReference(ref_id="sop:2", source_type="sop", source_id=2, version=1)
        ],
    )
    cleaned, reasons = ai_service.verify_report(report, [], snippets, [])
    assert cleaned.references == []  # 版本不符:移除而非采纳
    assert any("版本" in r for r in reasons)
