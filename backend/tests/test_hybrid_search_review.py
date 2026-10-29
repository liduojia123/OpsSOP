"""第三批修复复审回归(P2-1 ~ P2-4 后端部分)。

- P2-1:embedding 等待窗口内失效的来源,不随降级出口返回、不进入聊天请求;
  模型请求发出前的片段复核(独立窗口);
- P2-2:废弃/换版近邻不占候选池(SQL 前置过滤);内容指纹陈旧候选有界补取;
- P2-3:查询向量与聊天分析独立配额;缓存命中不占聊天额度;并发释放;
- P2-4:keyword_only 纯关键词路径零 embedding 调用。

合成向量只验证机制;不调用真实模型。
"""

import pytest

from app.ai.embeddings import EmbeddingError, EmbeddingResult
from app.config import Settings
from app.models import (
    Case,
    CaseStatus,
    Fault,
    FaultStatus,
    Role,
    Sop,
    SopStatus,
    utcnow,
)
from tests.conftest import TestingSessionLocal, make_user
from tests.test_hybrid_search import (
    _MapEmbedAdapter,
    _approve,
    _fp,
    _mk_case,
    _patch_embed,
    _search,
    _seed_state,
    _seed_unit,
    _unit_hash,
    _unit_hash_safe,
    _use_settings,
)
from tests.test_sop_ai_generate import _create_fault, _resolve


def _seed_ready_index(db, adapter, settings=None):
    """种入兼容活动索引(查询向量路径可达)。"""
    _seed_state(db, generation=1, fingerprint=_fp(settings))


# ===== P2-1:降级出口统一来源复核 =====


def test_degraded_exit_revalidates_after_remote_wait(client, op_headers, monkeypatch, db):
    """embedding 远程等待期间来源被废弃后,降级出口不得返回失效关键词命中。"""
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("查询壬特征", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    stable = _mk_case(client, op_headers, db, title="查询壬特征 稳定案例")
    f2 = _create_fault(
        client, op_headers, title="查询壬特征 将废弃", description="查询壬特征", error_logs=""
    )
    _resolve(client, op_headers, f2["id"])
    doomed = [
        c["id"]
        for c in client.get("/api/cases?status=pending_review", headers=op_headers).json()
        if "将废弃" in c["title"]
    ][0]
    _approve(client, db, doomed)
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", stable, _unit_hash(db, "case", stable), v1, _fp())
    _seed_unit(db, 1, "case", doomed, _unit_hash(db, "case", doomed), v1, _fp())

    # 合成钩子:远程等待期间独立事务废弃来源,再抛可重试超时(两次均失败 → 降级)
    def hook(texts):
        adapter.calls.append(list(texts))
        s = TestingSessionLocal()
        try:
            row = s.get(Case, doomed)
            row.status = CaseStatus.returned
            s.commit()
        finally:
            s.close()
        raise EmbeddingError("timeout", "合成超时", retryable=True)

    adapter.embed = hook
    body = _search(client, op_headers, "查询壬特征")
    assert body["retrieval_mode"] == "keyword" and body["degraded"] is True
    assert "失败" in body["degrade_reason"]
    got = {(h["source_type"], h["source_id"]) for h in body["hits"]}
    assert ("case", doomed) not in got  # 等待期间废弃的来源不随降级结果返回
    assert ("case", stable) in got  # 未失效关键词结果仍正常返回并展示降级原因


def test_degraded_search_keeps_stale_knowledge_out_of_model(
    client, op_headers, monkeypatch, db
):
    """同一窗口调用故障分析:已废弃知识片段不得进入聊天请求(复审 P2-1)。"""
    from tests.test_ai_analysis import _CaptureAdapter

    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("查询癸模型", v1)])
    _patch_embed(monkeypatch, adapter)
    cap = _CaptureAdapter()
    s = Settings(
        ai_mode="cloud",
        ai_base_url="http://127.0.0.1:9",
        ai_model="m",
        embedding_mode="cloud",
        embedding_base_url="https://emb.example.com/v1",
        embedding_model="emb-a",
    )
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: s)
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)

    stable = _mk_case(client, op_headers, db, title="查询癸模型 稳定案例")
    f2 = _create_fault(
        client, op_headers, title="查询癸模型 将废弃", description="查询癸模型", error_logs=""
    )
    _resolve(client, op_headers, f2["id"])
    doomed = [
        c["id"]
        for c in client.get("/api/cases?status=pending_review", headers=op_headers).json()
        if "将废弃" in c["title"]
    ][0]
    _approve(client, db, doomed)
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", stable, _unit_hash(db, "case", stable), v1, _fp())
    _seed_unit(db, 1, "case", doomed, _unit_hash(db, "case", doomed), v1, _fp())

    def hook(texts):
        adapter.calls.append(list(texts))
        sess = TestingSessionLocal()
        try:
            row = sess.get(Case, doomed)
            row.status = CaseStatus.returned
            sess.commit()
        finally:
            sess.close()
        raise EmbeddingError("timeout", "合成超时", retryable=True)

    adapter.embed = hook
    fault = _create_fault(client, op_headers, title="查询癸模型 故障", error_logs="")
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    full = "\n".join(m.content for m in cap.request.messages)
    assert f"[case:{doomed}]" not in full  # 失效来源不进入模型上下文
    assert "将废弃" not in full
    assert f"[case:{stable}]" in full  # 仍有效的知识保留


