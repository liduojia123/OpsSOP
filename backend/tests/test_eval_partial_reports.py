"""第四阶段第二批复审回归:运行中断时不得丢失已完成样本与进度。

覆盖独立复审 P2 #4:
- 检索在第二条查询中断时保留第一条两侧结果、计划/执行/未执行和 partial 状态;
- 分析在第二条请求中断时保留第一条和一条可审计的失败行,后续 suite 未执行。
"""

from pathlib import Path
from types import SimpleNamespace

from app.eval import analysis_runner
from app.eval.analysis_runner import _empty_reference_summary, run_analysis
from app.eval.datasets import load_dataset
from app.eval.retrieval_runner import run_retrieval
from app.eval.synthetic import SyntheticEmbeddingAdapter

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval_data" / "v1"


class _FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _InterruptingClient:
    def __init__(self, fail_on_request=3):
        self.calls = 0
        self.fail_on_request = fail_on_request

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == self.fail_on_request:
            raise RuntimeError("independent interruption after one complete query")
        return _FakeResponse(
            200,
            {"hits": [], "retrieval_mode": "hybrid", "degraded": False},
        )


class _CancellingClient(_InterruptingClient):
    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == self.fail_on_request:
            raise KeyboardInterrupt("independent synthetic user cancellation")
        return _FakeResponse(
            200,
            {"hits": [], "retrieval_mode": "hybrid", "degraded": False},
        )


def test_retrieval_cancellation_preserves_completed_query_and_marks_cancelled():
    dataset = load_dataset(DATASET_DIR)
    client = _CancellingClient()
    result = run_retrieval(
        client,
        {},
        dataset,
        {s.sample_id: [] for s in dataset.samples_for("retrieval")},
        SyntheticEmbeddingAdapter(),
    )
    report = result.report
    assert result.exit_code == 130
    assert report["cancelled"] is True
    assert report["complete"] is False
    assert report["complete_reason"] == "cancelled"
    assert report["interrupted"]["error_type"] == "KeyboardInterrupt"
    assert report["executed_queries"] == 1
    assert report["unexecuted_queries"] == 23
    assert len(report["cases"]) == 1
    assert report["metrics"]["keyword"]["recall@3"]["status"] == "partial"
    assert "取消" in report["unexecuted_reason"]


def test_retrieval_interruption_preserves_completed_query_and_progress():
    dataset = load_dataset(DATASET_DIR)
    client = _InterruptingClient()
    result = run_retrieval(
        client,
        {},
        dataset,
        {s.sample_id: [] for s in dataset.samples_for("retrieval")},
        SyntheticEmbeddingAdapter(),
    )
    report = result.report
    assert result.exit_code == 4
    assert report["complete"] is False
    assert report["interrupted"]["stage"] == "retrieval"
    assert report["executed_queries"] == 1
    assert report["unexecuted_queries"] == 23
    assert report["planned_queries"] == 24
    assert len(report["cases"]) == 1
    assert report["cases"][0]["keyword"]["http_status"] == 200
    assert report["cases"][0]["hybrid"]["http_status"] == 200
    assert report["metrics"]["keyword"]["recall@3"]["status"] == "partial"
    assert report["metrics"]["hybrid"]["recall@3"]["status"] == "partial"
    assert "中断" in report["unexecuted_reason"]


def _analysis_sample(sample_id):
    return SimpleNamespace(
        sample_id=sample_id,
        group="partial",
        tasks=("analysis",),
        input={"title": sample_id},
    )


def _analysis_label():
    return SimpleNamespace(
        expected_category="Kubernetes",
        allowed_categories=("Kubernetes",),
        abstain=False,
        expected_analysis_status="success",
        expected_behavior=None,
        expected_permission=None,
    )


class _FakeDataset:
    def __init__(self):
        self.samples = [_analysis_sample("S_PART_1"), _analysis_sample("S_PART_2")]
        self.labels = {sample.sample_id: _analysis_label() for sample in self.samples}

    def samples_for(self, task):
        return [s for s in self.samples if task in s.tasks]


