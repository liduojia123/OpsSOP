"""向量检索、混合检索与可靠性测试(第三阶段第三批)。

覆盖验收:disabled/未配置/无兼容索引降级零调用、失败降级不丢查询、local-only/脱敏、
pgvector 距离查询与 RRF 融合、关键词与向量双路排除不合规来源、陈旧索引识别、
SOP 版本规则、检索期间来源失效与代次切换、缓存失效条件。

合成向量(映射式)只验证机制与排序口径,不代表真实语义检索质量;不调用真实模型。
"""

import hashlib

import pytest

from app.ai.embeddings import EmbeddingError, EmbeddingResult
from app.config import Settings
from app.models import (
    Case,
    CaseStatus,
    EmbeddingIndexState,
    EmbeddingIndexUnit,
    Role,
    Sop,
    SopStatus,
    User,
    utcnow,
)
from app.search.hybrid import MAX_RESULTS, search_knowledge_hybrid
from app.search.index_text import config_fingerprint
from tests.conftest import TestingSessionLocal, login, make_user
from tests.test_sop_ai_generate import _approved_case, _create_fault, _resolve


@pytest.fixture(autouse=True)
def _reset_ai_state():
    """清理进程内限流/并发状态(聊天与 embedding 独立桶),避免测试互相干扰。"""
    from app.ai import service as ai_service

    with ai_service._rate_lock:
        ai_service._rate_buckets.clear()
        ai_service._inflight.clear()
        ai_service._embed_rate_buckets.clear()
        ai_service._embed_inflight.clear()
    yield
    with ai_service._rate_lock:
        ai_service._rate_buckets.clear()
        ai_service._inflight.clear()
        ai_service._embed_rate_buckets.clear()
        ai_service._embed_inflight.clear()


# ---- 合成适配器 ----


class _MapEmbedAdapter:
    """合成语义映射:文本命中子串规则即返回对应向量(可模拟改写问法的语义匹配)。"""

    def __init__(self, rules):
        self.rules = list(rules)  # [(substring, vector)]
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        vecs = []
        for t in texts:
            v = [0.01, 0.01, 0.01, 0.01]
            for sub, vec in self.rules:
                if sub in t:
                    v = list(vec)
                    break
            vecs.append(v)
        return EmbeddingResult(
            vectors=vecs, usage={"prompt_tokens": 1, "total_tokens": 1}, model="synthetic", dims=4
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


def _fp(settings=None):
    s = settings or _settings()
    return config_fingerprint(s.embedding_mode, s.embedding_base_url, s.embedding_model)


def _search(client, headers, q):
    resp = client.get("/api/search", params={"q": q}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_state(db, generation, fingerprint, dims=4):
    state = db.get(EmbeddingIndexState, 1)
    if state is None:
        state = EmbeddingIndexState(id=1)
        db.add(state)
    state.status = "ready"
    state.active_generation = generation
    state.active_fingerprint = fingerprint
    state.active_model = "emb-a"
    state.active_dims = dims
    state.active_text_version = "t1"
    state.unit_count = 1
    state.building_generation = generation
    state.build_attempt = 1
    state.build_fingerprint = fingerprint
    state.error_code = ""
    state.error_message = ""
    state.updated_at = utcnow()
    db.commit()
    return state


def _seed_unit(db, generation, source_type, source_id, content_hash, vector, fingerprint, dims=4, version=None):
    db.add(
        EmbeddingIndexUnit(
            generation=generation,
            source_type=source_type,
            source_id=source_id,
            source_version=version,
            unit_key="doc",
            content_hash=content_hash,
            text_excerpt="",
            config_fingerprint=fingerprint,
            embedding_dims=dims,
            embedding=list(vector),
        )
    )
    db.commit()


def _unit_hash(db, source_type, source_id, limit=4000) -> str:
    from app.search.index_text import _unit_text_now

    text = _unit_text_now(db, source_type, source_id, limit)
    assert text is not None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit_hash_safe(db, source_type, source_id, limit=4000) -> str:
    """不合规/不可见来源返回占位哈希(最终由业务校验排除)。"""
    from app.search.index_text import _unit_text_now

    text = _unit_text_now(db, source_type, source_id, limit)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "0" * 64


def _use_settings(monkeypatch, **overrides):
    s = _settings(**overrides)
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: s)
    monkeypatch.setattr("app.routers.admin_embedding.get_settings", lambda: s)
    return s


def _patch_embed(monkeypatch, adapter):
    monkeypatch.setattr("app.search.hybrid.build_embedding_adapter", lambda s: adapter)
    return adapter


def _op_user(db):
    return db.query(User).filter(User.role == Role.operator).first()


def _approve(client, db, case_id):
    """用独立审核员通过案例(operator 不能自审)。"""
    if not db.query(User).filter(User.username == "hy_rev").first():
        make_user(db, "hy_rev", Role.reviewer)
    rev = login(client, "hy_rev")
    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=rev)
    assert resp.status_code == 200, resp.text