def test_snippets_revalidated_before_model_request(client, op_headers, monkeypatch, db):
    """检索完成后、模型请求前的窗口内失效来源:发请求前复核剔除(独立窗口)。"""
    from app.ai import service as service_mod
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    case_id = _mk_case(client, op_headers, db, title="查询甲窗口 案例内容")

    orig = service_mod._snippets_from_hits

    def hooked(hits):
        snips = orig(hits)
        # 检索已返回、模型请求未发出的窗口:独立事务废弃来源
        s = TestingSessionLocal()
        try:
            row = s.get(Case, case_id)
            row.status = CaseStatus.returned
            s.commit()
        finally:
            s.close()
        return snips

    monkeypatch.setattr(service_mod, "_snippets_from_hits", hooked)
    fault = _create_fault(client, op_headers, title="查询甲窗口", error_logs="")
    resp = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert resp.status_code == 201, resp.text
    full = "\n".join(m.content for m in cap.request.messages)
    assert f"[case:{case_id}]" not in full  # 发出前复核:失效片段不进入提示词
    assert "案例内容" not in full


# ===== P2-2:过滤前移 + 陈旧候选有界补取 =====


def test_deprecated_neighbors_do_not_crowd_out_valid(client, op_headers, monkeypatch, db):
    """20 条废弃近邻(距离 0)不挤掉第 21 条有效答案:状态过滤在 LIMIT 之前。"""
    v_near = [1.0, 0.0, 0.0, 0.0]
    v_far = [0.9, 0.1, 0.05, 0.05]
    adapter = _MapEmbedAdapter([("查询癸特征", v_near)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    owner = make_user(db, "p22_owner", Role.operator)
    deprecated_ids: list[int] = []
    for i in range(20):
        sop = Sop(
            doc_id=f"doc-crowd-{i}",
            version=1,
            title=f"邻挤占废弃记录{i}",
            status=SopStatus.deprecated,
            created_by=owner.id,
            symptom="邻挤占废弃现象",
            check_steps="邻挤占废弃步骤",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(sop)
        db.flush()
        deprecated_ids.append(sop.id)
    valid = Sop(
        doc_id="doc-crowd-valid",
        version=1,
        title="邻挤占有效答案",
        status=SopStatus.published,
        created_by=owner.id,
        symptom="邻挤占有效现象",
        check_steps="邻挤占有效步骤",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(valid)
    db.commit()
    _seed_state(db, generation=1, fingerprint=_fp())
    for sid in deprecated_ids:
        # 废弃来源重建文本为 None(不合规):占位哈希即可,SQL 前置过滤会排除
        _seed_unit(db, 1, "sop", sid, _unit_hash_safe(db, "sop", sid), v_near, _fp(), version=1)
    _seed_unit(db, 1, "sop", valid.id, _unit_hash(db, "sop", valid.id), v_far, _fp(), version=1)

    body = _search(client, op_headers, "查询癸特征")
    assert body["retrieval_mode"] == "hybrid"
    got = {(h["source_type"], h["source_id"]) for h in body["hits"]}
    assert ("sop", valid.id) in got  # 合法近邻不被废弃近邻挤出候选池
    assert all(("sop", sid) not in got for sid in deprecated_ids)


def test_stale_candidates_bounded_refetch(client, op_headers, monkeypatch, db):
    """内容指纹陈旧候选被剔除后有界补取;窗口扩大不超过查询成本上限。"""
    from app.search import hybrid as hybrid_mod

    v_near = [1.0, 0.0, 0.0, 0.0]
    v_far = [0.9, 0.1, 0.05, 0.05]
    adapter = _MapEmbedAdapter([("查询子特征", v_near)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    owner = make_user(db, "p22b_owner", Role.operator)
    stale_ids: list[int] = []
    for i in range(20):
        f = Fault(title=f"补取占位{i}", created_by=owner.id, status=FaultStatus.resolved)
        db.add(f)
        db.flush()
        c = Case(
            title=f"补取陈旧案例{i}",
            symptom="补取陈旧现象",
            environment="t",
            service="s",
            component_version="1",
            confirmed_root_cause="补取旧根因",
            handling_steps="h",
            recovery_verification="r",
            status=CaseStatus.approved,
            created_by=owner.id,
            fault_id=f.id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(c)
        db.flush()
        stale_ids.append(c.id)
    f = Fault(title="补取占位有效", created_by=owner.id, status=FaultStatus.resolved)
    db.add(f)
    db.flush()
    valid = Case(
        title="补取有效案例",
        symptom="补取有效现象",
        environment="t",
        service="s",
        component_version="1",
        confirmed_root_cause="补取有效根因",
        handling_steps="h",
        recovery_verification="r",
        status=CaseStatus.approved,
        created_by=owner.id,
        fault_id=f.id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(valid)
    db.commit()
    _seed_state(db, generation=1, fingerprint=_fp())
    for sid in stale_ids:
        _seed_unit(db, 1, "case", sid, _unit_hash(db, "case", sid), v_near, _fp())
    _seed_unit(db, 1, "case", valid.id, _unit_hash(db, "case", valid.id), v_far, _fp())

    # 内容变化(独立事务):20 条旧向量成为内容指纹陈旧候选,排名仍占前 20
    s2 = TestingSessionLocal()
    try:
        for sid in stale_ids:
            row = s2.get(Case, sid)
            row.confirmed_root_cause = "补取全新根因(与索引文本不一致)"
        s2.commit()
    finally:
        s2.close()

    limits: list[int] = []
    orig_q = hybrid_mod._vector_distance_query

    def counting(db_, vec, generation, limit, user_):
        limits.append(limit)
        return orig_q(db_, vec, generation, limit, user_)

    monkeypatch.setattr(hybrid_mod, "_vector_distance_query", counting)
    body = _search(client, op_headers, "查询子特征")
    assert body["retrieval_mode"] == "hybrid"
    got = {h["source_id"] for h in body["hits"] if h["source_type"] == "case"}
    assert valid.id in got  # 有界补取:第 21 条有效近邻仍被取回
    assert all(sid not in got for sid in stale_ids)
    assert max(limits) <= hybrid_mod.MAX_VECTOR_FETCH  # 查询成本上限仍明确
    assert len(limits) <= 3  # 20 → 40 → 无更多行即停,不反复放大窗口


# ===== P2-3:embedding 与聊天独立配额 =====


def _quota_settings(**overrides):
    base = {
        "ai_mode": "cloud",
        "ai_base_url": "http://127.0.0.1:9",
        "ai_model": "m",
        "embedding_mode": "cloud",
        "embedding_base_url": "https://emb.example.com/v1",
        "embedding_model": "emb-a",
    }
    base.update(overrides)
    return Settings(**base)


def test_search_does_not_consume_chat_quota(client, op_headers, monkeypatch, db):
    """聊天额度 1:两次搜索不占聊天额度;首次合法分析成功,下一次未缓存分析才受限。"""
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("查询甲配额", v1)])
    _patch_embed(monkeypatch, adapter)
    s = _quota_settings(ai_rate_limit_per_minute=1)
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: s)
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    case_id = _mk_case(client, op_headers, db, title="查询甲配额 案例")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())

    b1 = _search(client, op_headers, "查询甲配额")
    b2 = _search(client, op_headers, "查询甲配额")
    assert b1["retrieval_mode"] == "hybrid" and b2["retrieval_mode"] == "hybrid"

    fault = _create_fault(client, op_headers, title="查询甲配额 故障", error_logs="")
    r1 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r1.status_code == 201, r1.text  # 首次合法分析成功(未被自身向量查询挤占)
    r2 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r2.status_code == 429  # 下一次真正的未缓存分析才受限


def test_embed_rate_limit_degrades_search_but_not_chat(client, op_headers, monkeypatch, db):
    """embedding 独立限流:向量限流降级关键词;聊天额度不受影响。"""
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("查询乙配额", v1)])
    _patch_embed(monkeypatch, adapter)
    s = _quota_settings(embedding_rate_limit_per_minute=1)
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: s)
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    case_id = _mk_case(client, op_headers, db, title="查询乙配额 案例")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())

    b1 = _search(client, op_headers, "查询乙配额")
    assert b1["retrieval_mode"] == "hybrid"
    b2 = _search(client, op_headers, "查询乙配额")
    assert b2["retrieval_mode"] == "keyword" and b2["degraded"] is True
    assert "频率限制" in b2["degrade_reason"]  # 单独记录并解释 embedding 限流

    fault = _create_fault(client, op_headers, title="查询乙配额 故障", error_logs="")
    r1 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r1.status_code == 201, r1.text  # 聊天额度未被向量查询消耗


