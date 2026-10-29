"""评测脚本契约测试(第三批修复复审 P2-5)。

- 真实评测入口必须构建真实来源索引(强制重建;合成与真实不混用);
- 索引构建失败/查询降级时,混合指标标为未验证,退出状态可识别;
- 空结果误报按真实返回结果计(任何命中均计误报);
- --setup 参数真实生效(缺夹具时要求先 setup)。

全部为 mock/离线契约探针,不调用真实模型。
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.search.hybrid import RetrievalResult
from app.search.keyword import SearchHit

_EVAL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_retrieval.py"
_spec = importlib.util.spec_from_file_location("eval_retrieval", _EVAL_PATH)
eval_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_mod)


def _hit(ref: str, source_type: str, source_id: int) -> SearchHit:
    return SearchHit(
        ref_id=ref,
        source_type=source_type,
        source_id=source_id,
        version=1 if source_type == "sop" else None,
        title="t",
        snippet="s",
        link="",
        status="published" if source_type == "sop" else "approved",
        score=1.0,
    )


def _settings(**overrides):
    base = {
        "embedding_mode": "cloud",
        "embedding_base_url": "https://emb.example.com/v1",
        "embedding_model": "emb-a",
    }
    base.update(overrides)
    return Settings(**base)


def _patch_search(monkeypatch, hybrid_fn):
    kw_hits = [_hit("case:1", "case", 1)]
    monkeypatch.setattr(
        "app.search.keyword.search_knowledge", lambda db, user, q: (list(kw_hits), "v")
    )
    monkeypatch.setattr("app.search.hybrid.search_knowledge_hybrid", hybrid_fn)


# ===== 降级不汇总成混合指标;退出状态可识别 =====


def test_all_degraded_marks_hybrid_unverified(monkeypatch):
    kw_hits = [_hit("case:1", "case", 1)]

    def fake_hybrid(db, user, q, settings, embed_adapter=None):
        return RetrievalResult(
            hits=list(kw_hits),
            knowledge_version="v",
            retrieval_mode="keyword",
            degraded=True,
            degrade_reason="合成降级",
        )

    _patch_search(monkeypatch, fake_hybrid)
    report = eval_mod.run_eval(
        None, None, _settings(), {"case:评测集-连接池耗尽": "case:1"}, adapter=object()
    )
    assert report["hybrid_metrics_valid"] is False
    assert report["summary"]["hybrid"]["status"] == "unverified"
    assert report["hybrid_stats"]["degrade_rate"] == 1.0
    assert eval_mod.eval_exit_code(report) == 3  # 可识别退出状态
    assert isinstance(report["summary"]["keyword"]["Recall@3"], float)  # 关键词指标仍有效


def test_partial_degrade_marks_unverified(monkeypatch):
    kw_hits = [_hit("case:1", "case", 1)]
    state = {"n": 0}

    def fake_hybrid(db, user, q, settings, embed_adapter=None):
        state["n"] += 1
        if state["n"] == 1:
            return RetrievalResult(
                hits=list(kw_hits),
                knowledge_version="v",
                retrieval_mode="keyword",
                degraded=True,
                degrade_reason="偶发降级",
            )
        return RetrievalResult(
            hits=list(kw_hits), knowledge_version="v", retrieval_mode="hybrid", degraded=False
        )

    _patch_search(monkeypatch, fake_hybrid)
    report = eval_mod.run_eval(
        None, None, _settings(), {"case:评测集-连接池耗尽": "case:1"}, adapter=object()
    )
    assert report["hybrid_stats"]["degraded"] == 1
    assert report["summary"]["hybrid"]["status"] == "unverified"  # 部分降级同样不汇总
    assert eval_mod.eval_exit_code(report) == 3


def test_all_hybrid_ok_reports_numeric_metrics(monkeypatch):
    kw_hits = [_hit("case:1", "case", 1)]

    def fake_hybrid(db, user, q, settings, embed_adapter=None):
        return RetrievalResult(
            hits=list(kw_hits), knowledge_version="v", retrieval_mode="hybrid", degraded=False
        )

    _patch_search(monkeypatch, fake_hybrid)
    report = eval_mod.run_eval(
        None, None, _settings(), {"case:评测集-连接池耗尽": "case:1"}, adapter=object()
    )
    assert report["hybrid_metrics_valid"] is True
    assert isinstance(report["summary"]["hybrid"]["Recall@3"], float)
    assert report["hybrid_stats"]["degraded"] == 0
    assert eval_mod.eval_exit_code(report) == 0


# ===== 空结果指标:任何命中均计误报 =====


def test_empty_gold_any_hit_counts_false_positive(monkeypatch):
    hit999 = _hit("sop:999", "sop", 999)  # 非评测夹具来源

    def fake_hybrid(db, user, q, settings, embed_adapter=None):
        return RetrievalResult(
            hits=[hit999], knowledge_version="v", retrieval_mode="hybrid", degraded=False
        )

    monkeypatch.setattr(
        "app.search.keyword.search_knowledge", lambda db, user, q: ([hit999], "v")
    )
    monkeypatch.setattr("app.search.hybrid.search_knowledge_hybrid", fake_hybrid)
    report = eval_mod.run_eval(
        None, None, _settings(), {"case:评测集-连接池耗尽": "case:1"}, adapter=object()
    )
    # gold 为空的查询返回了 sop:999 → 误报;不再因"非夹具来源"漏计
    assert report["summary"]["keyword"]["空结果正确率"] == 0.0
    assert report["summary"]["hybrid"]["空结果正确率"] == 0.0


# ===== 索引准备:真实入口构建真实来源;合成/真实不混用 =====


def _fake_run_build(monkeypatch, result="success", built_units=3):
    calls: list[dict] = []

    def fake(db, settings, adapter=None, force=False):
        calls.append({"adapter": adapter, "force": force})
        return SimpleNamespace(result=result, built_units=built_units)

    monkeypatch.setattr("app.search.embedding_index.run_build", fake)
    return calls


def test_prepare_index_real_builds_with_configured_adapter(monkeypatch):
    calls = _fake_run_build(monkeypatch)
    info = eval_mod.prepare_index(None, _settings(), synthetic=False)
    assert len(calls) == 1
    assert calls[0]["adapter"] is None  # 严格走配置适配器
    assert calls[0]["force"] is True  # 强制重建,不沿用旧(可能合成的)向量
    assert info["index_source"].startswith("configured")


def test_prepare_index_synthetic_injects_adapter_and_rebuilds(monkeypatch):
    calls = _fake_run_build(monkeypatch)
    adapter = object()
    info = eval_mod.prepare_index(None, _settings(), synthetic=True, adapter=adapter)
    assert calls[0]["adapter"] is adapter
    assert calls[0]["force"] is True  # 同样强制重建:切换来源不混用
    assert "synthetic" in info["index_source"]


def test_prepare_index_build_failure_raises(monkeypatch):
    _fake_run_build(monkeypatch, result="failed")
    with pytest.raises(eval_mod.EvalConfigError):
        eval_mod.prepare_index(None, _settings(), synthetic=False)


def test_prepare_index_real_without_config_errors_before_build(monkeypatch):
    calls = _fake_run_build(monkeypatch)
    with pytest.raises(eval_mod.EvalConfigError):
        eval_mod.prepare_index(None, Settings(embedding_mode="disabled"), synthetic=False)
    assert calls == []  # 未配置真实服务:直接可识别错误,不产生误导性指标


# ===== --setup 真实生效 =====


def test_resolve_fixtures_requires_setup(db):
    with pytest.raises(eval_mod.EvalConfigError):
        eval_mod.resolve_fixtures(db, setup=False)  # 空库缺夹具 → 提示先 --setup
    mapping = eval_mod.resolve_fixtures(db, setup=True)  # --setup 创建
    assert set(mapping) == set(eval_mod.FIXTURES)
    mapping2 = eval_mod.resolve_fixtures(db, setup=False)  # 已存在可复用
    assert mapping2 == mapping
