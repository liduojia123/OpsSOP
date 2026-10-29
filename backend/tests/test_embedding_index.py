"""embedding 索引构建与管理测试(第三阶段第二批)。

覆盖验收场景:disabled 零调用、管理接口鉴权、local-only/代理隔离/失败不切云、
脱敏、坏向量不产生可用索引、仅合规来源纳入、构建期间来源变化、重复构建不重复记录、
换模型/换维度/构建期间配置切换不混用、屏障并发领取单一执行者与接管保护、
重建失败不激活半成品与恢复流程、前两阶段回归不破坏(全量另行运行)。

合成向量(确定性伪向量)只验证协议/存储/状态机制,不代表真实语义检索能力。
"""

import threading

import pytest

from app.ai.embeddings import EmbeddingError, EmbeddingResult
from app.config import Settings
from app.models import Case, EmbeddingIndexUnit
from tests.conftest import TestingSessionLocal
from tests.test_sop_ai_generate import (
    _approved_case,
    _create_fault,
    _resolve,
)


# ---- 合成适配器(仅测试;不代表真实模型) ----


class _SyntheticAdapter:
    """确定性伪向量:记录调用次数与文本;可注入失败/钩子(竞争窗口控制)。"""

    def __init__(self, dims=8, fail=None, hook=None, usage={"prompt_tokens": 3, "total_tokens": 3}):
        self.dims = dims
        self.fail = fail  # EmbeddingError 实例(构造后每次调用抛出)
        self.hook = hook  # callable(call_index);每批 embed 时调用(屏障/变更注入)
        self.calls: list[list[str]] = []
        self.usage = usage

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.hook:
            self.hook(len(self.calls))
        if self.fail is not None:
            raise self.fail
        vectors = [
            [float((hash(t) >> s) % 97 + 1) for s in range(self.dims)] for t in texts
        ]
        return EmbeddingResult(
            vectors=vectors, usage=self.usage, model="synthetic-test", dims=self.dims
        )

    def describe(self):
        return {"mode": "synthetic-test"}


def _settings(**overrides):
    base = {
        "embedding_mode": "cloud",
        "embedding_base_url": "https://emb.example.com/v1",
        "embedding_model": "emb-a",
    }
    base.update(overrides)
    return Settings(**base)


def _use_settings(monkeypatch, **overrides):
    s = _settings(**overrides)
    monkeypatch.setattr("app.routers.admin_embedding.get_settings", lambda: s)
    return s


