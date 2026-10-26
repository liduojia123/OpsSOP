"""第四阶段第二批:检索运行器的混合降级整批不可用契约。"""

from pathlib import Path

from app.eval.datasets import load_dataset, resolve_gold
from app.eval.retrieval_runner import run_retrieval
from app.eval.synthetic import SyntheticEmbeddingAdapter

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval_data" / "v1"


class _FakeResponse:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, hits_by_query: dict[str, list[dict]], degraded_queries: set[str]):
        self.hits_by_query = hits_by_query
        self.degraded_queries = degraded_queries

    def get(self, url: str, params=None, headers=None):
        query = (params or {}).get("q", "")
        keyword_only = (params or {}).get("keyword_only") == "true"
        hits = self.hits_by_query.get(query, [])
        if keyword_only:
            mode, degraded = "keyword", False
        elif query in self.degraded_queries:
            mode, degraded = "keyword", True
        else:
            mode, degraded = "hybrid", False
        return _FakeResponse(
            200,
            {
                "hits": hits,
                "retrieval_mode": mode,
                "degraded": degraded,
                "degrade_reason": "合成降级" if degraded else "",
            },
        )


def test_any_hybrid_degradation_invalidates_whole_batch_but_keyword_remains():
    ds = load_dataset(DATASET_DIR)
    samples = ds.samples_for("retrieval")[:2]
    fixture_map = {
        k.key: {"entity_type": k.kind, "entity_id": i + 1, "version": k.version}
        for i, k in enumerate(ds.knowledge)
    }
    resolved = resolve_gold(ds, fixture_map)
    hits_by_query = {}
    for sample in samples:
        hits_by_query[sample.retrieval_query] = [
            {
                "ref_id": f"{g['source_type']}:{g['source_id']}",
                "source_type": g["source_type"],
                "source_id": g["source_id"],
                "version": g["version"],
                "title": "synthetic",
                "snippet": "synthetic",
                "status": "approved" if g["source_type"] == "case" else "published",
            }
            for g in resolved[sample.sample_id]
        ]
    first_query = samples[0].retrieval_query
    client = _FakeClient(hits_by_query, degraded_queries={first_query})
    result = run_retrieval(
        client,
        {},
        ds,
        resolved,
        SyntheticEmbeddingAdapter(),
        limit=2,
    )
    assert result.exit_code == 3
    assert result.report["hybrid_valid"] is False
    assert result.report["hybrid_degraded_queries"] == 1
    assert result.report["hybrid_degrade_rate"]["numerator"] == 1
    assert result.report["hybrid_degrade_rate"]["denominator"] == 2
    assert result.report["metrics"]["hybrid"]["recall@3"]["status"] == "unverified"
    assert result.report["metrics"]["hybrid"]["recall@3"]["value"] is None
    # 关键词路径不受混合降级影响
    assert result.report["metrics"]["keyword"]["recall@3"]["status"] == "available"
    assert result.report["metrics"]["keyword"]["recall@3"]["value"] == 1.0
    first = result.report["cases"][0]
    assert first["hybrid"]["degraded"] is True
    assert first["keyword"]["degraded"] is False
    assert result.report["cases"][1]["hybrid"]["retrieval_mode"] == "hybrid"
