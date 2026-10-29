"""第四阶段第二批:在全新专用评测库上完成完整合成检索/分析评测。

本测试只使用合成适配器、合成标签和隔离库;不读取真实 .env 的模型配置,
不调用真实聊天/embedding 服务。
"""

import uuid
from pathlib import Path

from app.config import get_settings
from app.eval.analysis_runner import run_analysis
from app.eval.app_context import (
    eval_app_context,
    login,
    reset_service_runtime,
    synthetic_settings,
)
from app.eval.datasets import load_dataset, resolve_gold
from app.eval.db import create_or_reuse_database, drop_eval_database
from app.eval.fixtures import EVAL_PASSWORDS, prepare_fixtures
from app.eval.retrieval_runner import run_retrieval
from app.eval.synthetic import SyntheticChatAdapter, SyntheticEmbeddingAdapter
from app.search.embedding_index import run_build

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval_data" / "v1"


def test_full_synthetic_eval_on_fresh_disposable_db():
    dataset = load_dataset(DATASET_DIR)
    base = get_settings()
    run_id = "test" + uuid.uuid4().hex[:8]
    target = f"opssop_eval_{run_id}"
    settings = synthetic_settings(base)
    eval_db = None
    try:
        eval_db = create_or_reuse_database(
            base,
            target,
            run_id=run_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
            reuse=False,
        )
        db = eval_db.session_factory()
        try:
            fixtures = prepare_fixtures(db, dataset, eval_db.manifest)
        finally:
            db.close()
        resolved = resolve_gold(dataset, fixtures.fixture_map)

        chat = SyntheticChatAdapter(dataset)
        embedding = SyntheticEmbeddingAdapter()
        db = eval_db.session_factory()
        try:
            build = run_build(db, settings, adapter=embedding, force=True)
        finally:
            db.close()
        assert build.result == "success"

        reset_service_runtime()
        with eval_app_context(eval_db.session_factory, settings, chat, embedding) as client:
            owner_headers = login(client, "eval_owner", EVAL_PASSWORDS["eval_owner"])
            viewer_headers = login(client, "eval_viewer", EVAL_PASSWORDS["eval_viewer"])
            retrieval = run_retrieval(client, viewer_headers, dataset, resolved, embedding)
            analysis = run_analysis(
                client,
                owner_headers,
                viewer_headers,
                dataset,
                fixtures,
                chat,
                eval_db.session_factory,
            )

        assert retrieval.exit_code == 0, retrieval.report.get("notes")
        assert analysis.exit_code == 0, [
            a for a in analysis.report["mechanism_assertions"] if a.get("passed") is False
        ]
        assert retrieval.report["hybrid_valid"] is True
        assert retrieval.report["metrics"]["keyword"]["recall@3"]["status"] == "available"
        assert retrieval.report["metrics"]["keyword"]["recall@3"]["denominator"] == 15
        assert retrieval.report["metrics"]["keyword"]["no_answer_accuracy"]["denominator"] == 9

        main = analysis.report["classification"]["main"]
        assert main["accuracy"]["denominator"] == 18
        assert main["abstain_accuracy"]["denominator"] == 3
        assert analysis.report["failures"]["main"]["failure_rate"]["denominator"] == 21
        assert analysis.report["references"]["failure"]["raw_adopted"]["invalid_occurrences"] >= 1
        assert analysis.report["references"]["failure"]["format_repair"]["samples"] >= 3
        final_refs = analysis.report["references"]["main"]["final"]
        assert final_refs["validity"]["status"] == "available"
        assert final_refs["validity"]["numerator"] == final_refs["validity"]["denominator"]
        assert final_refs["observable_samples"] == 21
        assert analysis.report["failures"]["cache"]["reused"] == 1
        assert analysis.report["failures"]["cache"]["new_model_calls"] == 1
        assert analysis.report["failures"]["cache"]["source_probe_requests"] == 1
        assert analysis.report["failures"]["permission"][0]["http_status"] == 403
        assert analysis.report["injection"]["passed"] is True
        assert analysis.report["usage"]["real_chat_calls"] == 0
        assert analysis.report["usage"]["real_embedding_calls"] == 0
        cache_probe = analysis.report["cache_probe"]
        assert cache_probe["status"] == "available"
        assert cache_probe["applicable"] is True
        assert cache_probe["passed"] is True
        assert cache_probe["api_requests_during_source_probe"] == 1
        assert cache_probe["invalid_reference_absent"] is True
        assert cache_probe["path"] in (
            "cache_hit_revalidated",
            "cache_invalidated_reanalysis",
        )
        assert analysis.report["assertion_counts"]["failed"] == 0
        assert analysis.report["assertion_counts"]["skipped"] == 0
        assert all(
            a.get("passed") is True
            for a in analysis.report["mechanism_assertions"]
            if a.get("status") != "skipped"
        )
    finally:
        if eval_db is not None:
            try:
                drop_eval_database(base, eval_db.name, run_id)
            except Exception:  # noqa: BLE001
                # 不掩盖主断言失败;正常路径应无异常
                pass
