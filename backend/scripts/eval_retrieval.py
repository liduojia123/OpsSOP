"""小型检索评测集与脚本(第三阶段第三批)。

比较关键词检索与混合检索;指标口径:
- Recall@3 / Recall@5:正确来源(gold)出现在前 3/前 5 的比例(按查询平均);
- MRR:第一个正确来源的排名倒数(仅对 gold 非空的查询平均);
- 空结果正确率:gold 为空的查询中,未返回错误来源的比例(误报率的补);
- 覆盖:每类查询(问法改写/精确匹配/相同表象不同根因/无答案/来源失效)的分项结果。

查询与演示输入分开:评测夹具以「评测集-」前缀写入(仅 --setup 创建,--cleanup 删除);
gold 为人工标注(见 EVAL_CASES)。合成 embedding(同义词表词袋)只验证脚本、
排序机制与统计口径,不代表真实语义检索质量;真实效果保留待运行(配置真实服务后
去掉 --synthetic 即使用配置适配器,当前不调用付费真实模型)。

用法(后端容器内或已配置数据库环境的主机):
    python scripts/eval_retrieval.py --setup --synthetic      # 写入评测夹具并用合成向量评测
    python scripts/eval_retrieval.py --synthetic --out /tmp/eval.json   # 复跑(夹具已在)
    python scripts/eval_retrieval.py --setup                  # 真实评测(配置真实服务后):
                                                              # 强制构建真实索引再评测
    python scripts/eval_retrieval.py --cleanup                # 删除评测夹具

退出状态:0 = 混合指标有效(全部查询为 hybrid);2 = 配置/夹具/索引构建错误;
3 = 存在降级查询,混合指标标为未验证(不把关键词降级汇总成混合效果)。
每次评测都强制重建索引,合成与真实向量来源不混用。
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass, field

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))


# ---- 评测集(查询与演示输入分开;gold 为人工标注的正确来源)----

@dataclass
class EvalCase:
    case_id: str
    category: str  # paraphrase | exact | same_symptom | no_answer | stale_source
    query: str
    gold: list[str] = field(default_factory=list)  # 期望来源,如 case:12 / sop:3;可为空(应无答案)


EVAL_CASES = [
    EvalCase("E1", "paraphrase", "缓存问题一般怎么排查", ["case:评测集-连接池耗尽"]),
    EvalCase("E2", "exact", "OOMKilled", ["case:评测集-Pod内存溢出"]),
    EvalCase("E3", "exact", "ERR_CONNECTION_REFUSED", ["sop:评测集-端口不匹配"]),
    EvalCase("E4", "same_symptom", "服务响应慢", ["case:评测集-慢查询根因", "case:评测集-锁竞争根因"]),
    EvalCase("E5", "no_answer", "区块链共识节点出块超时", []),
    # 来源已失效:内容存在但状态非正式;正确行为是不返回(gold 为空)
    EvalCase("E6", "stale_source", "将废弃的专题", []),
]

# 评测夹具内容(知识正文;与演示种子输入分开)
FIXTURES = {
    "case:评测集-连接池耗尽": {
        "kind": "case",
        "title": "评测集-连接池耗尽处理",
        "symptom": "应用请求堆积,连接池耗尽",
        "confirmed_root_cause": "连接池上限过低",
        "handling_steps": "上调连接池并重启应用",
        "recovery_verification": "请求恢复正常",
    },
    "case:评测集-Pod内存溢出": {
        "kind": "case",
        "title": "评测集-Pod内存溢出",
        "symptom": "Pod 被 OOMKilled 重启",
        "confirmed_root_cause": "limits.memory 过低",
        "handling_steps": "调高内存上限",
        "recovery_verification": "稳定运行",
    },
    "sop:评测集-端口不匹配": {
        "kind": "sop",
        "title": "评测集-端口不匹配排查",
        "symptom": "出现 ERR_CONNECTION_REFUSED",
        "check_steps": "核对 Service targetPort 与容器端口",
        "handling_steps": "修正端口映射",
        "recovery_verification": "连接恢复",
    },
    "case:评测集-慢查询根因": {
        "kind": "case",
        "title": "评测集-慢查询导致响应慢",
        "symptom": "服务响应慢",
        "confirmed_root_cause": "数据库慢查询",
        "handling_steps": "优化查询并加索引",
        "recovery_verification": "延迟回落",
    },
    "case:评测集-锁竞争根因": {
        "kind": "case",
        "title": "评测集-锁竞争导致响应慢",
        "symptom": "服务响应慢",
        "confirmed_root_cause": "行锁竞争",
        "handling_steps": "缩短事务",
        "recovery_verification": "延迟回落",
    },
    "case:评测集-将废弃": {
        "kind": "case",
        "title": "评测集-将废弃的专题记录",
        "symptom": "评测集将废弃的专题",
        "confirmed_root_cause": "历史专题",
        "handling_steps": "记录保留",
        "recovery_verification": "已恢复",
        "status": "returned",  # 非正式状态:正确检索不得返回
    },
}

# 合成 embedding 的同义词表(仅测试口径;真实语义由真实模型提供)
_SYNONYMS = [
    ({"缓存", "连接池", "连接", "池"}, "concept_pool"),
    ({"OOMKilled", "内存溢出", "OOM"}, "concept_oom"),
    ({"ERR_CONNECTION_REFUSED", "端口不匹配", "连接被拒"}, "concept_port"),
    ({"响应慢", "慢查询", "锁竞争", "延迟"}, "concept_slow"),
    ({"废弃", "将废弃"}, "concept_stale"),
]

_VOCAB = [c for _, c in _SYNONYMS] + ["_unknown"]
_DIMS = len(_VOCAB)


def _synthetic_vector(text: str) -> list[float]:
    """合成语义向量:同义词概念 one-hot;无概念 → _unknown one-hot(与任何概念正交)。"""
    vec = [0.0] * _DIMS
    for words, concept in _SYNONYMS:
        if any(w in text for w in words):
            vec[_VOCAB.index(concept)] = 1.0
    if sum(vec) == 0.0:
        vec[-1] = 1.0  # 无概念命中:unknown 轴(与主题向量距离明显)
    return vec


class _SyntheticEvalAdapter:
    """评测专用合成适配器(同义词表词袋);不代表真实模型能力。"""

    def embed(self, texts):
        from app.ai.embeddings import EmbeddingResult

        return EmbeddingResult(
            vectors=[_synthetic_vector(t) for t in texts],
            usage=None,  # 合成调用不虚构用量
            model="synthetic-eval",
            dims=_DIMS,
        )

    def describe(self):
        return {"mode": "synthetic-eval"}


# ---- 评测夹具:创建/删除 ----


def setup_fixtures(db) -> dict[str, str]:
    """写入评测夹具(带前缀,与演示输入分开);幂等:已存在则复用。返回 gold 标签 → ref_id。"""
    from app.models import Case, CaseStatus, Sop, SopStatus, User, utcnow

    owner = db.query(User).filter(User.username == "eval_owner").first()
    if owner is None:
        from app.security import hash_password

        owner = User(
            username="eval_owner",
            display_name="eval_owner",
            password_hash=hash_password("eval-owner-password-1"),
            role="operator",
        )
        db.add(owner)
        db.commit()
    mapping: dict[str, str] = {}
    for label, fx in FIXTURES.items():
        if fx["kind"] == "case":
            existing = db.query(Case).filter(Case.title == fx["title"]).first()
            if existing is not None:
                mapping[label] = f"case:{existing.id}"
                continue
            row = Case(
                title=fx["title"],
                symptom=fx["symptom"],
                environment="评测环境",
                service="eval-svc",
                component_version="1.0",
                tags=["evaluation-fixture", "legacy-eval-retrieval"],
                confirmed_root_cause=fx["confirmed_root_cause"],
                handling_steps=fx["handling_steps"],
                recovery_verification=fx["recovery_verification"],
                status=CaseStatus.approved if fx.get("status") != "returned" else CaseStatus.returned,
                created_by=owner.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            # fault_id 非空约束:挂到一个专用占位故障(评测夹具不混入演示输入)
            from app.models import Fault, FaultStatus

            f = Fault(
                title=f"评测占位-{fx['title']}",
                tags=["evaluation-fixture", "legacy-eval-retrieval"],
                created_by=owner.id,
                status=FaultStatus.resolved,
            )
            db.add(f)
            db.flush()
            row.fault_id = f.id
            db.add(row)
            db.flush()
            mapping[label] = f"case:{row.id}"
        else:
            existing = db.query(Sop).filter(Sop.title == fx["title"]).first()
            if existing is not None:
                mapping[label] = f"sop:{existing.id}"
                continue
            row = Sop(
                doc_id=f"eval-{label}",
                version=1,
                title=fx["title"],
                tags=["evaluation-fixture", "legacy-eval-retrieval"],
                symptom=fx["symptom"],
                check_steps=fx["check_steps"],
                handling_steps=fx["handling_steps"],
                recovery_verification=fx["recovery_verification"],
                status=SopStatus.published,
                created_by=owner.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(row)
            db.flush()
            mapping[label] = f"sop:{row.id}"
    db.commit()
    return mapping


def cleanup_fixtures(db) -> int:
    """按本轮评测标签精确清理,不使用标题前缀或全表删除。"""
    from app.models import Case, Fault, Sop

    marker = ["legacy-eval-retrieval"]
    n = 0
    # 先删案例/SOP,再删占位故障,避免外键顺序问题
    for row in db.query(Case).filter(Case.tags.contains(marker)).all():
        db.delete(row)
        n += 1
    for row in db.query(Sop).filter(Sop.tags.contains(marker)).all():
        db.delete(row)
        n += 1
    for row in db.query(Fault).filter(Fault.tags.contains(marker)).all():
        db.delete(row)
        n += 1
    db.commit()
    return n


# ---- 评测入口契约:夹具解析、索引准备、退出状态(复审 P2-5)----


class EvalConfigError(Exception):
    """评测配置/准备错误:返回可识别退出状态 2,不输出误导性指标。"""


def resolve_fixtures(db, setup: bool) -> dict[str, str]:
    """--setup 创建评测夹具;不带 --setup 时要求夹具已存在(参数真实生效)。"""
    if setup:
        return setup_fixtures(db)
    mapping: dict[str, str] = {}
    missing: list[str] = []
    from app.models import Case, Sop

    for label, fx in FIXTURES.items():
        if fx["kind"] == "case":
            row = db.query(Case).filter(Case.title == fx["title"]).first()
        else:
            row = db.query(Sop).filter(Sop.title == fx["title"]).first()
        if row is None:
            missing.append(label)
        else:
            mapping[label] = f"{'case' if fx['kind'] == 'case' else 'sop'}:{row.id}"
    if missing:
        raise EvalConfigError(
            f"缺少评测夹具 {missing};请先运行 --setup 写入评测夹具(--setup --synthetic)"
        )
    return mapping


def prepare_index(db, settings, synthetic: bool, adapter=None) -> dict:
    """构建评测对应的兼容索引;每次评测强制重建,隔离合成与真实向量来源(复审 P2-5)。

    - synthetic:注入合成适配器构建(机制验证,零真实调用);
    - 非 synthetic(真实评测入口):用配置的真实适配器构建真实来源索引,
      不允许沿用此前合成或其他配置的向量;构建失败返回可识别错误。
    """
    from app.search.embedding_index import run_build

    if synthetic:
        if adapter is None:
            raise EvalConfigError("合成评测需要注入合成适配器")
        source = "synthetic-eval(机制验证,非真实语义)"
    else:
        adapter = None  # 真实评测:严格走配置适配器,不接受注入的合成向量
        source = "configured-embedding-adapter(真实配置来源)"
        if settings.embedding_mode == "disabled" or not settings.embedding_model:
            raise EvalConfigError(
                "真实评测需要配置 EMBEDDING_MODE/BASE_URL/MODEL(将用配置适配器构建真实索引)"
            )
    if settings.embedding_mode == "disabled" or not settings.embedding_model:
        raise EvalConfigError(
            "合成评测同样需要配置 EMBEDDING_MODE/BASE_URL/MODEL"
            "(适配器已注入,不会发生真实调用;指纹/维度一致性走真实兼容性检查)"
        )
    outcome = run_build(db, settings, adapter=adapter, force=True)
    if outcome.result != "success":
        raise EvalConfigError(
            f"评测索引构建失败({outcome.result}):未验证的混合指标不予输出"
        )
    return {
        "index_build": outcome.result,
        "built_units": outcome.built_units,
        "index_source": source,
        "note": "每次评测强制重建索引,合成与真实向量来源不混用",
    }


def eval_exit_code(report: dict) -> int:
    """退出状态:0 = 混合指标有效;3 = 存在降级,混合指标未验证(可识别)。"""
    return 0 if report.get("hybrid_metrics_valid") else 3


# ---- 评测执行 ----


def _rank_lookup(hits, mapping) -> dict[str, int]:
    """gold 标签 → 命中排名(1-based);未命中为 None。"""
    actual_to_label = {v: k for k, v in mapping.items()}
    out: dict[str, int] = {}
    for i, h in enumerate(hits, start=1):
        ref = f"{h.source_type}:{h.source_id}"
        label = actual_to_label.get(ref)
        if label and label not in out:
            out[label] = i
    return out


def run_eval(db, user, settings, mapping, adapter=None) -> dict:
    from app.search.hybrid import search_knowledge_hybrid
    from app.search.keyword import search_knowledge

    # 评测自身控制查询次数:放宽限流以免把降级误计为检索效果
    try:
        settings.ai_rate_limit_per_minute = 1000
    except Exception:
        pass

    results = []
    agg = {
        "keyword": {"recall3": [], "recall5": [], "mrr": [], "empty_correct": []},
        "hybrid": {"recall3": [], "recall5": [], "mrr": [], "empty_correct": []},
    }
    for case in EVAL_CASES:
        kw_hits, _ = search_knowledge(db, user, case.query)
        hy = search_knowledge_hybrid(db, user, case.query, settings, embed_adapter=adapter)
        row = {"case_id": case.case_id, "category": case.category, "query": case.query, "gold": case.gold}
        for mode, hits in (("keyword", kw_hits), ("hybrid", hy.hits)):
            ranks = _rank_lookup(hits, mapping) if case.gold else {}
            if case.gold:
                found = [ranks[g] for g in case.gold if g in ranks]
                r3 = sum(1 for g in case.gold if g in ranks and ranks[g] <= 3) / len(case.gold)
                r5 = sum(1 for g in case.gold if g in ranks and ranks[g] <= 5) / len(case.gold)
                mrr = (1.0 / min(found)) if found else 0.0
                agg[mode]["recall3"].append(r3)
                agg[mode]["recall5"].append(r5)
                agg[mode]["mrr"].append(mrr)
                row[mode] = {"top": [f"{h.source_type}:{h.source_id}" for h in hits[:5]], "first_gold_rank": (min(found) if found else None)}
            else:
                # gold 为空:查询应无答案,任何命中都是误报(不只评测夹具来源;
                # 复审 P2-5 次要项:按真实返回结果计误报)
                false_pos = [f"{h.source_type}:{h.source_id}" for h in hits]
                correct = 0.0 if false_pos else 1.0
                agg[mode]["empty_correct"].append(correct)
                row[mode] = {"top": [f"{h.source_type}:{h.source_id}" for h in hits[:5]], "false_positives": false_pos}
        row["hybrid_degraded"] = hy.degraded
        row["hybrid_mode"] = hy.retrieval_mode
        results.append(row)

    def _avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    n_total = len(results)
    n_degraded = sum(
        1 for r in results if r.get("hybrid_degraded") or r.get("hybrid_mode") != "hybrid"
    )
    hybrid_stats = {
        "queries": n_total,
        "hybrid_ok": n_total - n_degraded,
        "degraded": n_degraded,
        "degrade_rate": round(n_degraded / n_total, 4) if n_total else None,
    }

    summary = {}
    a = agg["keyword"]
    summary["keyword"] = {
        "Recall@3": _avg(a["recall3"]),
        "Recall@5": _avg(a["recall5"]),
        "MRR": _avg(a["mrr"]),
        "空结果正确率": _avg(a["empty_correct"]),
    }
    if n_total and n_degraded == 0:
        b = agg["hybrid"]
        summary["hybrid"] = {
            "Recall@3": _avg(b["recall3"]),
            "Recall@5": _avg(b["recall5"]),
            "MRR": _avg(b["mrr"]),
            "空结果正确率": _avg(b["empty_correct"]),
        }
        hybrid_metrics_valid = True
    else:
        # 有查询降级为关键词:不得把关键词降级结果汇总成有效的混合指标(复审 P2-5);
        # 混合指标标为未验证/不可用,另行给出降级率
        summary["hybrid"] = {
            "status": "unverified",
            "reason": (
                f"{n_degraded}/{n_total} 次查询降级为关键词检索,"
                "混合指标不可用(未验证);不得据此推断混合检索效果"
            ),
            "degrade_rate": hybrid_stats["degrade_rate"],
        }
        hybrid_metrics_valid = False

    return {
        "notice": (
            "合成 embedding 只验证脚本、排序机制与统计口径;不代表真实语义检索质量、"
            "Recall 提升或模型效果;真实效果保留待运行状态。"
        ),
        "synthetic_embedding": adapter is not None,
        "hybrid_metrics_valid": hybrid_metrics_valid,
        "metrics_definition": {
            "Recall@3": "gold 出现在前 3 的比例(按查询平均,gold 可多项)",
            "Recall@5": "gold 出现在前 5 的比例",
            "MRR": "首个 gold 排名倒数(gold 非空查询平均)",
            "空结果正确率": "gold 为空的查询完全未返回结果的比例(任何命中均计误报)",
        },
        "hybrid_stats": hybrid_stats,
        "summary": summary,
        "cases": results,
    }


_ACTIVE_EVAL_DB: dict = {}
_LEGACY_DATASET_VERSION = "legacy-eval-retrieval/1"


def _eval_session():
    """安全旧入口:每轮创建 opssop_eval_<run_id> 专用库,正式迁移并记录 manifest。

    不再使用固定 opssop_eval 或 create_all;连接前由 app.eval.db 校验目标库名/
    生产环境/业务库冲突。旧入口保留 0/2/3 退出语义;清理改用 --cleanup + 精确目标。
    """
    from app.config import get_settings
    from app.eval.db import create_or_reuse_database, default_target_db

    legacy_payload = {
        "cases": [
            [c.case_id, c.category, c.query, list(c.gold)]
            for c in EVAL_CASES
        ],
        "fixtures": FIXTURES,
    }
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    run_id = "legacy" + uuid.uuid4().hex[:8]
    eval_db = create_or_reuse_database(
        get_settings(),
        default_target_db(run_id),
        run_id=run_id,
        dataset_version=_LEGACY_DATASET_VERSION,
        dataset_hash=legacy_hash,
        reuse=False,
    )
    _ACTIVE_EVAL_DB.clear()
    _ACTIVE_EVAL_DB.update({"name": eval_db.name, "run_id": run_id})
    print(
        json.dumps(
            {
                "legacy_eval_database": eval_db.name,
                "run_id": run_id,
                "cleanup_hint": (
                    "python scripts/eval_retrieval.py --cleanup "
                    f"--target-db {eval_db.name} --run-id {run_id}"
                ),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return eval_db.session_factory()


def main() -> int:
    parser = argparse.ArgumentParser(description="检索评测(关键词 vs 混合;独立库 opssop_eval)")
    parser.add_argument("--setup", action="store_true", help="写入评测夹具(评测集-前缀);评测前必带")
    parser.add_argument("--synthetic", action="store_true", help="使用合成向量(验证口径;非真实效果)")
    parser.add_argument("--allow-real-embedding", action="store_true", help="显式允许旧入口真实 embedding 评测(本批不执行)")
    parser.add_argument("--out", default="", help="结果 JSON 输出路径(可选)")
    parser.add_argument("--cleanup", action="store_true", help="按精确名称清理本轮评测库(需 --target-db/--run-id)")
    parser.add_argument("--target-db", default="", help="--cleanup 的评测库名")
    parser.add_argument("--run-id", default="", help="--cleanup 的 run_id")
    args = parser.parse_args()

    from app.config import get_settings
    from app.eval.db import EvalSetupError, drop_eval_database
    from app.models import User

    settings = get_settings()
    if args.cleanup:
        if not args.target_db or not args.run_id:
            print(
                "旧入口 cleanup 需要精确 --target-db 和 --run-id;"
                "也推荐使用 python scripts/eval_suite.py --cleanup --target-db ... --run-id ...",
                file=sys.stderr,
            )
            return 2
        try:
            print(
                json.dumps(
                    {"cleanup": drop_eval_database(settings, args.target_db, args.run_id)},
                    ensure_ascii=False,
                )
            )
            return 0
        except EvalSetupError as exc:
            print(f"清理拒绝: {exc}", file=sys.stderr)
            return 2
    if not args.synthetic and not args.allow_real_embedding:
        print(
            "旧入口真实 embedding 评测未授权;本批只执行合成机制评测。"
            "如确需真实入口,显式 --allow-real-embedding。",
            file=sys.stderr,
        )
        return 2
    try:
        db = _eval_session()
    except EvalSetupError as exc:
        print(f"评测库准备失败: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            mapping = resolve_fixtures(db, setup=args.setup)
            user = db.query(User).filter(User.username == "eval_owner").first()
            adapter = _SyntheticEvalAdapter() if args.synthetic else None
            # 真实评测与合成评测都先构建对应来源的索引(强制重建,来源不混用;复审 P2-5)
            build_info = prepare_index(db, settings, synthetic=args.synthetic, adapter=adapter)
        except EvalConfigError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps(build_info, ensure_ascii=False), file=sys.stderr)
        report = run_eval(db, user, settings, mapping, adapter=adapter)
        report["gold_mapping"] = mapping
        report["index_build"] = build_info
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        # 0 = 混合指标有效;3 = 存在降级,混合指标未验证(不把关键词降级汇总成混合效果)
        return eval_exit_code(report)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
