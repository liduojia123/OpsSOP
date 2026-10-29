"""第三阶段第一批复审回归(2026-10-15:P1-1 / P2-1 / P2-3 / P2-4)。

按复审「交给开发 AI 的修复顺序」补充有意义的边界回归:
- P1-1 并发首撞/顺序重放他人 token:不返回他人 run 或私有草稿,明确冲突;
- P2-1 原子领取单一执行者;过期接管后旧执行者不留下孤儿草稿、不覆盖新结果;
- P2-3 重试绑定本次输入与模型配置(不沿用首次失败的标识);
- P2-4 字段来源映射持久化可核对;伪造证据 ID 不保留;无来源关键内容标注待人工确认。

并发场景使用独立 SQLAlchemy Session(与生产每请求一会话一致);不调用外部模型。
"""

from datetime import timedelta

import json

from sqlalchemy import select

from app.ai import sop_draft as sop_draft_mod
from app.ai.llm import LLMError, LLMRequest, LLMResult
from app.models import Role
from tests.conftest import TestingSessionLocal, login, make_user
from tests.test_sop_ai_generate import (
    _TOKEN,
    _approved_case,
    _create_fault,
    _draft_json,
    _gen,
    _resolve,
    _result,
    _use_adapter,
    _use_settings,
)


class _ScriptedAdapter:
    def __init__(self, results):
        self._results = list(results)
        self.requests: list[LLMRequest] = []

    def chat(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _other_approved_case(db, client, username="sop_gen_rival"):
    """另一操作员自己的已通过案例:返回 (case_id, headers)。"""
    user = make_user(db, username, Role.operator)
    headers = login(client, username)
    fault = _create_fault(client, headers, title="对方的故障", error_logs="对方日志")
    _resolve(client, headers, fault["id"])
    cases = client.get("/api/cases?status=pending_review", headers=headers).json()
    case_id = cases[0]["id"]
    resp = client.post(
        f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=login(client, "rev_user")
    )
    assert resp.status_code == 200, resp.text
    return case_id, headers, user


# ===== P1-1:所有既有结果返回路径的归属与可见性复核 =====


def test_token_collision_integrity_branch_rejects_others_draft(
    client, op_headers, rev_headers, db, monkeypatch
):
    """并发首撞(IntegrityError 分支)不得返回他人 run/私有草稿,明确 409。"""
    case_a = _approved_case(client, op_headers, rev_headers, title="A 的案例")
    adapter = _ScriptedAdapter([_result(_draft_json(title="PRIVATE_VICTIM_DRAFT"))])
    _use_settings(monkeypatch)
    _use_adapter(monkeypatch, adapter)
    r1 = client.post(f"/api/cases/{case_a}/generate-sop-ai", json=_gen(), headers=op_headers)
    assert r1.status_code == 201
    assert r1.json()["sop"]["title"] == "PRIVATE_VICTIM_DRAFT"

    # B 提交相同 token;人为制造并发窗口:B 的首次查询「未见到」A 的记录
    # → 插入撞唯一约束 → IntegrityError 分支(修复前会在这里返回 A 的草稿)
    case_b, other_headers, _ = _other_approved_case(db, client)
    orig = sop_draft_mod._find_run_by_token
    calls = {"n": 0}

    def fake_find(db_, token):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 竞态窗口:首查不存在
        return orig(db_, token)

    monkeypatch.setattr(sop_draft_mod, "_find_run_by_token", fake_find)
    resp = client.post(f"/api/cases/{case_b}/generate-sop-ai", json=_gen(), headers=other_headers)
    assert resp.status_code == 409, resp.text
    assert "PRIVATE_VICTIM_DRAFT" not in resp.text  # 他人私有草稿不得进入响应
    assert "sop" not in resp.json()

    # 同用户同案例的正常幂等返回保持可用;B 未获得任何 run/草稿
    r2 = client.post(f"/api/cases/{case_a}/generate-sop-ai", json=_gen(), headers=op_headers)
    assert r2.status_code == 201 and r2.json()["reused"] is True
    assert r2.json()["sop"]["title"] == "PRIVATE_VICTIM_DRAFT"
    assert client.get(f"/api/cases/{case_b}/sop-generations", headers=other_headers).json() == []


def test_cross_user_token_replay_conflict(client, op_headers, rev_headers, db, monkeypatch):
    """顺序重放他人 token 同样 409,响应不含他人 run/SOP。"""
    case_a = _approved_case(client, op_headers, rev_headers, title="A 的案例")
    adapter = _ScriptedAdapter([_result(_draft_json(title="PRIVATE_VICTIM_DRAFT"))])
    _use_settings(monkeypatch)
    _use_adapter(monkeypatch, adapter)
    client.post(f"/api/cases/{case_a}/generate-sop-ai", json=_gen(), headers=op_headers)

    case_b, other_headers, _ = _other_approved_case(db, client)
    resp = client.post(f"/api/cases/{case_b}/generate-sop-ai", json=_gen(), headers=other_headers)
    assert resp.status_code == 409
    assert "PRIVATE_VICTIM_DRAFT" not in resp.text
    assert "sop" not in resp.json()


# ===== P2-1:原子领取与结果提交的唯一执行者 =====


def test_concurrent_claim_single_executor(db):
    """failed 记录的原子领取:独立会话并发领取至多一个成功。"""
    from app.models import Case, Fault, SopAiGenerationRun, SopGenStatus, utcnow

    owner = make_user(db, "claim_owner", Role.operator)
    fault = Fault(title="claim 故障", created_by=owner.id)
    db.add(fault)
    db.flush()
    case = Case(fault_id=fault.id, title="claim 案例", created_by=owner.id)
    db.add(case)
    db.flush()
    run = SopAiGenerationRun(
        case_id=case.id,
        created_by=owner.id,
        request_token="claim-token-1",
        attempt=1,
        input_hash="h" * 8,
        status=SopGenStatus.failed.value,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(run)
    db.commit()

    other = TestingSessionLocal()
    try:
        a1 = sop_draft_mod._claim_run(db, run.id, "h" * 8, {}, "cfg-2", False)
        a2 = sop_draft_mod._claim_run(other, run.id, "h" * 8, {}, "cfg-2", False)
    finally:
        other.close()
    assert a1 == 2  # 首次领取 attempt+1
    assert a2 is None  # 并发领取失败:至多一个执行者


def test_takeover_blocks_old_executor_draft(client, op_headers, rev_headers, monkeypatch):
    """过期接管后,旧执行者整体丢弃:不留下孤儿草稿、不覆盖新结果,返回可识别冲突。"""
    case_id = _approved_case(client, op_headers, rev_headers)

    class _TakeoverAdapter:
        def __init__(self, data):
            self.data = data

        def chat(self, request):
            # 生成期间:独立会话将记录老化并领取(attempt+1),模拟过期接管
            s = TestingSessionLocal()
            try:
                row = s.execute(
                    select(sop_draft_mod.SopAiGenerationRun).where(
                        sop_draft_mod.SopAiGenerationRun.request_token == _TOKEN
                    )
                ).scalar_one()
                row.updated_at = sop_draft_mod.utcnow() - timedelta(
                    seconds=sop_draft_mod.STALE_RUNNING_SECONDS * 2
                )
                s.commit()
                taken = sop_draft_mod._claim_run(s, row.id, "h" * 8, {}, "cfg-x", False)
                assert taken is not None, "接管领取应成功"
            finally:
                s.close()
            return _result(self.data)

    adapter = _TakeoverAdapter(_draft_json(title="旧执行者的草稿"))
    _use_settings(monkeypatch)
    _use_adapter(monkeypatch, adapter)

    resp = client.post(f"/api/cases/{case_id}/generate-sop-ai", json=_gen(), headers=op_headers)
    assert resp.status_code == 409, resp.text  # 被接管:可识别冲突,不返回旧执行者结果
    sops = client.get("/api/sops?status=draft", headers=op_headers).json()
    assert sops == []  # 旧执行者的草稿被整体丢弃(rollback),不留下孤儿草稿
    runs = client.get(f"/api/cases/{case_id}/sop-generations", headers=op_headers).json()
    assert runs[0]["status"] == "running"  # 接管者的执行仍在途(本测试不再完成它)
    assert runs[0]["attempt"] == 2


# ===== P2-3:重试绑定本次输入与模型配置 =====


def test_retry_rebinds_config_and_input(client, op_headers, rev_headers, db, monkeypatch):
    """同 token 重试后,运行记录绑定本次执行的输入与模型配置(不沿用首次失败的标识)。"""
    from app.models import Fault

    case_id = _approved_case(client, op_headers, rev_headers)
    adapter = _ScriptedAdapter([LLMError("timeout", "模型调用超时(测试)")])
    _use_settings(monkeypatch, ai_model="model-a")
    _use_adapter(monkeypatch, adapter)
    r1 = client.post(f"/api/cases/{case_id}/generate-sop-ai", json=_gen(), headers=op_headers)
    run1 = r1.json()["run"]
    assert run1["status"] == "failed"
    cfg_a = run1["model_config_version"]
    hash_a = run1["input_hash"]
    assert cfg_a  # 本次配置有实际标识

    # 来源证据变化 + 模型配置切换后,同 token 重试成功
    fault_id = client.get(f"/api/cases/{case_id}", headers=op_headers).json()["fault_id"]
    fault = db.get(Fault, fault_id)
    fault.error_logs = fault.error_logs + "\n重试前新增的证据"
    db.commit()

    adapter2 = _ScriptedAdapter([_result(_draft_json())])
    _use_settings(monkeypatch, ai_model="model-b")
    _use_adapter(monkeypatch, adapter2)
    r2 = client.post(f"/api/cases/{case_id}/generate-sop-ai", json=_gen(), headers=op_headers)
    run2 = r2.json()["run"]
    assert run2["status"] == "success"
    assert run2["id"] == run1["id"]  # 原地更新,同一运行记录
    assert run2["attempt"] == 2  # 第二次执行领取
    assert run2["model_config_version"] != cfg_a  # 绑定本次配置,不沿用首次失败的配置
    assert run2["input_hash"] != hash_a  # 绑定本次输入快照
    # is_demo 与本次配置一致(不出现 demo 标识与旧配置并存的矛盾)
    assert run2["is_demo"] is False


# ===== P2-4:字段来源映射持久化与人工核对 =====


def test_field_sources_persisted_and_reviewable(client, op_headers, rev_headers, monkeypatch):
    """字段来源映射持久化:响应、生成记录与草稿 notes 均可核对;伪造证据 ID 不保留。"""
    case_id = _approved_case(client, op_headers, rev_headers)
    adapter = _ScriptedAdapter([
        _result(_draft_json(sources=[
            {"target": "handling_steps", "evidence_ids": ["case.handling_steps"]},
            {"target": "recovery_verification", "evidence_ids": ["case.recovery_verification"]},
            {"target": "applicable_env", "evidence_ids": ["case.applicable_env"]},
            {"target": "symptom", "evidence_ids": ["case.symptom", "fake:99"]},
        ]))
    ])
    _use_settings(monkeypatch)
    _use_adapter(monkeypatch, adapter)
    resp = client.post(f"/api/cases/{case_id}/generate-sop-ai", json=_gen(), headers=op_headers)
    run = resp.json()["run"]
    assert run["status"] == "success"

    mapping = {s["target"]: s["evidence_ids"] for s in run["field_sources"]}
    assert mapping["handling_steps"] == ["case.handling_steps"]
    assert mapping["symptom"] == ["case.symptom"]  # fake:99 被移除
    assert all("fake:99" not in ids for ids in mapping.values())
    # 来源展示只含证据 ID 标识,不携带来源材料正文(不借展示读取受限材料)
    assert "limits.memory" not in json.dumps(run["field_sources"], ensure_ascii=False)

    # 草稿 notes 持久可核对(刷新/重新进入草稿仍可见);不泄露来源正文
    notes = resp.json()["sop"]["notes"]
    assert "字段来源依据" in notes
    assert "handling_steps ← case.handling_steps" in notes

    # 重新读取生成记录仍可核对(不依赖单次响应)
    runs = client.get(f"/api/cases/{case_id}/sop-generations", headers=op_headers).json()
    assert runs[0]["field_sources"] == run["field_sources"]


def test_key_content_without_source_marked_for_manual_review(
    client, op_headers, rev_headers, monkeypatch
):
    """无有效来源映射的关键内容明确「待人工确认」,不把全局证据清单当字段来源映射。"""
    case_id = _approved_case(client, op_headers, rev_headers)
    adapter = _ScriptedAdapter([_result(_draft_json(sources=[]))])
    _use_settings(monkeypatch)
    _use_adapter(monkeypatch, adapter)
    resp = client.post(f"/api/cases/{case_id}/generate-sop-ai", json=_gen(), headers=op_headers)
    run = resp.json()["run"]
    assert run["status"] == "success"
    assert run["field_sources"] == []
    assert any("无有效来源" in r for r in run["incomplete_reasons"])
    sop = resp.json()["sop"]
    assert "待人工确认" in sop["handling_steps"]
    assert "待人工确认" in sop["recovery_verification"]
    assert "(无有效来源,待人工确认)" in sop["notes"]
