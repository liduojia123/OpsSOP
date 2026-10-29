"""第四阶段第二批:指标数学、分母/状态、宏平均召回与分位数。"""

from app.eval.metrics import (
    aggregate_mrr,
    aggregate_recall,
    dedupe_hits,
    gold_matches,
    metric,
    no_answer_metric,
    percentile,
    query_recall,
    timing_summary,
)


def _hit(source_type: str, source_id: int, version=None) -> dict:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "version": version,
    }


def test_metric_zero_denominator_is_not_applicable_not_zero_percent():
    m = metric(0, 0, definition="x")
    assert m["value"] is None
    assert m["status"] == "not_applicable"
    assert m["numerator"] == 0
    m2 = metric(1, 0, definition="x")
    assert m2["value"] is None and m2["status"] == "not_applicable"


def test_percentile_nearest_rank_and_empty():
    assert percentile([], 50)["value"] is None
    assert percentile([], 50)["status"] == "not_applicable"
    assert percentile([10], 95)["value"] == 10
    assert percentile([1, 2, 3, 4], 50)["value"] == 2
    assert percentile([1, 2, 3, 4], 95)["value"] == 4
    assert timing_summary([])["n"] == 0
    assert timing_summary([4])["p50"] == 4
    assert timing_summary([4])["p95"] == 4


def test_query_recall_multi_gold_dedupes_and_requires_version_match():
    hits = [
        _hit("case", 2),
        _hit("sop", 1, 2),
        _hit("case", 2),  # 重复片段不重复算召回
        _hit("sop", 1, 1),  # 错误版本
    ]
    gold = [_hit("case", 2), _hit("sop", 1, 2)]
    result = query_recall(hits, gold, 3)
    assert result["recall"] == 1.0
    assert result["mrr"] == 1.0
    assert result["hit_count"] == 2
    assert result["first_gold_rank"] == 1
    assert dedupe_hits(hits) == [hits[0], hits[1], hits[3]]
    assert gold_matches(hits[1], gold[1]) is True
    assert gold_matches(hits[3], gold[1]) is False


def test_query_recall_wrong_version_and_no_gold():
    result = query_recall([_hit("sop", 1, 1)], [_hit("sop", 1, 2)], 3)
    assert result["recall"] == 0.0
    assert result["mrr"] == 0.0
    empty = query_recall([], [], 3)
    assert empty["status"] == "not_applicable"
    assert empty["recall"] is None


def test_aggregate_recall_is_macro_average_over_planned_queries():
    rows = [
        {"recall3": 1.0},
        {"recall3": 0.5},
        {"recall3": 0.0},  # 失败查询由调用方置 0,仍在分母
    ]
    m = aggregate_recall(rows, 3)
    assert m["numerator"] == 1.5
    assert m["denominator"] == 3
    assert m["value"] == 0.5
    assert m["status"] == "available"
    mrr = aggregate_mrr([{"mrr": 1.0}, {"mrr": 0.0}])
    assert mrr["numerator"] == 1.0
    assert mrr["denominator"] == 2
    assert mrr["value"] == 0.5


def test_no_answer_requires_success_and_zero_hits():
    rows = [
        {"no_answer_correct": True},
        {"no_answer_correct": False},
        {"no_answer_correct": True},
    ]
    m = no_answer_metric(rows)
    assert m["numerator"] == 2
    assert m["denominator"] == 3
    assert m["value"] == 0.6667