def _status(client, headers):
    resp = client.get("/api/admin/embedding-index", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _build(client, headers, path="build"):
    return client.post(f"/api/admin/embedding-index/{path}", headers=headers)


# ===== 1. disabled:零 embedding 调用,基本业务与关键词检索正常 =====


def test_disabled_zero_calls_and_business_unaffected(client, op_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch, embedding_mode="disabled", embedding_base_url="", embedding_model="")

    snap = _status(client, adm_headers)
    assert snap["status"] == "unconfigured"
    resp = _build(client, adm_headers)
    assert resp.status_code == 400  # 明确拒绝,不静默生成伪向量
    assert adapter.calls == []  # 零 embedding 调用

    # 基本业务与关键词检索不受影响
    fault = _create_fault(client, op_headers)
    assert fault["id"] > 0
    search = client.get("/api/search?q=OOMKilled", headers=op_headers)
    assert search.status_code == 200
    assert search.json()["hits"] == []  # 如实空结果


# ===== 2. 管理接口鉴权 =====


def test_admin_only_status_and_build(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)

    for headers in (op_headers, rev_headers):
        assert client.get("/api/admin/embedding-index", headers=headers).status_code == 403
        assert _build(client, headers).status_code == 403
        assert _build(client, headers, "rebuild").status_code == 403
    assert adapter.calls == []

    snap = _status(client, adm_headers)
    assert snap["status"] == "pending"
    assert snap["config_fingerprint"] and "sk" not in snap["config_fingerprint"]


# ===== 3. local-only 拒 cloud;local 不经环境代理;失败不切云 =====


def test_local_only_rejects_cloud_on_build_path(client, adm_headers, monkeypatch):
    # 不注入合成适配器:生产路径构造真实适配器,LOCAL_ONLY+cloud 在构造时明确拒绝
    _use_settings(
        monkeypatch,
        embedding_mode="cloud",
        embedding_base_url="https://emb.example.com/v1",
        embedding_model="emb-a",
        local_only=True,
    )
    resp = _build(client, adm_headers)
    assert resp.status_code == 400
    assert "LOCAL_ONLY" in resp.json()["detail"]


def test_failure_does_not_switch_service(client, adm_headers, monkeypatch):
    """embedding 失败不自动切换另一服务或云端:只产生对同一适配器的有限重试。"""
    adapter = _SyntheticAdapter(fail=EmbeddingError("timeout", "超时", retryable=True))
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch, embedding_mode="local", embedding_base_url="http://10.1.2.3:8000")
    _approved_case(client, adm_headers, adm_headers)  # 至少一个可索引来源
    resp = _build(client, adm_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "failed"
    assert body["status"]["status"] == "failed"
    assert body["embedded_calls"] == 2  # 首次 + 有限重试一次,不再有第三次/切换服务
    assert len(adapter.calls) == 2
    assert body["status"]["error_code"] == "timeout"


def test_real_429_fails_without_immediate_resend(client, adm_headers, monkeypatch):
    """真实适配器的 429 映射:只发出一次请求即失败,不立即重发同一批次。"""
    from app.ai.embeddings import OpenAICompatEmbeddingAdapter

    posts = {"n": 0}

    class _RateLimitedClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            posts["n"] += 1
            return type("_Resp", (), {"status_code": 429})()

    monkeypatch.setattr("app.ai.embeddings.httpx.Client", _RateLimitedClient)
    _use_settings(monkeypatch)
    _approved_case(client, adm_headers, adm_headers)  # 至少一个可索引来源
    adapter = OpenAICompatEmbeddingAdapter(
        base_url="https://emb.example.com/v1",
        model="emb-a",
        api_key="",
        timeout_seconds=1,
    )
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)

    body = _build(client, adm_headers).json()
    assert body["result"] == "failed"
    assert body["status"]["error_code"] == "rate_limited"
    assert body["embedded_calls"] == 1
    assert posts["n"] == 1  # 429 后不立即重发(复审 2026-10-19 P2)
    assert body["status"]["status"] == "failed"


# ===== 4. 脱敏:索引文本与最终请求不残留凭据 =====


def test_index_text_sanitized_before_embedding(client, adm_headers, monkeypatch):
    class _CaptureAdapter(_SyntheticAdapter):
        captured_texts: list[str] = []

        def embed(self, texts):
            _CaptureAdapter.captured_texts.extend(texts)
            return super().embed(texts)

    adapter = _CaptureAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(
        client,
        adm_headers,
        adm_headers,
        title="password=IdxSecretA1! 的故障",
        error_logs="log token=IdxSecretB2!",
    )
    resp = _build(client, adm_headers)
    assert resp.json()["result"] in ("success", "partial")
    joined = "\n".join(_CaptureAdapter.captured_texts)
    assert "IdxSecretA1!" not in joined
    assert "IdxSecretB2!" not in joined
    assert "[REDACTED]" in joined


# ===== 5. 坏向量不产生可用索引 =====


@pytest.mark.parametrize(
    "fail",
    [
        EmbeddingError("bad_response", "embedding 返回条数与输入不一致(输入 1,返回 2)"),
        EmbeddingError("bad_response", "embedding 维度不一致(期望 8,第 0 条为 4)"),
        EmbeddingError("bad_response", "embedding 第 0 条含非有限值"),
        EmbeddingError("upstream", "embedding 服务错误(HTTP 502)", retryable=False),
    ],
)
def test_bad_vectors_do_not_activate_index(client, adm_headers, monkeypatch, fail):
    adapter = _SyntheticAdapter(fail=fail)
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(client, adm_headers, adm_headers)
    resp = _build(client, adm_headers)
    body = resp.json()
    assert body["result"] == "failed"
    assert body["status"]["status"] == "failed"
    assert body["status"]["active_generation"] == 0  # 半成品未激活
    rows = client.get("/api/admin/embedding-index", headers=adm_headers).json()
    assert rows["total_rows"] == 0  # 半成品已清理,无坏索引行