def test_cache_hit_does_not_consume_chat_quota(client, op_headers, monkeypatch, db):
    """缓存命中不占聊天额度;额度 1 时命中缓存仍可用,强制重分析才 429。"""
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    _patch_embed(monkeypatch, _MapEmbedAdapter([]))
    s = _quota_settings(ai_rate_limit_per_minute=1)
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: s)
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    fault = _create_fault(client, op_headers, title="查询丙配额 故障", error_logs="丙")

    r1 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers
    )
    assert r1.status_code == 201 and r1.json()["reused"] is False  # 占用唯一聊天额度
    r2 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": True}, headers=op_headers
    )
    assert r2.status_code == 201 and r2.json()["reused"] is True  # 命中缓存不占额度
    r3 = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r3.status_code == 429  # 强制重分析才受限


def test_embed_slot_released_between_searches(client, op_headers, monkeypatch, db):
    """embedding 并发释放:并发上限 1 时顺序检索逐次获取/释放,不互相卡死。"""
    v1 = [1.0, 0.01, 0.01, 0.01]
    adapter = _MapEmbedAdapter([("查询丁配额", v1)])
    _patch_embed(monkeypatch, adapter)
    s = _quota_settings(embedding_max_concurrent_per_user=1)
    monkeypatch.setattr("app.routers.ai.get_settings", lambda: s)
    case_id = _mk_case(client, op_headers, db, title="查询丁配额 案例")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), v1, _fp())

    for _ in range(3):
        body = _search(client, op_headers, "查询丁配额")
        assert body["retrieval_mode"] == "hybrid"  # 释放后可再次获取
        assert body["degraded"] is False