def _mk_case(client, op_headers, db, **overrides) -> int:
    """创建并审批案例(返回案例 ID)。"""
    fault = _create_fault(client, op_headers, **overrides)
    _resolve(client, op_headers, fault["id"])
    cases = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    title = overrides.get("title")
    if title:
        case_id = next(c["id"] for c in cases if c["title"] == title)
    else:
        case_id = cases[0]["id"]
    _approve(client, db, case_id)
    return case_id


# ===== 1. disabled / 未配置 / 无兼容索引:关键词正常,零 embedding 调用 =====


def test_disabled_search_keyword_only_zero_calls(client, op_headers, monkeypatch, db):
    adapter = _MapEmbedAdapter([])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch, embedding_mode="disabled", embedding_base_url="", embedding_model="")
    _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    body = _search(client, op_headers, "OOMKilled")
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is True
    assert "未启用" in body["degrade_reason"]
    assert adapter.calls == []
    assert any("OOMKilled" in h["title"] for h in body["hits"])  # 关键词能力保持


def test_index_not_built_degrades_without_embedding_call(client, op_headers, monkeypatch, db):
    adapter = _MapEmbedAdapter([])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    body = _search(client, op_headers, "OOMKilled")
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is True
    assert "构建" in body["degrade_reason"]
    assert adapter.calls == []  # 无兼容索引零调用;搜索不自动重建索引


def test_incompatible_fingerprint_degrades(client, op_headers, monkeypatch, db):
    adapter = _MapEmbedAdapter([])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    # 索引存在但由不同模型构建(相同维度不同模型也不混用)
    _seed_state(db, generation=1, fingerprint="other-model-fp1")
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0, 0, 0], "other-model-fp1")
    body = _search(client, op_headers, "OOMKilled")
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is True
    assert adapter.calls == []  # 指纹不匹配在生成查询向量之前即降级


# ===== 2. 失败降级:不丢查询/业务输入 =====


@pytest.mark.parametrize(
    "code,message",
    [
        ("timeout", "查询向量生成失败(timeout)"),
        ("rate_limited", "embedding 服务限流(HTTP 429)"),
        ("bad_response", "embedding 返回条数与输入不一致(输入 1,返回 2)"),
    ],
)
def test_embedding_failures_degrade_keyword(client, op_headers, monkeypatch, db, code, message):
    adapter = _MapEmbedAdapter([])

    def failing(texts):
        adapter.calls.append(list(texts))
        raise EmbeddingError(code, message)

    adapter.embed = failing
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0, 0, 0], _fp())
    body = _search(client, op_headers, "OOMKilled")
    assert body["query"] == "OOMKilled"  # 查询不丢
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is True
    assert "失败" in body["degrade_reason"]
    assert any("OOMKilled" in h["title"] for h in body["hits"])  # 业务结果仍可用


def test_query_dims_mismatch_degrades(client, op_headers, monkeypatch, db):
    adapter = _MapEmbedAdapter([])
    adapter.embed = lambda texts: EmbeddingResult(
        vectors=[[1.0] * 6 for _ in texts], usage=None, model="synthetic", dims=6
    )
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    _seed_state(db, generation=1, fingerprint=_fp(), dims=4)
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0, 0, 0], _fp())
    body = _search(client, op_headers, "OOMKilled")
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is True
    assert "维度" in body["degrade_reason"]