def test_mixed_dims_across_batches_rejected(client, adm_headers, monkeypatch):
    """维度不一致(批间)同样失败,不留下混维索引。"""
    class _MixedDimsAdapter(_SyntheticAdapter):
        def embed(self, texts):
            self.calls.append(list(texts))
            dims = self.dims if len(self.calls) == 1 else self.dims + 4
            vectors = [[1.0] * dims for _ in texts]
            return EmbeddingResult(vectors=vectors, usage=None, model="synthetic", dims=dims)

    adapter = _MixedDimsAdapter(dims=8)
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch, embedding_batch_size=1)
    for i in range(2):
        _approved_case(client, adm_headers, adm_headers, title=f"案例 {i}")
    resp = _build(client, adm_headers)
    assert resp.json()["result"] == "failed"
    assert resp.json()["status"]["total_rows"] == 0


# ===== 6. 仅合规状态来源纳入 =====


def test_only_compliant_sources_indexed(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)

    approved_case = _approved_case(client, op_headers, rev_headers, title="已通过案例")
    # 待审核案例
    f2 = _create_fault(client, op_headers, title="待审案例来源")
    _resolve(client, op_headers, f2["id"])
    # 退回案例
    f3 = _create_fault(client, op_headers, title="退回案例来源")
    _resolve(client, op_headers, f3["id"])
    pending = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    for c in pending:
        if c["title"] in ("待审案例来源", "退回案例来源"):
            client.post(f"/api/cases/{c['id']}/return", json={"comment": "补证"}, headers=rev_headers)

    # 已发布 SOP(经完整流程)+ 私有草稿 SOP
    from tests.test_ai_analysis import _publish_sop

    f4 = _create_fault(client, op_headers, title="SOP 来源故障", error_logs="ERR sop")
    sop = _publish_sop(client, op_headers, rev_headers, f4["id"])
    draft_resp = client.post(f"/api/cases/{approved_case}/generate-sop", headers=op_headers)
    assert draft_resp.status_code == 201, draft_resp.text
    draft_id = draft_resp.json()["id"]

    resp = _build(client, adm_headers)
    assert resp.json()["result"] in ("success", "partial")
    body = _status(client, adm_headers)
    assert body["unit_count"] >= 2

    rows = TestingSessionLocal()
    try:
        units = rows.query(EmbeddingIndexUnit).all()
    finally:
        rows.close()
    case_ids = {u.source_id for u in units if u.source_type == "case"}
    sop_ids = {u.source_id for u in units if u.source_type == "sop"}
    assert approved_case in case_ids
    assert sop["id"] in sop_ids
    assert draft_id not in sop_ids  # 私有草稿 SOP 不纳入
    # 待审/退回案例不得纳入
    for c in pending:
        if c["title"] in ("待审案例来源", "退回案例来源"):
            assert c["id"] not in case_ids


# ===== 7. 构建期间来源变化/废弃:旧结果不落为有效索引 =====


def test_source_changed_during_build_skipped(client, op_headers, rev_headers, adm_headers, monkeypatch):
    from tests.conftest import TestingSessionLocal as TSL

    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    case_a = _approved_case(client, op_headers, rev_headers, title="保持不变的案例")
    case_b = _approved_case(client, op_headers, rev_headers, title="构建中被改写的案例")

    def hook(call_index):
        s = TSL()
        try:
            row = s.get(Case, case_b)
            row.confirmed_root_cause = row.confirmed_root_cause + "(构建期间改写)"
            s.commit()
        finally:
            s.close()

    adapter.hook = hook
    resp = _build(client, adm_headers)
    body = resp.json()
    assert body["result"] == "partial"  # 部分成功必须明示覆盖范围
    assert any(f"case:{case_b}" in s for s in body["skipped"])
    assert body["status"]["error_code"] == "partial"
    rows = TestingSessionLocal()
    try:
        units = rows.query(EmbeddingIndexUnit).filter_by(source_type="case").all()
        indexed = {u.source_id for u in units}
    finally:
        rows.close()
    assert case_a in indexed
    assert case_b not in indexed  # 变化后的旧结果不得落为有效索引


# ===== 8. 重复构建不重复记录;内容变化正确更新 =====