# ===== P2-6:发模型前片段复核保留查询相关依据 =====


def test_revalidation_preserves_query_matching_sop_instruction(
    client, op_headers, monkeypatch, db
):
    """复审 P2-6 复现:SOP 仅排查步骤命中查询,复核后排查依据不得被现象前缀覆盖。"""
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    owner = make_user(db, "p26_owner", Role.operator)
    sop = Sop(
        doc_id="doc-p26",
        version=1,
        title="后台队列诊断手册",
        status=SopStatus.published,
        created_by=owner.id,
        symptom="业务处理偶发缓慢,具体报错需逐项核对。",
        check_steps="遇到 REVIEW_DIAG_CODE,先核对队列偏移量与提交游标。",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(sop)
    db.commit()

    # 基准:正式搜索命中片段含排查依据
    resp = client.get(
        "/api/search",
        params={"q": "REVIEW_DIAG_CODE", "keyword_only": True},
        headers=op_headers,
    )
    hit = next(h for h in resp.json()["hits"] if h["source_id"] == sop.id)
    assert "队列偏移量" in hit["snippet"]

    # 同查询故障分析:实际聊天请求必须保留该排查依据
    fault = _create_fault(
        client,
        op_headers,
        title="REVIEW_DIAG_CODE 队列异常",
        description="REVIEW_DIAG_CODE 报错",
        error_logs="",
    )
    r = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r.status_code == 201, r.text
    full = "\n".join(m.content for m in cap.request.messages)
    assert f"[sop:{sop.id}]" in full
    assert "队列偏移量" in full  # 查询命中的排查依据保留,不被 symptom 前缀覆盖
    assert "REVIEW_DIAG_CODE" in full


def test_source_snippet_for_query_hits_sop_handling_field(db):
    """只命中 SOP 处理方法字段的场景:按查询重定位到处理依据(非标题/现象)。"""
    from app.search.keyword import source_snippet_for_query

    owner = make_user(db, "p26b_owner", Role.operator)
    sop = Sop(
        doc_id="doc-p26b",
        version=1,
        title="连接故障处置规范",
        status=SopStatus.published,
        created_by=owner.id,
        symptom="连接偶发失败,需按现象逐项核对。",
        check_steps="查看常规监控与日志轮转。",
        handling_steps="按 HANDLER_TOKEN_Y2 执行回滚并切换只读副本。",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(sop)
    db.commit()
    title, snip = source_snippet_for_query(
        db, "sop", sop.id, "HANDLER_TOKEN_Y2", old_snippet=""
    )
    assert title == "连接故障处置规范"
    assert "回滚并切换只读副本" in snip  # 处理依据按查询定位,不是现象前缀


def test_revalidation_preserves_case_root_cause_hit(client, op_headers, monkeypatch, db):
    """只命中案例人工根因/处理字段的场景:复核后保留根因依据。"""
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    owner = make_user(db, "p26c_owner", Role.operator)
    f = Fault(title="p26c占位", created_by=owner.id, status=FaultStatus.resolved)
    db.add(f)
    db.flush()
    case = Case(
        title="线上超时处置记录",
        symptom="偶发超时,需结合监控判断。",
        environment="t",
        service="s",
        component_version="1",
        confirmed_root_cause="RCA_MARKER_X1 导致线程池耗尽",
        handling_steps="按 RCA_MARKER_X1 处置手册扩容并回放积压",
        recovery_verification="r",
        status=CaseStatus.approved,
        created_by=owner.id,
        fault_id=f.id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(case)
    db.commit()

    fault = _create_fault(
        client, op_headers, title="RCA_MARKER_X1 超时", description="RCA_MARKER_X1", error_logs=""
    )
    r = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r.status_code == 201, r.text
    full = "\n".join(m.content for m in cap.request.messages)
    assert f"[case:{case.id}]" in full
    assert "线程池耗尽" in full  # 人工根因依据保留(查询命中字段),不是现象前缀


def test_revalidation_uses_current_content_after_source_change(
    client, op_headers, monkeypatch, db
):
    """检索后、发模型前来源内容变化:采用当前有效内容按查询重定位,不沿用旧正文。"""
    from app.ai import service as service_mod
    from tests.test_ai_analysis import _CaptureAdapter

    cap = _CaptureAdapter()
    monkeypatch.setattr(
        "app.routers.ai.get_settings",
        lambda: Settings(ai_mode="cloud", ai_base_url="http://127.0.0.1:9", ai_model="m"),
    )
    monkeypatch.setattr("app.ai.service.build_adapter", lambda st: cap)
    owner = make_user(db, "p26d_owner", Role.operator)
    sop = Sop(
        doc_id="doc-p26d",
        version=1,
        title="游标核对手册",
        status=SopStatus.published,
        created_by=owner.id,
        symptom="处理延迟异常。",
        check_steps="遇到 REVIEW_DIAG_CODE,核对旧游标。",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(sop)
    db.commit()

    orig = service_mod._snippets_from_hits

    def hooked(hits):
        snips = orig(hits)
        # 窗口内更新来源内容(独立事务;状态仍有效)
        s = TestingSessionLocal()
        try:
            row = s.get(Sop, sop.id)
            row.check_steps = "遇到 REVIEW_DIAG_CODE,核对新游标与积压阈值。"
            s.commit()
        finally:
            s.close()
        return snips

    monkeypatch.setattr(service_mod, "_snippets_from_hits", hooked)
    fault = _create_fault(
        client,
        op_headers,
        title="REVIEW_DIAG_CODE 延迟",
        description="REVIEW_DIAG_CODE",
        error_logs="",
    )
    r = client.post(
        f"/api/faults/{fault['id']}/analyze", json={"use_cache": False}, headers=op_headers
    )
    assert r.status_code == 201, r.text
    full = "\n".join(m.content for m in cap.request.messages)
    assert "新游标" in full  # 当前有效内容按查询重定位
    assert "旧游标" not in full  # 不盲目沿用旧正文


def test_source_snippet_for_query_length_capped(db):
    """片段长度约束保持:超长命中字段截断到上限。"""
    from app.search.keyword import MAX_INDEX_SNIPPET_CHARS, source_snippet_for_query

    owner = make_user(db, "p26e_owner", Role.operator)
    sop = Sop(
        doc_id="doc-p26e",
        version=1,
        title="超长手册",
        status=SopStatus.published,
        created_by=owner.id,
        symptom="s",
        check_steps="LONG_TOKEN_Z3 " + ("长内容" * 2000),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(sop)
    db.commit()
    _t, snip = source_snippet_for_query(db, "sop", sop.id, "LONG_TOKEN_Z3", old_snippet="")
    assert len(snip) <= MAX_INDEX_SNIPPET_CHARS


# ===== P2-4:keyword_only 纯关键词路径 =====


def test_keyword_only_search_zero_embedding_calls(client, op_headers, monkeypatch, db):
    """状态未知时的纯关键词路径:零 embedding 调用/零外发;关键词能力保持。"""
    v1 = [1.0, 0.0, 0.0, 0.0]
    adapter = _MapEmbedAdapter([("查询丑特征", v1)])
    _patch_embed(monkeypatch, adapter)
    _use_settings(monkeypatch)
    case_id = _mk_case(client, op_headers, db, title="查询丑特征 案例")
    _seed_state(db, generation=1, fingerprint=_fp())
    _seed_unit(db, 1, "case", case_id, _unit_hash(db, "case", case_id), [1, 0.01, 0.01, 0.01], _fp())

    resp = client.get(
        "/api/search",
        params={"q": "查询丑特征", "keyword_only": True},
        headers=op_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retrieval_mode"] == "keyword"
    assert body["degraded"] is False  # 受控约束模式,不是向量路径失败
    assert adapter.calls == []  # 零 embedding 调用(不外发查询文本)
    assert any(h["source_id"] == case_id for h in body["hits"])

    # 对照:默认路径在索引可用时走混合(会调用 embedding)
    body2 = _search(client, op_headers, "查询丑特征")
    assert body2["retrieval_mode"] == "hybrid"
    assert len(adapter.calls) >= 1