# ===== 3. local-only / 失败不切云 / 最终请求脱敏 =====


def test_local_only_cloud_degrades_without_calls(client, op_headers, monkeypatch, db):
    from app.ai.embeddings import build_embedding_adapter as real_build

    constructed = {"n": 0}

    def counting_build(s):
        constructed["n"] += 1
        return real_build(s)  # LOCAL_ONLY + cloud:构造即拒

    monkeypatch.setattr("app.search.hybrid.build_embedding_adapter", counting_build)
    _use_settings(monkeypatch, local_only=True)  # cloud + LOCAL_ONLY
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0, 0, 0], _fp())
    body = _search(client, op_headers, "OOMKilled")
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is True
    assert "配置" in body["degrade_reason"]
    assert constructed["n"] == 1  # 构造即拒;零 embedding 调用


def test_query_embedding_failure_does_not_switch_service(client, op_headers, monkeypatch, db):
    """local 失败不切云:只对同一适配器有限重试一次,随后降级关键词。"""
    adapter = _MapEmbedAdapter([])
    calls = {"n": 0}

    def failing(texts):
        calls["n"] += 1
        raise EmbeddingError("timeout", "本地 embedding 超时", retryable=True)

    adapter.embed = failing
    _patch_embed(monkeypatch, adapter)
    s = _use_settings(monkeypatch, embedding_mode="local", embedding_base_url="http://10.1.2.3:8000")
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    fp_local = config_fingerprint(s.embedding_mode, s.embedding_base_url, s.embedding_model)
    _seed_state(db, generation=1, fingerprint=fp_local)
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0, 0, 0], fp_local)
    body = _search(client, op_headers, "OOMKilled")
    assert body["retrieval_mode"] == "keyword" and body["degraded"] is True
    assert calls["n"] == 2  # 首次 + 有限重试一次;不出现第三次/切换服务


def test_real_429_degrades_without_immediate_resend(client, op_headers, monkeypatch, db):
    """真实适配器的 429 映射:只发出一次请求即降级关键词(不立即重发)。"""
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
    settings = _use_settings(monkeypatch)
    adapter = OpenAICompatEmbeddingAdapter(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        api_key="",
        timeout_seconds=1,
    )
    monkeypatch.setattr("app.search.hybrid.build_embedding_adapter", lambda s: adapter)
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0, 0, 0], _fp())

    body = _search(client, op_headers, "OOMKilled")
    assert posts["n"] == 1  # 429 后不立即重发(复审 2026-10-19 P2)
    assert body["retrieval_mode"] == "keyword" and body["degraded"] is True
    assert "rate_limited" in body["degrade_reason"]
    assert any("OOMKilled" in h["title"] for h in body["hits"])  # 关键词结果仍可用


def test_query_text_sanitized_at_final_request(client, op_headers, monkeypatch, db):
    from tests.test_embeddings import _FakeClient, _FakeResp, _ok_payload

    _FakeClient.captured = {}
    _FakeClient.response = _FakeResp(payload=_ok_payload(n=1, dims=4))
    monkeypatch.setattr("app.ai.embeddings.httpx.Client", _FakeClient)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    _use_settings(monkeypatch)
    case_id = _mk_case(client, op_headers, db, title="Pod 频繁 OOMKilled")
    _seed_state(db, generation=1, fingerprint=_fp(), dims=4)
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1.0, 0.01, 0.01, 0.01], _fp())
    body = _search(client, op_headers, "password=QuerySecretZ9! OOMKilled")
    req_body = str(_FakeClient.captured.get("body"))
    assert req_body != "None"  # 查询确实到达 embedding 最终请求
    assert "QuerySecretZ9!" not in req_body
    assert "[REDACTED]" in req_body
    assert body["query"] == "password=QuerySecretZ9! OOMKilled"  # 原查询如实回显


# ===== 4. pgvector 距离查询 + RRF 融合;去重与 top_k 稳定 =====


