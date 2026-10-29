"""评测指标:分母、计数、适用状态、宏平均召回与分位数。

所有比例输出 numerator/denominator/value/status/definition;分母为 0 时
value=None、status=not_applicable,不填 0% 或 100%。分位数使用最近秩算法,
零样本同样为 not_applicable。
"""

from __future__ import annotations

import math
import statistics


def metric(
    numerator,
    denominator,
    *,
    definition: str,
    status_if_zero: str = "not_applicable",
    status: str | None = None,
) -> dict:
    num = numerator
    den = denominator
    if den is None or den <= 0:
        return {
            "numerator": 0 if num is None else num,
            "denominator": 0 if den is None else den,
            "value": None,
            "status": status_if_zero,
            "definition": definition,
        }
    value = num / den
    return {
        "numerator": num,
        "denominator": den,
        "value": round(value, 4),
        "status": status or "available",
        "definition": definition,
    }


def percentile(values: list[float], p: float, *, method: str = "nearest_rank") -> dict:
    values = [float(v) for v in values if v is not None]
    if not values:
        return {
            "n": 0,
            "p": p,
            "value": None,
            "status": "not_applicable",
            "method": method,
        }
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return {
        "n": len(ordered),
        "p": p,
        "value": round(ordered[rank - 1], 3),
        "status": "available",
        "method": method,
    }


def timing_summary(values: list[float]) -> dict:
    values = [float(v) for v in values if v is not None]
    if not values:
        return {
            "n": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "status": "not_applicable",
            "method": "nearest_rank",
        }
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "p50": percentile(values, 50)["value"],
        "p95": percentile(values, 95)["value"],
        "status": "available",
        "method": "nearest_rank",
    }


def dedupe_hits(hits: list[dict]) -> list[dict]:
    """按来源类型/ID/版本去重,保留首次出现的排名。"""
    seen = set()
    out = []
    for hit in hits:
        key = (
            hit.get("source_type"),
            int(hit.get("source_id", 0)),
            hit.get("version"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def gold_matches(hit: dict, gold: dict) -> bool:
    return (
        hit.get("source_type") == gold.get("source_type")
        and int(hit.get("source_id", 0)) == int(gold.get("source_id", -1))
        and hit.get("version") == gold.get("version")
    )


def query_recall(hits: list[dict], gold: list[dict], k: int) -> dict:
    """单条 gold 非空查询的 Recall@k / MRR;去重后按实际顺序。"""
    ranked = dedupe_hits(hits)
    ranks: list[int] = []
    for g in gold:
        for index, hit in enumerate(ranked, start=1):
            if gold_matches(hit, g):
                ranks.append(index)
                break
    if not gold:
        return {
            "recall": None,
            "mrr": None,
            "gold_count": 0,
            "hit_count": 0,
            "first_gold_rank": None,
            "status": "not_applicable",
        }
    hit_count = sum(1 for r in ranks if r <= k)
    return {
        "recall": hit_count / len(gold),
        "mrr": (1.0 / min(ranks)) if ranks else 0.0,
        "gold_count": len(gold),
        "hit_count": hit_count,
        "first_gold_rank": min(ranks) if ranks else None,
        "status": "available",
    }


def aggregate_recall(rows: list[dict], k: int) -> dict:
    """宏平均:分子为逐条 Recall 之和,分母为该组计划查询数。

    rows 中每条须有 recall3/recall5/mrr（失败查询由调用方置 0）。缺结果行
    不应被静默丢弃;调用方用 planned/executed 计数另报。
    """
    key = f"recall{k}"
    values = [float(row.get(key)) for row in rows if row.get(key) is not None]
    denominator = len(rows)
    numerator = sum(values)
    return metric(
        round(numerator, 6),
        denominator,
        definition=(
            f"gold 非空查询的 Recall@{k} 宏平均:逐条在前 {k} 条中命中的不同 gold 数/"
            "该条 gold 总数,再对全部计划查询求平均;失败查询计 0"
        ),
    )


def aggregate_mrr(rows: list[dict]) -> dict:
    numerator = sum(float(row.get("mrr") or 0.0) for row in rows)
    return metric(
        round(numerator, 6),
        len(rows),
        definition="首个正确 gold 排名倒数的宏平均;失败查询计 0",
    )


def no_answer_metric(rows: list[dict]) -> dict:
    """rows 须包含 no_answer_correct(bool);失败查询由调用方置 False。"""
    correct = sum(1 for row in rows if row.get("no_answer_correct") is True)
    return metric(
        correct,
        len(rows),
        definition=(
            "gold 为空的查询中,HTTP 成功且实际返回 0 条正式来源的比例;"
            "任何命中计误报,接口失败不计正确"
        ),
    )