def test_repeat_build_reuses_vectors_and_content_change_reembeds(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(client, op_headers, rev_headers, title="重复构建案例")

    r1 = _build(client, adm_headers).json()
    assert r1["result"] == "success" and r1["embedded_calls"] == 1
    calls_after_first = sum(len(c) for c in adapter.calls)

    # 同模型同内容重复构建:向量复用,不重复记录,不重复计费
    r2 = _build(client, adm_headers).json()
    assert r2["embedded_calls"] == 0
    assert r2["reused_units"] == r1["built_units"]
    assert r2["status"]["total_rows"] == r1["built_units"]  # 不随构建次数累积
    assert sum(len(c) for c in adapter.calls) == calls_after_first

    # 内容变化:重新嵌入并更新内容指纹
    f = client.get("/api/cases?status=approved", headers=op_headers).json()[0]
    rows = TestingSessionLocal()
    try:
        row = rows.get(Case, f["id"])
        row.handling_steps = row.handling_steps + "\n新增处理步骤"
        rows.commit()
    finally:
        rows.close()
    r3 = _build(client, adm_headers).json()
    assert r3["embedded_calls"] == 1  # 变化内容重新嵌入
    assert r3["status"]["total_rows"] == r3["built_units"]  # 旧代次清理,不重复


# ===== 9. 换模型/换维度/构建期间配置切换:向量不混用 =====


def test_model_switch_marks_needs_rebuild_and_new_build_uses_new_fingerprint(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch, embedding_model="emb-a")
    _approved_case(client, op_headers, rev_headers)
    r1 = _build(client, adm_headers).json()
    assert r1["result"] == "success"
    fp_a = r1["status"]["config_fingerprint"]

    # 同维度换模型:明确提示需要重建,不静默混用
    _use_settings(monkeypatch, embedding_model="emb-b")
    snap = _status(client, adm_headers)
    assert snap["status"] == "needs_rebuild"
    assert snap["active_model"] == "emb-a"  # 旧代次保留但不冒充当前配置

    # 重建后全部单元绑定新指纹
    r2 = _build(client, adm_headers, "rebuild").json()
    assert r2["result"] == "success"
    assert r2["status"]["active_model"] == "emb-b"
    assert r2["status"]["config_fingerprint"] != fp_a
    rows = TestingSessionLocal()
    try:
        units = rows.query(EmbeddingIndexUnit).all()
        assert units and all(u.config_fingerprint == r2["status"]["config_fingerprint"] for u in units)
    finally:
        rows.close()


def test_dims_change_needs_rebuild(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter(dims=8)
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    settings = _use_settings(monkeypatch)
    _approved_case(client, op_headers, rev_headers)
    assert _build(client, adm_headers).json()["status"]["embedding_dims"] == 8

    # 同模型名但维度变化(服务端行为变化):文本版本/维度记录使旧代次可识别
    adapter.dims = 16
    settings.embedding_base_url = "https://emb2.example.com/v1"  # 端点变化 → 指纹变化
    snap = _status(client, adm_headers)
    assert snap["status"] == "needs_rebuild"
    r2 = _build(client, adm_headers, "rebuild").json()
    assert r2["status"]["embedding_dims"] == 16


def test_config_switch_mid_build_result_rejected(client, op_headers, rev_headers, adm_headers, monkeypatch):
    """构建期间配置切换:旧任务不得提交为当前有效结果(指纹条件校验)。"""
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    settings = _use_settings(monkeypatch, embedding_model="emb-a")
    _approved_case(client, op_headers, rev_headers)

    def hook(call_index):
        settings.embedding_model = "emb-switched"  # 构建期间配置切换

    adapter.hook = hook
    resp = _build(client, adm_headers)
    assert resp.status_code == 409  # 可识别冲突,结果未提交
    snap = _status(client, adm_headers)
    assert snap["active_generation"] == 0
    assert snap["total_rows"] == 0  # 半成品已清理


# ===== 10. 屏障并发领取单一执行者;接管后旧执行者不能覆盖新代次 =====


def test_barrier_concurrent_claim_single_executor(db):
    """两个独立会话 + 屏障同时领取:至多一个执行者(真实并发竞争窗口)。"""
    from app.search.embedding_index import _claim_build

    barrier = threading.Barrier(2, timeout=10)
    results: list = [None, None]
    errors: list = [None, None]

    def worker(idx):
        session = TestingSessionLocal()
        try:
            barrier.wait()
            results[idx] = _claim_build(session, _settings())
        except Exception as exc:  # 记录异常,断言在下方
            errors[idx] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert errors == [None, None], errors
    claims = [r for r in results if r is not None]
    assert len(claims) == 1  # 恰好一个执行者
    assert claims[0].attempt == 1 and claims[0].generation == 1


def test_takeover_blocks_old_executor_commit(client, op_headers, rev_headers, adm_headers, monkeypatch):
    """过期接管后,旧执行者的半成品被清理且不能覆盖新代次(条件提交保护)。"""
    from datetime import timedelta

    from app.models import EmbeddingIndexState, utcnow
    from app.search.embedding_index import _claim_build

    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(client, op_headers, rev_headers)

    def hook(call_index):
        s = TestingSessionLocal()
        try:
            state = s.get(EmbeddingIndexState, 1)
            state.updated_at = utcnow() - timedelta(seconds=31 * 60)  # 老化为过期执行者
            s.commit()
            taken = _claim_build(s, _settings())  # 新任务接管(attempt+1)
            assert taken is not None and taken.attempt == 2
        finally:
            s.close()

    adapter.hook = hook
    resp = _build(client, adm_headers)
    assert resp.status_code == 409  # 旧执行者结果不得提交

    snap = _status(client, adm_headers)
    assert snap["total_rows"] == 0  # 旧执行者半成品已清理,无孤儿行
    assert snap["build_attempt"] == 2
    assert snap["active_generation"] == 0  # 新代次未被旧任务污染

    # 接管任务随后可正常完成(恢复流程)
    adapter.hook = None
    # 接管任务仍是 building 状态:需新一次构建(过期接管由领取条件处理)
    s = TestingSessionLocal()
    try:
        state = s.get(EmbeddingIndexState, 1)
        state.updated_at = utcnow() - timedelta(seconds=31 * 60)
        s.commit()
    finally:
        s.close()
    r = _build(client, adm_headers)
    assert r.status_code == 200 and r.json()["result"] == "success"


# ===== 11. 重建失败不激活半成品;可按明确流程恢复 =====


def test_rebuild_failure_keeps_compatible_old_generation(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(client, op_headers, rev_headers)
    r1 = _build(client, adm_headers).json()
    assert r1["result"] == "success"
    gen1 = r1["status"]["active_generation"]

    # 内容变化使重建必须实际调用(全复用时不会走模型,也就无从失败)
    f = client.get("/api/cases?status=approved", headers=op_headers).json()[0]
    s = TestingSessionLocal()
    try:
        row = s.get(Case, f["id"])
        row.handling_steps = row.handling_steps + "\n重建前的变更"
        s.commit()
    finally:
        s.close()

    # 重建失败:半成品不激活;同配置兼容旧代次保留可用,失败原因可见
    adapter.fail = EmbeddingError("bad_response", "合成向量维度不一致")
    r2 = _build(client, adm_headers, "rebuild").json()
    assert r2["result"] == "failed"
    snap = r2["status"]
    assert snap["active_generation"] == gen1  # 旧代次保留
    assert snap["status"] == "ready"  # 兼容旧代次仍可用
    assert "维度不一致" in snap["error_message"]
    assert snap["total_rows"] == snap["unit_count"]  # 无半成品残留

    # 明确恢复流程:重新构建成功后新代次激活
    adapter.fail = None
    r3 = _build(client, adm_headers, "rebuild").json()
    assert r3["result"] == "success"
    assert r3["status"]["active_generation"] != gen1
    assert r3["status"]["error_code"] == ""


def test_rebuild_failure_with_incompatible_config_marks_needs_rebuild(client, op_headers, rev_headers, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch, embedding_model="emb-a")
    _approved_case(client, op_headers, rev_headers)
    assert _build(client, adm_headers).json()["result"] == "success"

    _use_settings(monkeypatch, embedding_model="emb-b")
    adapter.fail = EmbeddingError("timeout", "超时", retryable=False)
    r = _build(client, adm_headers, "rebuild").json()
    assert r["result"] == "failed"
    assert r["status"]["status"] == "needs_rebuild"  # 不兼容旧代次不冒充可用


def test_interrupted_build_recoverable(client, op_headers, rev_headers, adm_headers, monkeypatch, db):
    """中断的构建执行者:超时后按失败回收并可重新构建(明确恢复策略)。"""
    from datetime import timedelta

    from app.models import EmbeddingIndexState, utcnow

    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(client, op_headers, rev_headers)

    state = db.get(EmbeddingIndexState, 1)
    if state is None:
        _status(client, adm_headers)  # 触发初始化
        state = db.get(EmbeddingIndexState, 1)
    state.status = "building"
    state.building_generation = 1
    state.build_attempt = 1
    state.updated_at = utcnow() - timedelta(seconds=31 * 60)  # 模拟崩溃残留
    db.commit()

    snap = _status(client, adm_headers)
    assert snap["status"] == "failed"
    assert snap["error_code"] == "interrupted"
    r = _build(client, adm_headers).json()
    assert r["result"] == "success"  # 恢复:重新构建成功


# ===== 12. 状态查询不触发模型调用 / 启动不自动构建 =====


def test_status_query_never_calls_model(client, adm_headers, monkeypatch):
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    for _ in range(3):
        _status(client, adm_headers)
    assert adapter.calls == []


# ===== 第二批复审回归(2026-10-15,P2-1/P2-2/P2-3)=====


def _seed_stale_building(db, attempt=1, generation=1):
    """播种一个过期 building 记录(模拟崩溃残留执行者)。"""
    from datetime import timedelta

    from app.models import EmbeddingIndexState, utcnow

    state = db.get(EmbeddingIndexState, 1)
    if state is None:
        state = EmbeddingIndexState(id=1)
        db.add(state)
    state.status = "building"
    state.build_attempt = attempt
    state.building_generation = generation
    state.build_fingerprint = ""
    state.updated_at = utcnow() - timedelta(seconds=31 * 60)
    db.commit()


def test_p21_get_status_recovery_does_not_cancel_new_claimer(client, adm_headers, db, monkeypatch):
    """P2-1:迟到的状态查询不得取消新领取者(条件回收未命中后重读,不覆盖 building)。

    复现审查屏障时序:旧 GET 读取过期 building 后、写入前,新 POST 原子接管(attempt=2);
    旧 GET 的回收写入必须落空,新领取者租约完好、可正常提交;后续 POST 返回冲突。
    """
    from app.search import embedding_index as ei

    _use_settings(monkeypatch)  # API 状态查询经 get_settings;需处于已配置 embedding 状态
    _seed_stale_building(db, attempt=1, generation=1)
    claim_box = {}
    orig = ei._recover_if_stale

    def hooked(db_, settings_, obs_attempt, obs_gen):
        # 模拟屏障窗口:旧 GET 读取后、写入前,新领取者接管
        s = TestingSessionLocal()
        try:
            claim_box["claim"] = ei._claim_build(s, _settings())
            assert claim_box["claim"] is not None
        finally:
            s.close()
        return orig(db_, settings_, obs_attempt, obs_gen)

    monkeypatch.setattr(ei, "_recover_if_stale", hooked)
    snap = client.get("/api/admin/embedding-index", headers=adm_headers).json()

    # 新领取者未被取消:attempt=2 仍在 building,未被标记 interrupted
    assert snap["build_attempt"] == 2
    assert snap["status"] == "building"
    view = ei._read_state(db)
    assert view.status == "building" and view.build_attempt == 2
    assert view.error_code != "interrupted"

    # 后续 POST 返回冲突(在途不得重复领取);新领取者租约完好、可正常提交
    assert _build(client, adm_headers).status_code == 409
    s2 = TestingSessionLocal()
    try:
        assert ei._finalize_success(s2, claim_box["claim"], unit_count=1, dims=8) is True
    finally:
        s2.close()
    snap2 = client.get("/api/admin/embedding-index", headers=adm_headers).json()
    assert snap2["status"] == "ready" and snap2["active_generation"] == 2


def test_p22_force_rebuild_reembeds_same_content_same_config(client, op_headers, rev_headers, adm_headers, monkeypatch):
    """P2-2:强制重建绕过向量复用——同端点/同模型/同内容也实际重新嵌入并刷新维度。"""
    adapter = _SyntheticAdapter(dims=8)
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    _use_settings(monkeypatch)
    _approved_case(client, op_headers, rev_headers)
    r1 = _build(client, adm_headers).json()
    assert r1["result"] == "success" and r1["status"]["embedding_dims"] == 8

    # 端点/模型名/内容全部不变,远端实际返回维度变为 16
    adapter.dims = 16
    r2 = _build(client, adm_headers, "rebuild").json()
    assert r2["embedded_calls"] == 1  # 强制重建实际重新嵌入(刷新同名模型的旧向量)
    assert r2["reused_units"] == 0
    assert r2["status"]["embedding_dims"] == 16

    # 普通重复 build 仍零调用复用(强制语义只属于 rebuild)
    r3 = _build(client, adm_headers).json()
    assert r3["embedded_calls"] == 0 and r3["reused_units"] == 1


def test_p22_cli_rebuild_flag_forces_reembed(monkeypatch):
    """P2-2:CLI --rebuild 与 API 一致,把强制语义贯通到构建层。"""
    import importlib.util
    import pathlib
    import sys as _sys

    from app.search.embedding_index import BuildOutcome

    spec = importlib.util.spec_from_file_location(
        "build_embedding_index_cli",
        pathlib.Path(__file__).resolve().parent.parent / "scripts" / "build_embedding_index.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    def fake_run_build(db_, settings_, force=False):
        captured["force"] = force
        return BuildOutcome(result="success")

    monkeypatch.setattr("app.search.embedding_index.run_build", fake_run_build)
    monkeypatch.setattr("app.search.embedding_index.get_status", lambda db_, s: {"status": "ready"})
    monkeypatch.setattr("app.database.SessionLocal", lambda: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr("app.config.get_settings", lambda: _settings())

    monkeypatch.setattr(_sys, "argv", ["build_embedding_index.py", "--rebuild"])
    assert mod.main() == 0
    assert captured["force"] is True

    monkeypatch.setattr(_sys, "argv", ["build_embedding_index.py"])
    assert mod.main() == 0
    assert captured["force"] is False


def test_p23_config_switch_exits_and_allows_immediate_rebuild(client, op_headers, rev_headers, adm_headers, monkeypatch):
    """P2-3:配置冲突退出必须结束自己的租约,新配置立即可重建(不遗留 building 阻塞)。"""
    adapter = _SyntheticAdapter()
    monkeypatch.setattr("app.search.embedding_index.build_embedding_adapter", lambda s: adapter)
    settings = _use_settings(monkeypatch, embedding_model="emb-a")
    _approved_case(client, op_headers, rev_headers)

    def hook(call_index):
        settings.embedding_model = "emb-switched"

    adapter.hook = hook
    resp = _build(client, adm_headers)
    assert resp.status_code == 409  # 配置切换:结果不提交

    # 租约已结束:不再 building,安全失败原因可见
    snap = _status(client, adm_headers)
    assert snap["status"] != "building"
    assert snap["error_code"] == "config_switch"
    assert snap["total_rows"] == 0  # 半成品已清理

    # 新配置立即重建成功(不必等过期接管时限)
    adapter.hook = None
    r2 = _build(client, adm_headers).json()
    assert r2["result"] == "success"
    assert r2["status"]["active_model"] == "emb-switched"


def test_p23_failure_finalize_does_not_release_taker_lease(db):
    """P2-3:已被接管时,旧执行者的失败收尾不得修改新执行者的租约状态。"""
    from app.search.embedding_index import _claim_build, _finalize_failure

    s1 = TestingSessionLocal()
    s2 = TestingSessionLocal()
    try:
        claim1 = _claim_build(s1, _settings())
        assert claim1 is not None and claim1.attempt == 1
        # 同会话内领取需要记录过期才能被接管:直接领取第二次前先老化
        from datetime import timedelta

        from app.models import EmbeddingIndexState, utcnow

        row = s2.get(EmbeddingIndexState, 1)
        row.updated_at = utcnow() - timedelta(seconds=31 * 60)
        s2.commit()
        claim2 = _claim_build(s2, _settings())
        assert claim2 is not None and claim2.attempt == 2

        # 旧执行者失败收尾:返回 False,新执行者租约保持 building
        assert _finalize_failure(s1, claim1, "config_switch", "旧任务退出") is False
        row = s2.get(EmbeddingIndexState, 1)
        s2.refresh(row)
        assert row.status == "building" and row.build_attempt == 2
    finally:
        s1.close()
        s2.close()