def test_vector_only_paraphrase_found_via_fusion(client, op_headers, monkeypatch, db):
    """改写问法(无关键词重合)经向量命中并进入融合结果;纯关键词检索不到。"""
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("连接池耗尽", v1), ("缓存服务响应慢", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    # 文档不含查询词(缓存/服务/响应/慢),关键词路径检索不到
    fault = _create_fault(
        client,
        op_headers,
        title="应用告警处理记录甲",
        description="现象:应用报错连接池耗尽,重启后恢复",
        error_logs="",
        environment="",
        service="",
        component_version="",
        recent_changes="",
        attempted_actions="",
    )
    _resolve(client, op_headers, fault["id"])
    cases = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    case_id = cases[0]["id"]
    _approve(client, db, case_id)

    # 无索引时(纯关键词)检索不到:改写问法无关键词重合
    body0 = _search(client, op_headers, "缓存服务响应慢")
    assert body0["retrieval_mode"] == "keyword"
    assert all(h["source_id"] != case_id for h in body0["hits"])

    # 种入索引后,混合检索经向量命中
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())
    body = _search(client, op_headers, "缓存服务响应慢")
    assert body["retrieval_mode"] == "hybrid"
    assert body["degraded"] is False
    assert any(h["source_id"] == case_id for h in body["hits"])

    # 融合去重:同一来源只出现一次
    keys = [(h["source_type"], h["source_id"]) for h in body["hits"]]
    assert len(keys) == len(set(keys))


