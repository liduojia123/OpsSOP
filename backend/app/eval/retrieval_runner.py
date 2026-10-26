"""检索评测:真实 GET /api/search 路由,关键词 vs 混合同快照比较。

指标口径:
- Recall@k/MRR 为 gold 非空查询的宏平均;失败查询计 0;
- 同一来源重复片段去重;SOP 必须匹配正确 version;
- gold 为空查询只有 HTTP 成功且 0 命中才算正确;失败不算正确空结果;
- 任一正常查询混合降级/失败,整批混合指标 status=unverified,不发布子集提升;
- 关键词指标仍单独可读;合成 embedding 仅机制验证。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.eval.datasets import Dataset
from app.eval.metrics import (
    aggregate_mrr,
    aggregate_recall,
    dedupe_hits,
    metric,
    no_answer_metric,
    timing_summary,
)
from app.eval.synthetic import SyntheticEmbeddingAdapter


@dataclass
class RetrievalRun:
    report: dict
    exit_code: int


def _hit_rows(data: dict) -> list[dict]:
    out = []
    for hit in data.get("hits") or []:
        try:
            out.append(
                {
                    "ref_id": hit.get("ref_id"),
                    "source_type": hit.get("source_type"),
                    "source_id": int(hit.get("source_id")),
                    "version": hit.get("version"),
                    "status": hit.get("status"),
                }
            )
        except (TypeError, ValueError):
            continue
    return dedupe_hits(out)


def _search(client, headers, query: str, *, keyword_only: bool) -> dict:
    params = {"q": query}
    if keyword_only:
        params["keyword_only"] = "true"
    start = time.monotonic()
    resp = client.get("/api/search", params=params, headers=headers)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {}
    valid_shape = isinstance(data, dict) and isinstance(data.get("hits"), list)
    return {
        "http_status": resp.status_code,
        # 200 但结构异常不能算成功检索/正确空结果
        "http_ok": resp.status_code == 200 and valid_shape,
        "data": data if isinstance(data, dict) else {},
        "hits": _hit_rows(data) if valid_shape else [],
        "timing_ms": elapsed_ms,
    }


def run_retrieval(
    client,
    headers,
    dataset: Dataset,
    resolved_gold: dict[str, list[dict]],
    embedding_adapter: SyntheticEmbeddingAdapter,
    *,
    limit: int = 0,
) -> RetrievalRun:
    samples = dataset.samples_for("retrieval")
    if limit > 0:
        samples = samples[:limit]
    rows = []
    interrupted: dict | None = None
    for sample in samples:
        try:
            label = dataset.labels[sample.sample_id]
            gold = resolved_gold.get(sample.sample_id, [])
            kw = _search(client, headers, sample.retrieval_query or "", keyword_only=True)
            hy = _search(client, headers, sample.retrieval_query or "", keyword_only=False)
            row = {
                "sample_id": sample.sample_id,
                "group": sample.group,
                "query": sample.retrieval_query,
                "gold_keys": list(label.gold_knowledge_keys),
                "gold": gold,
                "keyword": {},
                "hybrid": {},
                "timing_ms": {},
            }
            for mode, result in (("keyword", kw), ("hybrid", hy)):
                data = result["data"]
                hits = result["hits"]
                mode_name = data.get("retrieval_mode", "")
                degraded = bool(data.get("degraded")) or (mode == "hybrid" and mode_name != "hybrid")
                if not result["http_ok"]:
                    degraded = True
                if gold:
                    if result["http_ok"]:
                        from app.eval.metrics import query_recall

                        r3 = query_recall(hits, gold, 3)
                        r5 = query_recall(hits, gold, 5)
                        rec = {
                            "recall3": r3["recall"],
                            "recall5": r5["recall"],
                            "mrr": r3["mrr"],
                            "gold_count": len(gold),
                            "hit_count": r3["hit_count"],
                            "first_gold_rank": r3["first_gold_rank"],
                            "status": "available",
                        }
                    else:
                        rec = {
                            "recall3": 0.0,
                            "recall5": 0.0,
                            "mrr": 0.0,
                            "gold_count": len(gold),
                            "hit_count": 0,
                            "first_gold_rank": None,
                            "status": "failed",
                        }
                else:
                    rec = {
                        "recall3": None,
                        "recall5": None,
                        "mrr": None,
                        "gold_count": 0,
                        "hit_count": 0,
                        "first_gold_rank": None,
                        "no_answer_correct": bool(result["http_ok"] and not hits),
                        "status": "available" if result["http_ok"] else "failed",
                    }
                row[mode] = {
                    "http_status": result["http_status"],
                    "retrieval_mode": mode_name,
                    "degraded": degraded,
                    "degrade_reason": data.get("degrade_reason", ""),
                    "hits": [
                        {"rank": i, **hit} for i, hit in enumerate(hits, start=1)
                    ],
                    **rec,
                }
                row["timing_ms"][mode] = round(result["timing_ms"], 3)
                if not result["http_ok"]:
                    row[mode]["error"] = str(data.get("detail") or f"HTTP {result['http_status']}")[:200]
            rows.append(row)
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
            interrupted = {
                "stage": "retrieval",
                "sample_id": sample.sample_id,
                "error_type": type(exc).__name__,
                "message": str(exc)[:300],
            }
            break

    planned_total = len(dataset.samples_for("retrieval"))
    limit_restricted = (
        limit > 0 and interrupted is None and len(rows) < planned_total
    )
    complete = interrupted is None and not limit_restricted
    # 混合有效性只由已完成查询与是否中断决定;--limit 冒烟不伪装成完整评测,
    # 但已完成查询没有降级时仍可退出 0。
    hybrid_valid = (
        all(not row["hybrid"]["degraded"] for row in rows) if rows else True
    ) and interrupted is None
    degraded_count = sum(1 for row in rows if row["hybrid"]["degraded"])
    cancelled = bool(
        interrupted is not None
        and interrupted.get("error_type") == "KeyboardInterrupt"
    )

    def _aggregate(group_rows: list[dict]) -> dict:
        out = {}
        for mode in ("keyword", "hybrid"):
            gold_rows = [r for r in group_rows if r["gold"]]
            empty_rows = [r for r in group_rows if not r["gold"]]
            stats = {
                "recall@3": aggregate_recall(
                    [{"recall3": r[mode]["recall3"]} for r in gold_rows], 3
                ),
                "recall@5": aggregate_recall(
                    [{"recall5": r[mode]["recall5"]} for r in gold_rows], 5
                ),
                "mrr": aggregate_mrr([{"mrr": r[mode]["mrr"]} for r in gold_rows]),
                "no_answer_accuracy": no_answer_metric(
                    [{"no_answer_correct": r[mode].get("no_answer_correct")} for r in empty_rows]
                ),
                "planned_gold_queries": len(gold_rows),
                "planned_no_answer_queries": len(empty_rows),
            }
            if interrupted is not None:
                reason = (
                    "运行被用户取消:仅包含已完成查询;不得作为完整检索效果评测"
                    if cancelled
                    else "运行中断:仅包含已完成查询;不得作为完整检索效果评测"
                )
                for key in ("recall@3", "recall@5", "mrr", "no_answer_accuracy"):
                    stats[key] = {
                        **stats[key],
                        "status": "partial",
                        "reason": reason,
                    }
            elif mode == "hybrid" and not hybrid_valid:
                reason = (
                    f"{degraded_count}/{len(rows)} 个正常评测查询发生混合降级/失败;"
                    "整批混合指标不可用(未验证),不得只选未降级子集发布提升"
                )
                for key in ("recall@3", "recall@5", "mrr", "no_answer_accuracy"):
                    stats[key] = {
                        **stats[key],
                        "value": None,
                        "status": "unverified",
                        "reason": reason,
                    }
            out[mode] = stats
        return out

    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    group_metrics = {name: _aggregate(items) for name, items in sorted(groups.items())}
    overall = _aggregate(rows)

    degrade_rate = metric(
        degraded_count,
        len(rows),
        definition="混合检索降级/失败的查询数 / 计划检索查询数",
    )
    if interrupted is not None:
        degrade_rate = {
            **degrade_rate,
            "status": "partial",
            "reason": (
                "运行被用户取消:降级率只基于已完成查询"
                if cancelled
                else "运行中断:降级率只基于已完成查询"
            ),
        }
    planned = planned_total
    report = {
        "complete": complete,
        "cancelled": cancelled,
        "complete_reason": (
            "cancelled"
            if cancelled
            else "interrupted"
            if interrupted is not None
            else "limit"
            if limit_restricted
            else "full"
        ),
        "interrupted": interrupted,
        "unexecuted_reason": (
            f"运行在 {interrupted['sample_id']} "
            f"{'被用户取消' if cancelled else '中断'}:{interrupted['error_type']};"
            "后续查询未执行,已完成结果保留为部分报告"
            if interrupted
            else f"--limit={limit} 仅执行 {len(rows)}/{planned};剩余查询未执行,不是完整评测"
            if limit_restricted
            else ""
        ),
        "planned_queries": planned,
        "executed_queries": len(rows),
        "unexecuted_queries": max(0, planned - len(rows)),
        "limit": limit,
        "hybrid_valid": hybrid_valid,
        "hybrid_degraded_queries": degraded_count,
        "hybrid_degrade_rate": degrade_rate,
        "timing_method": "in-process TestClient (not production network latency)",
        "timing": {
            "keyword": timing_summary([r["timing_ms"]["keyword"] for r in rows]),
            "hybrid": timing_summary([r["timing_ms"]["hybrid"] for r in rows]),
        },
        "metrics": overall,
        "groups": group_metrics,
        "cases": rows,
        "notes": [
            "Recall@k/MRR 为 gold 非空查询的宏平均;失败查询计 0。",
            "同一来源重复片段按 (source_type, source_id, version) 去重。",
            "gold 为空查询只有 HTTP 成功且 0 命中才算正确;失败不算正确空结果。",
            "合成 embedding 只验证脚本/排序/统计机制,不代表真实语义效果或召回提升。",
            "运行中断时保留已完成查询;complete=false 时指标状态为 partial,不是完整效果评测。",
        ],
    }
    if interrupted is not None:
        exit_code = 130 if cancelled else 4
    elif hybrid_valid:
        exit_code = 0
    else:
        exit_code = 3
    return RetrievalRun(report=report, exit_code=exit_code)