class _FakeSession:
    def scalar(self, *_args, **_kwargs):
        return 0

    def close(self):
        pass


def _success_row(sample, suite):
    return {
        "suite": suite,
        "sample_id": sample.sample_id,
        "group": sample.group,
        "tasks": list(sample.tasks),
        "fault_id": 1,
        "http_status": 201,
        "http_ok": True,
        "business_status": "success",
        "error_code": "",
        "error_message": "",
        "analysis_id": 1,
        "input_hash": "x",
        "model_config_version": "cfg",
        "knowledge_version": "kb",
        "is_demo": False,
        "reused": False,
        "new_model_calls": 1,
        "historical_call_count": 1,
        "usage": None,
        "usage_status": "unknown",
        "suggested_category": "Kubernetes",
        "expected_category": "Kubernetes",
        "allowed_categories": ["Kubernetes"],
        "abstain_expected": False,
        "category_correct": True,
        "abstain_correct": None,
        "valid_category": True,
        "category_bucket": "Kubernetes",
        "reference_mismatch": False,
        "incomplete_reasons": [],
        "references": _empty_reference_summary(),
        "timing_ms": 1.0,
        "expected_analysis_status": "success",
        "expected_behavior": None,
        "is_quality": True,
    }


def test_analysis_interruption_preserves_completed_request_and_failure_row(monkeypatch):
    dataset = _FakeDataset()
    fixtures = SimpleNamespace(analysis_faults={"S_PART_1": 1, "S_PART_2": 2})
    calls = {"n": 0}

    def fake_request_row(client, headers, *, sample, label, fault_id, chat_adapter, use_cache, suite, session_factory=None):
        calls["n"] += 1
        if sample.sample_id == "S_PART_2":
            raise RuntimeError("independent analysis interruption")
        return _success_row(sample, suite)

    monkeypatch.setattr(analysis_runner, "_request_row", fake_request_row)
    result = run_analysis(
        client=None,
        owner_headers={},
        viewer_headers={},
        dataset=dataset,
        fixtures=fixtures,
        chat_adapter=SimpleNamespace(calls=[]),
        session_factory=lambda: _FakeSession(),
    )
    report = result.report
    assert result.exit_code == 4
    assert report["complete"] is False
    assert report["interrupted"]["stage"] == "main"
    assert report["interrupted"]["sample_id"] == "S_PART_2"
    assert report["executed"]["main"] == 2
    assert report["unexecuted"]["main"] == 0
    assert report["unexecuted"]["failure"] == 0
    assert len(report["cases"]) == 2
    assert report["cases"][1]["error_code"] == "eval_runner_exception"
    assert report["classification"]["main"]["accuracy"]["numerator"] == 1
    assert report["classification"]["main"]["accuracy"]["denominator"] == 2
    assert report["assertion_counts"]["skipped"] >= 1
    assert "中断" in report["unexecuted_reason"]


def test_analysis_cancellation_preserves_completed_request_and_marks_cancelled(monkeypatch):
    dataset = _FakeDataset()
    fixtures = SimpleNamespace(analysis_faults={"S_PART_1": 1, "S_PART_2": 2})

    def fake_request_row(client, headers, *, sample, label, fault_id, chat_adapter, use_cache, suite, session_factory=None):
        if sample.sample_id == "S_PART_2":
            raise KeyboardInterrupt("independent analysis cancellation")
        return _success_row(sample, suite)

    monkeypatch.setattr(analysis_runner, "_request_row", fake_request_row)
    result = run_analysis(
        client=None,
        owner_headers={},
        viewer_headers={},
        dataset=dataset,
        fixtures=fixtures,
        chat_adapter=SimpleNamespace(calls=[]),
        session_factory=lambda: _FakeSession(),
    )
    report = result.report
    assert result.exit_code == 130
    assert report["cancelled"] is True
    assert report["complete"] is False
    assert report["complete_reason"] == "cancelled"
    assert report["interrupted"]["error_type"] == "KeyboardInterrupt"
    assert report["executed"]["main"] == 2
    assert report["cases"][1]["error_code"] == "eval_runner_exception"
    assert "取消" in report["unexecuted_reason"]