def test_fusion_dedup_and_topk_stable(client, op_headers, monkeypatch, db):
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("共性现象词根", v1), ("共性查询问法", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    ids = []
    for i in range(12):
        fault = _create_fault(
            client,
            op_headers,
            title=f"共性现象词根 案例 {i}",
            description=f"描述 {i} 含共性现象词根",
            error_logs="",
            service="",
            environment="",
            component_version="",
            recent_changes="",
            attempted_actions="",
        )
        _resolve(client, op_headers, fault["id"])
    pending = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    for c in pending:
        _approve(client, db, c["id"])
        ids.append(c["id"])
    _seed_state(db, generation=1, fingerprint=_fp())
    for cid in ids:
        _seed_unit(db, 1, "case", cid, _unit_hash(db, "case", cid), v1, _fp())

    body1 = _search(client, op_headers, "共性查询问法")
    body2 = _search(client, op_headers, "共性查询问法")
    assert body1["retrieval_mode"] == "hybrid"
    assert len(body1["hits"]) == MAX_RESULTS  # top_k 上限
    keys1 = [(h["source_type"], h["source_id"], h["version"]) for h in body1["hits"]]
    keys2 = [(h["source_type"], h["source_id"], h["version"]) for h in body2["hits"]]
    assert keys1 == keys2  # 稳定排序
    assert len(set(keys1)) == len(keys1)  # 去重


def test_vector_order_follows_pgvector_distance(client, op_headers, monkeypatch, db):
    near = [1.0, 0.0, 0.0, 0.0]
    far = [1.0, 1.0, 0.0, 0.0]
    adapter = _MapEmbedAdapter([("查询短语", near), ("完全贴合近邻", near), ("部分相关远端", far)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    for title, desc in (("标题近邻甲", "完全贴合近邻"), ("标题远端乙", "部分相关远端")):
        fault = _create_fault(
            client, op_headers, title=title, description=desc, error_logs="", service="",
            environment="", component_version="", recent_changes="", attempted_actions="",
        )
        _resolve(client, op_headers, fault["id"])
    pending = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    id_near = id_far = None
    for c in pending:
        _approve(client, db, c['id'])
        if "近邻" in c["title"]:
            id_near = c["id"]
        else:
            id_far = c["id"]
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", id_near, _unit_hash(db, "case", id_near), near, _fp())
    _seed_unit(db, 1, "case", id_far, _unit_hash(db, "case", id_far), far, _fp())

    body = _search(client, op_headers, "查询短语")
    ordered = [h["source_id"] for h in body["hits"] if h["source_type"] == "case"]
    assert ordered.index(id_near) < ordered.index(id_far)  # 数据库距离排序:近邻在前


# ===== 5. 关键词与向量双路排除不合规来源 =====


def test_both_paths_exclude_non_compliant_sources(client, op_headers, rev_headers, monkeypatch, db):
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("特征词甲", v1), ("查询特征词", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    approved = _mk_case(client, op_headers, db, title="特征词甲 已通过")
    # 待审核案例
    f2 = _create_fault(client, op_headers, title="特征词甲 待审", description="特征词甲", error_logs="")
    _resolve(client, op_headers, f2["id"])
    pending_id = [
        c["id"]
        for c in client.get("/api/cases?status=pending_review", headers=op_headers).json()
        if "待审" in c["title"]
    ][0]
    # 私有草稿 SOP 与废弃 SOP
    draft = client.post(f"/api/cases/{approved}/generate-sop", headers=op_headers).json()
    owner = make_user(db, "excl_owner", Role.operator)
    deprecated_sop = Sop(
        doc_id="doc-x", version=1, title="特征词甲 废弃SOP", status=SopStatus.deprecated,
        created_by=owner.id, symptom="特征词甲",
    )
    db.add(deprecated_sop)
    db.commit()

    # 种入包含不合规来源的索引行(模拟索引未更新;最终由业务校验排除)
    _seed_state(db, generation=1, fingerprint=_fp())
    for sid, st in (
        (approved, "case"),
        (pending_id, "case"),
        (draft["id"], "sop"),
        (deprecated_sop.id, "sop"),
    ):
        _seed_unit(db, 1, st, sid, _unit_hash_safe(db, st, sid), v1, _fp())

    body = _search(client, op_headers, "查询特征词")
    assert body["retrieval_mode"] == "hybrid"
    got = {(h["source_type"], h["source_id"]) for h in body["hits"]}
    assert ("case", approved) in got  # 合规来源可出现
    assert ("case", pending_id) not in got  # 待审案例双路排除
    assert ("sop", draft["id"]) not in got  # 私有草稿不进入
    assert ("sop", deprecated_sop.id) not in got  # 废弃 SOP 不进入

    # 业务校验本身独立生效:不合规来源的命中被返回前复核剔除
    from app.search.keyword import SearchHit, finalize_hits

    direct = finalize_hits(
        [
            SearchHit(
                ref_id=f"case:{pending_id}", source_type="case", source_id=pending_id,
                version=None, title="t", snippet="s", link="", status="pending_review", score=1.0,
            ),
            SearchHit(
                ref_id=f"sop:{deprecated_sop.id}", source_type="sop", source_id=deprecated_sop.id,
                version=1, title="t", snippet="s", link="", status="deprecated", score=1.0,
            ),
        ],
        db,
        _op_user(db),
    )
    assert direct == []


# ===== 6/7. 索引未更新时废弃来源 / 内容变化后旧向量识别 =====


def test_deprecated_source_excluded_even_if_index_stale(client, op_headers, rev_headers, monkeypatch, db):
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("特征词乙", v1), ("查询乙", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    f1 = _create_fault(client, op_headers, title="特征词乙 案例", description="特征词乙", error_logs="")
    _resolve(client, op_headers, f1["id"])
    case_id = [
        c["id"]
        for c in client.get("/api/cases?status=pending_review", headers=op_headers).json()
        if "特征词乙" in c["title"]
    ][0]
    _approve(client, db, case_id)
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())

    # 索引未更新,来源被改回非正式状态(独立事务)
    s = TestingSessionLocal()
    try:
        row = s.get(Case, case_id)
        row.status = CaseStatus.returned
        s.commit()
    finally:
        s.close()
    body = _search(client, op_headers, "查询乙")
    assert all(h["source_id"] != case_id for h in body["hits"])  # 索引状态不能替代业务校验

    # 助手共同路径同样不得使用
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: cap)
    fault = _create_fault(client, op_headers, title="查询乙", error_logs="")
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201
    full = "\n".join(m.content for m in cap.request.messages)
    assert f"[case:{case_id}]" not in full  # 已失效来源不进入助手知识片段


def test_changed_content_old_vector_not_used(client, op_headers, monkeypatch, db):
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("特征词丙", v1), ("查询丙", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    f1 = _create_fault(client, op_headers, title="特征词丙 案例", description="特征词丙", error_logs="")
    _resolve(client, op_headers, f1["id"])
    case_id = [
        c["id"]
        for c in client.get("/api/cases?status=pending_review", headers=op_headers).json()
        if "特征词丙" in c["title"]
    ][0]
    _approve(client, db, case_id)
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())

    # 内容变化(索引未重建):旧向量不得作为当前内容的有效依据
    s = TestingSessionLocal()
    try:
        row = s.get(Case, case_id)
        row.confirmed_root_cause = "全新的根因内容(与索引文本不一致)"
        s.commit()
    finally:
        s.close()
    body = _search(client, op_headers, "查询丙")  # 仅向量可命中的问法
    assert all(h["source_id"] != case_id for h in body["hits"])  # 陈旧向量候选被剔除


# ===== 8. SOP 版本:草稿不替换旧发布;新版本发布后仅新版本为依据 =====


def test_sop_version_rules_in_search(client, op_headers, rev_headers, monkeypatch, db):
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("特征词丁", v1), ("查询丁", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    from tests.test_ai_analysis import _publish_sop

    f1 = _create_fault(client, op_headers, title="特征词丁 故障", error_logs="ERR 特征词丁")
    sop = _publish_sop(client, op_headers, rev_headers, f1["id"])
    assert sop["version"] == 1

    # 私有草稿(独立 doc)不替换仍有效的已发布 v1
    draft = client.post(f"/api/cases/{sop['source_case_id']}/generate-sop", headers=op_headers)
    assert draft.status_code == 201
    body = _search(client, op_headers, "特征词丁")
    sop_hits = [h for h in body["hits"] if h["source_type"] == "sop"]
    assert any(h["source_id"] == sop["id"] and h["version"] == 1 for h in sop_hits)
    assert all(h["source_id"] != draft.json()["id"] for h in sop_hits)  # 草稿不进入

    # 换版发布后:仅新有效版本作为依据(旧 v1 superseded)
    v1_hash = _unit_hash(db, "sop", sop["id"])
    s = TestingSessionLocal()
    try:
        row = s.get(Sop, sop["id"])
        row.status = SopStatus.superseded
        s.commit()
        v2 = Sop(
            doc_id=row.doc_id, version=2, title="特征词丁 故障处理SOP v2",
            status=SopStatus.published, created_by=row.created_by,
            symptom="特征词丁", source_case_id=row.source_case_id,
        )
        s.add(v2)
        s.commit()
        v2_id = v2.id
    finally:
        s.close()
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "sop", v2_id, _unit_hash(db, "sop", v2_id), v1, _fp(), version=2)
    _seed_unit(db, 1, "sop", sop["id"], v1_hash, v1, _fp(), version=1)  # 旧版本向量行仍在
    body = _search(client, op_headers, "查询丁")
    sop_ids = {h["source_id"] for h in body["hits"] if h["source_type"] == "sop"}
    assert v2_id in sop_ids
    assert sop["id"] not in sop_ids  # 历史版本不再是正式依据


# ===== 9. 检索期间来源失效 / 代次切换 =====


def test_source_invalidated_mid_search_excluded(client, op_headers, rev_headers, monkeypatch, db):
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("特征词戊", v1), ("查询戊", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    approved = _mk_case(client, op_headers, db, title="特征词戊 稳定")
    f2 = _create_fault(client, op_headers, title="特征词戊 将失效", description="特征词戊", error_logs="")
    _resolve(client, op_headers, f2["id"])
    doomed = [
        c["id"]
        for c in client.get("/api/cases?status=pending_review", headers=op_headers).json()
        if "将失效" in c["title"]
    ][0]
    _approve(client, db, doomed)
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", approved, _unit_hash(db, "case", approved), v1, _fp())
    _seed_unit(db, 1, "case", doomed, _unit_hash(db, "case", doomed), v1, _fp())

    # 查询向量生成期间(检索窗口)独立事务废弃来源
    def hook(texts):
        adapter.calls.append(list(texts))
        s = TestingSessionLocal()
        try:
            row = s.get(Case, doomed)
            row.status = CaseStatus.returned
            s.commit()
        finally:
            s.close()
        return EmbeddingResult(vectors=[list(v1) for _ in texts], usage=None, model="synthetic", dims=4)

    adapter.embed = hook
    body = _search(client, op_headers, "查询戊")
    got = {h["source_id"] for h in body["hits"] if h["source_type"] == "case"}
    assert approved in got
    assert doomed not in got  # 窗口内失效的来源不返回


def test_generation_switch_mid_query_no_mix(client, op_headers, monkeypatch, db):
    """查询期间重建切换代次:重读新代次(不重复 embedding 调用),不混用旧代次候选。"""
    from app.search import hybrid as hybrid_mod

    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("特征词己", v1), ("查询己", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    case_id = _mk_case(client, op_headers, db, title="特征词己 案例", description="特征词己")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())

    orig_query = hybrid_mod._vector_distance_query
    state = {"n": 0}

    def hooked_query(db_, vec, generation, limit, user_):
        state["n"] += 1
        if state["n"] == 1:
            # 窗口内重建切换:旧代次被清理,新代次(同指纹)生效
            s = TestingSessionLocal()
            try:
                s.query(EmbeddingIndexUnit).filter_by(generation=1).delete()
                row = s.get(EmbeddingIndexState, 1)
                row.active_generation = 2
                row.building_generation = 2
                s.add(
                    EmbeddingIndexUnit(
                        generation=2, source_type="case", source_id=case_id, source_version=None,
                        unit_key="doc", content_hash=_unit_hash(db_, "case", case_id), text_excerpt="",
                        config_fingerprint=_fp(), embedding_dims=4, embedding=list(v1),
                    )
                )
                s.commit()
            finally:
                s.close()
            return []  # 旧代次查询为空 → 触发重读
        return orig_query(db_, vec, generation, limit, user_)

    monkeypatch.setattr(hybrid_mod, "_vector_distance_query", hooked_query)
    body = _search(client, op_headers, "查询己")
    assert body["retrieval_mode"] == "hybrid"
    assert any(h["source_id"] == case_id for h in body["hits"])  # 使用新代次,不混用
    assert state["n"] == 2  # 仅重读一次


# ===== 10. 缓存失效条件:检索策略/索引代次变化进入缓存键 =====


def test_analysis_cache_invalidates_on_retrieval_and_generation_change(client, op_headers, monkeypatch, db):
    """检索策略/活动代次变化使分析缓存失效;策略稳定后命中恢复。"""
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    _patch_embed(monkeypatch, _MapEmbedAdapter([]))
    base_kwargs = {
        "ai_mode": "cloud",
        "ai_base_url": "http://127.0.0.1:9",
        "ai_model": "m",
        # embedding 已配置:索引从无到有 = 检索策略变化(keyword → hybrid)
        "embedding_mode": "cloud",
        "embedding_base_url": "https://emb.example.com/v1",
        "embedding_model": "emb-a",
    }
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: Settings(**base_kwargs))
    monkeypatch.setattr("app.ai.service.build_adapter", lambda s: cap)
    case_id = _mk_case(client, op_headers, db, title="缓存案例", description="缓存内容")

    fault = _create_fault(client, op_headers, title="缓存验证", error_logs="")
    r1 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r1.status_code == 201 and r1.json()["reused"] is False
    r2 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r2.json()["reused"] is True  # 同输入/知识/配置/检索策略:命中缓存

    # 检索策略变化(索引可用 → hybrid):缓存键失效
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1.0, 0.01, 0.01, 0.01], _fp())
    r3 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r3.json()["reused"] is False
    r4 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r4.json()["reused"] is True  # 策略稳定后再次命中

    # 活动索引代次变化:缓存键失效
    _seed_state(db, generation=2, fingerprint=_fp())
    _seed_unit(db, 2, "case", case_id, _unit_hash(db, "case", case_id), [1.0, 0.01, 0.01, 0.01], _fp())
    r5 = client.post(f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers)
    assert r5.json()["reused"] is False
    assert r5.json()["analysis"]["id"] not in (r1.json()["analysis"]["id"], r3.json()["analysis"]["id"])
