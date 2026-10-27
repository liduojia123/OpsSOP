"""AI 分析编排:证据分配 → 检索 → 模型调用 → 引用校验 → 存储。

分析编排与数据可靠性约束:
- 输入证据与知识片段由后端分配稳定 ID;模型输出结构/证据 ID/来源 ID/版本/权限/状态全部校验;
- 无效引用移除并标记结果不完整(incomplete_reasons);格式修复重试次数有限;
- 允许证据不足(insufficient_evidence)与无匹配知识;不编造引用;
- 分析记录绑定故障 ID、输入快照哈希、模型配置版本、知识版本;旧结果自然过期;
- 模型异常不影响业务输入;调用前服务端脱敏;usage 缺失记录为未知(null)。
"""

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.json_out import aggregate_usage, complete_json
from app.ai.llm import ChatMessage, DemoAdapter, build_adapter
from app.ai.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from app.ai.sanitize import sanitize_text
from app.ai.schema import AnalysisReport, EvidenceItem, KnowledgeReference, KnowledgeSnippet
from app.config import Settings
from app.models import (
    AiAnalysis,
    AiAnalysisStatus,
    Case,
    CaseStatus,
    Category,
    Fault,
    FaultRecord,
    Sop,
    SopStatus,
    User,
)
from app.search.hybrid import search_knowledge_hybrid
from app.search.keyword import SearchHit

# 测试与兼容别名:usage 聚合现由 app.ai.json_out 提供(与 SOP 生成共用)
_aggregate_usage = aggregate_usage
# 每个证据/知识片段进入提示词前的单条上限,避免单条日志撑爆上下文
MAX_EVIDENCE_CHARS = 3000
MAX_SNIPPET_CHARS = 800


@dataclass
class AnalysisOutcome:
    status: AiAnalysisStatus
    report: dict | None
    incomplete_reasons: list[str]
    error_code: str
    error_message: str
    usage: dict | None
    call_count: int
    latency_ms: int
    is_demo: bool


@dataclass
class AnalysisInput:
    """一次分析的输入快照(可来自未保存的表单覆盖,也可来自已保存故障)。

    与 ORM 分离:分析不写回故障字段;输入仅用于证据/哈希/提示词。
    """

    title: str = ""
    description: str = ""
    environment: str = ""
    service: str = ""
    component_version: str = ""
    recent_changes: str = ""
    error_logs: str = ""
    attempted_actions: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: int | None = None
    category_name: str = ""  # 展示用,由调用方解析


def input_from_fault(fault: Fault) -> AnalysisInput:
    """从已保存故障构建输入快照。"""
    return AnalysisInput(
        title=fault.title,
        description=fault.description,
        environment=fault.environment,
        service=fault.service,
        component_version=fault.component_version,
        recent_changes=fault.recent_changes,
        error_logs=fault.error_logs,
        attempted_actions=fault.attempted_actions,
        tags=list(fault.tags or []),
        category_id=fault.category_id,
        category_name=fault.category.name if fault.category else "",
    )


def input_from_overlay(fault: Fault, overlay: dict) -> AnalysisInput:
    """表单覆盖(未保存编辑)叠在已保存故障之上:缺省字段沿用已保存值。

    category_id 显式出现时(含 null 清空)按覆盖处理:清空后分类名为空,
    不再沿用数据库旧分类;非空覆盖的名称由 run_analysis 按 id 解析。
    """
    base = input_from_fault(fault)
    cat_id, cat_name = base.category_id, base.category_name
    if "category_id" in overlay:
        cat_id = overlay.get("category_id")
        cat_name = ""  # 覆盖/清空后不再沿用旧分类名;清空即提示「未分类」
    merged = {
        "title": overlay.get("title", base.title),
        "description": overlay.get("description", base.description),
        "environment": overlay.get("environment", base.environment),
        "service": overlay.get("service", base.service),
        "component_version": overlay.get("component_version", base.component_version),
        "recent_changes": overlay.get("recent_changes", base.recent_changes),
        "error_logs": overlay.get("error_logs", base.error_logs),
        "attempted_actions": overlay.get("attempted_actions", base.attempted_actions),
        "tags": list(overlay.get("tags", base.tags) or []),
        "category_id": cat_id,
    }
    return AnalysisInput(**merged, category_name=cat_name)


def has_substance(inp: AnalysisInput, records: list[FaultRecord]) -> bool:
    """空输入判定:无标题/描述/日志/变更/尝试/记录时不触发分析。"""
    texts = [
        inp.title,
        inp.description,
        inp.error_logs,
        inp.recent_changes,
        inp.attempted_actions,
        inp.environment,
        inp.service,
    ]
    if any(t and t.strip() for t in texts):
        return True
    return any(r.content and r.content.strip() for r in records)


def build_evidence_items(inp: AnalysisInput, records: list[FaultRecord]) -> list[EvidenceItem]:
    """为输入证据分配稳定 ID(基于字段名/记录 ID,同一输入得到同一 ID)。"""
    items: list[EvidenceItem] = []
    field_map = [
        ("desc", "description", inp.description),
        ("logs", "error_logs", inp.error_logs),
        ("changes", "recent_changes", inp.recent_changes),
        ("tried", "attempted_actions", inp.attempted_actions),
    ]
    for eid, source, text in field_map:
        if text and text.strip():
            items.append(EvidenceItem(evidence_id=eid, source=source, text=text.strip()))
    for rec in records:
        if rec.content and rec.content.strip():
            items.append(
                EvidenceItem(
                    evidence_id=f"rec:{rec.id}",
                    source=f"record:{rec.kind.value}",
                    text=rec.content.strip(),
                )
            )
    return items


def compute_input_hash(inp: AnalysisInput, records: list[FaultRecord]) -> str:
    """输入快照哈希:问题/环境/日志/检查记录任一变化即变(过期判定依据)。"""
    payload = {
        "title": inp.title,
        "description": inp.description,
        "environment": inp.environment,
        "service": inp.service,
        "component_version": inp.component_version,
        "recent_changes": inp.recent_changes,
        "error_logs": inp.error_logs,
        "attempted_actions": inp.attempted_actions,
        "tags": list(inp.tags or []),
        "category_id": inp.category_id,
        "records": [[r.id, r.kind.value, r.content] for r in records],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_dict(inp: AnalysisInput, records: list[FaultRecord]) -> dict:
    return {
        "title": inp.title,
        "description": inp.description,
        "environment": inp.environment,
        "service": inp.service,
        "component_version": inp.component_version,
        "error_logs": inp.error_logs[:2000],
        "records": [{"id": r.id, "kind": r.kind.value, "content": r.content[:2000]} for r in records],
    }


def _snippets_from_hits(hits: list[SearchHit]) -> list[KnowledgeSnippet]:
    return [
        KnowledgeSnippet(
            ref_id=h.ref_id,
            source_type=h.source_type,
            source_id=h.source_id,
            version=h.version,
            title=h.title,
            snippet=h.snippet[:MAX_SNIPPET_CHARS],
            link=h.link,
            status=h.status,
        )
        for h in hits
    ]


def _revalidate_snippets(
    db: Session, user: User, snippets: list[KnowledgeSnippet], query: str = ""
) -> tuple[list[KnowledgeSnippet], list[str]]:
    """模型请求发出前的片段复核:

    检索完成与聊天请求之间的窗口内来源可能被废弃/换版;不能仅靠模型返回后的
    引用清洗——失效知识一旦发出就无法撤回。此处按当前状态/版本重新校验;
    不再有效的内容直接排除,不进入提示词。

    仍有效来源的片段**保留查询相关性**(P2-6):旧命中片段仍可在当前来源验证
    (原文未变)则保留其命中位置;来源变化后按原查询在当前字段重新定位
    (与正式检索共用提取逻辑),不把排查依据统一替换成通用现象前缀。
    """
    from app.search.keyword import finalize_hits, source_snippet_for_query

    probes = [
        SearchHit(
            ref_id=s.ref_id,
            source_type=s.source_type,
            source_id=s.source_id,
            version=s.version,
            title=s.title,
            snippet=s.snippet,
            link=s.link,
            status=s.status,
            score=0.0,
        )
        for s in snippets
    ]
    kept_hits = finalize_hits(probes, db, user)
    kept_ids = {h.ref_id for h in kept_hits}
    refreshed: list[KnowledgeSnippet] = []
    dropped: list[str] = []
    for s in snippets:
        if s.ref_id not in kept_ids:
            dropped.append(s.ref_id)
            continue
        disp = source_snippet_for_query(
            db, s.source_type, s.source_id, query, old_snippet=s.snippet
        )
        if disp is None:
            dropped.append(s.ref_id)
            continue
        title, snippet = disp
        refreshed.append(
            KnowledgeSnippet(
                ref_id=s.ref_id,
                source_type=s.source_type,
                source_id=s.source_id,
                version=s.version,
                title=title[:200],
                snippet=snippet[:MAX_SNIPPET_CHARS],
                link=s.link,
                status=s.status,
            )
        )
    return refreshed, dropped


def _clip(text: str, limit: int) -> str:
    """整字段先脱敏、后截断:

    先截断会把长 PEM 的 END 行截掉,依赖 BEGIN/END 配对的打码因此失效,
    半截密钥材料会原样进入请求;因此每个进入提示词的字段都先打码再取前缀,
    最终提示词仍统一脱敏一次(兜底跨字段/元数据形态,审查 P1 #3)。
    """
    return sanitize_text(text)[:limit]


def _build_user_prompt(
    inp: AnalysisInput,
    evidence: list[EvidenceItem],
    snippets: list[KnowledgeSnippet],
    categories: list[str],
) -> str:
    evidence_block = "\n".join(
        f"[{e.evidence_id}] ({e.source}) {_clip(e.text, MAX_EVIDENCE_CHARS)}"
        for e in evidence
    ) or "(无)"
    if snippets:
        # 知识元数据显式整数版本(source_type=sop, source_id=2, version=2),
        # 不用 "v2" 之类的展示后缀——模型模仿提示文本写 version:"v2" 会直接违反
        # Schema(整数或 null)
        knowledge_block = "\n".join(
            f"[{s.ref_id}] {_clip(s.title, 200)} "
            f"(source_type={s.source_type}, source_id={s.source_id}, "
            f"version={s.version if s.version is not None else 'null'})\n"
            f"    {_clip(s.snippet, MAX_SNIPPET_CHARS)}"
            for s in snippets
        )
    else:
        knowledge_block = "(知识库无匹配,请基于输入证据分析,不要编造来源)"
    category_block = "、".join(categories) if categories else "(无分类)"
    prompt = USER_TEMPLATE.format(
        title=_clip(inp.title, 500),
        category=inp.category_name or "未分类",
        environment=_clip(inp.environment, 200),
        service=_clip(inp.service, 200),
        component_version=_clip(inp.component_version, 200),
        recent_changes=_clip(inp.recent_changes, 2000),
        evidence_block=evidence_block,
        knowledge_block=knowledge_block,
        category_block=category_block,
    )
    # 统一脱敏:所有进入模型的用户/知识字符串(含标题、分类名、知识片段标题等元数据)
    # 在最终提示词上一次处理,避免按字段零散调用时遗漏(审查 P1 #3)
    return sanitize_text(prompt)


def verify_report(
    report: AnalysisReport,
    evidence: list[EvidenceItem],
    snippets: list[KnowledgeSnippet],
    categories: list[str],
) -> tuple[AnalysisReport, list[str]]:
    """校验证据 ID、来源 ID/版本/状态与分类;无效引用移除并记录不完整原因。"""
    reasons: list[str] = []
    valid_evidence = {e.evidence_id for e in evidence}
    valid_refs = {s.ref_id: s for s in snippets}
    valid_categories = set(categories)

    def scrub_evidence(ids: list[str], where: str) -> list[str]:
        kept = [i for i in ids if i in valid_evidence]
        dropped = [i for i in ids if i not in valid_evidence]
        if dropped:
            reasons.append(f"{where}: 移除无效证据引用 {dropped}")
        return kept

    def scrub_refs(ids: list[str], where: str) -> list[str]:
        kept = []
        dropped = []
        for ref in ids:
            s = valid_refs.get(ref)
            if s is None:
                dropped.append(ref)
                continue
            # 来源 ID/版本必须与下发片段一致(防模型编造)
            kept.append(ref)
        if dropped:
            reasons.append(f"{where}: 移除无效来源引用 {dropped}")
        return kept

    # 已知事实必须绑定至少一个有效证据 ID(审查 P2 #10):
    # 无证据绑定的事实不是「已知事实」,移除并标记不完整,不允许以推测冒充事实。
    kept_facts = []
    for f in report.observed_facts:
        f = f.model_copy(update={"evidence_ids": scrub_evidence(f.evidence_ids, "observed_facts")})
        if f.evidence_ids:
            kept_facts.append(f)
        else:
            reasons.append("observed_facts: 移除未绑定有效证据的事实(已知事实必须有证据支持)")
    report.observed_facts = kept_facts
    new_hypotheses = []
    for h in report.hypotheses:
        h = h.model_copy(
            update={
                "evidence_ids": scrub_evidence(h.evidence_ids, f"hypotheses[{h.title}]"),
                "reference_ids": scrub_refs(h.reference_ids, f"hypotheses[{h.title}]"),
            }
        )
        if h.priority not in ("优先排查", "次优先", "证据不足"):
            h = h.model_copy(update={"priority": "次优先"})
        new_hypotheses.append(h)
    report.hypotheses = new_hypotheses

    new_refs = []
    seen = set()
    for ref in report.references:
        s = valid_refs.get(ref.ref_id)
        if s is None:
            reasons.append(f"references: 移除无效来源 {ref.ref_id}")
            continue
        if s.source_type != ref.source_type or s.source_id != ref.source_id:
            reasons.append(f"references: 来源 {ref.ref_id} 的类型/ID 与校验清单不一致,移除")
            continue
        if s.version is not None and ref.version != s.version:
            reasons.append(f"references: 来源 {ref.ref_id} 版本与当前有效版本不符,移除")
            continue
        if ref.ref_id in seen:
            continue
        seen.add(ref.ref_id)
        new_refs.append(ref)
    report.references = new_refs

    if report.suggested_category is not None and report.suggested_category not in valid_categories:
        reasons.append(f"suggested_category '{report.suggested_category}' 不在系统分类中,已置空")
        report.suggested_category = None

    # 标签收敛:去重、去空、限量
    tags: list[str] = []
    for t in report.suggested_tags:
        t = t.strip()
        if t and t not in tags:
            tags.append(t)
        if len(tags) >= 8:
            break
    report.suggested_tags = tags

    return report, reasons


# ---- 来源复核:模型返回/引用展示前重新读取审核/发布状态与当前版本 ----

_REF_ID_RE = re.compile(r"^(case|sop):(\d+)(?:#v(\d+))?$")


def _ref_identity(ref_id: str, fallback: KnowledgeReference | None) -> tuple[str, int, int | None] | None:
    """解析 ref_id → (来源类型, 来源 ID, 版本);fallback 提供模型给出的类型/版本。"""
    m = _REF_ID_RE.match(ref_id or "")
    if m is None:
        return None
    stype, sid, ver = m.group(1), int(m.group(2)), (int(m.group(3)) if m.group(3) else None)
    if fallback is not None:
        if fallback.source_type:
            stype = fallback.source_type
        if fallback.version is not None:
            ver = fallback.version
    return stype, sid, ver


def _source_currently_valid(db: Session, stype: str, sid: int, version: int | None) -> bool:
    """来源当前是否仍为有效正式知识(已审核案例 / 当前有效已发布 SOP)。

    用列级 select 直读数据库,绕过会话 identity map 的陈旧实例——
    模型调用期间的外部事务变更必须在这里被看到。
    """
    if stype == "case":
        row = db.execute(select(Case.status).where(Case.id == sid)).first()
        return row is not None and row[0] == CaseStatus.approved
    if stype == "sop":
        row = db.execute(select(Sop.status, Sop.version).where(Sop.id == sid)).first()
        if row is None or row[0] != SopStatus.published:
            return False
        if version is not None and row[1] != version:
            return False
        return True
    return False


def revalidate_report_sources(
    db: Session, report: AnalysisReport
) -> tuple[AnalysisReport, list[str]]:
    """模型返回及引用展示前重新读取来源状态/版本;失效引用移除并记录原因。

    调用前检索命中的来源可能在模型调用期间被废弃或被新版本替换(审查 P2 #9),
    因此不能只信任调用前的片段清单;缓存复用与历史展示遵循同一契约。
    """
    reasons: list[str] = []
    fallback_map = {r.ref_id: r for r in report.references}
    all_ids = set(fallback_map) | {rid for h in report.hypotheses for rid in h.reference_ids}
    valid_ids: set[str] = set()
    for rid in all_ids:
        ident = _ref_identity(rid, fallback_map.get(rid))
        if ident is not None and _source_currently_valid(db, *ident):
            valid_ids.add(rid)
        else:
            reasons.append(f"references: 来源 {rid} 已失效(被废弃/版本更新/不可见),移除")

    report.references = [r for r in report.references if r.ref_id in valid_ids]
    new_hypotheses = []
    for h in report.hypotheses:
        kept = [rid for rid in h.reference_ids if rid in valid_ids]
        if len(kept) != len(h.reference_ids):
            h = h.model_copy(update={"reference_ids": kept})
        new_hypotheses.append(h)
    report.hypotheses = new_hypotheses
    return report, reasons


def revalidate_report_dict(db: Session, report_dict: dict) -> tuple[dict, list[str]]:
    """对已落库的报告字典做来源复核(返回副本,不写回数据库)。"""
    report = AnalysisReport.model_validate(report_dict)
    report, reasons = revalidate_report_sources(db, report)
    return report.model_dump(), reasons


# ---- 单用户限流(单实例内存计数;多实例部署需外部限流,README 已注明) ----

_rate_lock = threading.Lock()
_rate_buckets: dict[int, list[float]] = {}
_inflight: dict[int, int] = {}


class RateLimited(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class EmptyInput(Exception):
    """输入为空:不调用模型、不落库(由 API 返回 400 提示补充输入)。"""


def _acquire_slot(user_id: int, settings: Settings) -> None:
    now = time.monotonic()
    with _rate_lock:
        window = _rate_buckets.setdefault(user_id, [])
        window[:] = [t for t in window if now - t < 60.0]
        if len(window) >= settings.ai_rate_limit_per_minute:
            raise RateLimited("分析请求过于频繁,请稍后再试(限流)")
        if _inflight.get(user_id, 0) >= settings.ai_max_concurrent_per_user:
            raise RateLimited("并发分析过多,请等待当前分析完成")
        window.append(now)
        _inflight[user_id] = _inflight.get(user_id, 0) + 1


def _release_slot(user_id: int) -> None:
    with _rate_lock:
        _inflight[user_id] = max(0, _inflight.get(user_id, 0) - 1)


# 查询向量(embedding)独立配额:与聊天分析限流桶分离。
# 单次助手 = 一次检索(可选向量)+ 一次分析,二者不得互相挤占;
# 缓存命中不占聊天额度;向量查询仅占 embedding 额度,限流时降级关键词。
_embed_rate_buckets: dict[int, list[float]] = {}
_embed_inflight: dict[int, int] = {}


def _acquire_embed_slot(user_id: int, settings: Settings) -> None:
    now = time.monotonic()
    with _rate_lock:
        window = _embed_rate_buckets.setdefault(user_id, [])
        window[:] = [t for t in window if now - t < 60.0]
        if len(window) >= settings.embedding_rate_limit_per_minute:
            raise RateLimited("embedding 查询过于频繁")
        if _embed_inflight.get(user_id, 0) >= settings.embedding_max_concurrent_per_user:
            raise RateLimited("embedding 并发查询过多")
        window.append(now)
        _embed_inflight[user_id] = _embed_inflight.get(user_id, 0) + 1


def _release_embed_slot(user_id: int) -> None:
    with _rate_lock:
        _embed_inflight[user_id] = max(0, _embed_inflight.get(user_id, 0) - 1)


# ---- 结果缓存:键含故障 + 用户 + 输入哈希 + 知识版本 + 模型配置版本;复用前重新校验 ----

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, int]] = {}  # key -> (写入时间, AiAnalysis.id)
CACHE_TTL_SECONDS = 600.0


def _cache_key(input_hash: str, knowledge_ver: str, config_ver: str, fault_id: int, user_id: int) -> str:
    """缓存键绑定故障与用户范围(审查 P1 #1):

    仅凭输入哈希会让相同输入的其他故障/其他用户命中并拿走别人的分析记录,
    绕过历史接口的越权隔离;因此故障 ID 与用户 ID 必须进入键,且命中后复核行归属。
    """
    return f"f{fault_id}|u{user_id}|{input_hash}|{knowledge_ver}|{config_ver}"


def _demo_outcome(
    inp: AnalysisInput,
    evidence: list[EvidenceItem],
    snippets: list[KnowledgeSnippet],
    categories: list[str],
) -> AnalysisOutcome:
    """演示模式的确定性模拟报告:仅引用真实证据与真实检索片段,明确标注演示数据。"""
    facts = [
        {"evidence_ids": [e.evidence_id], "fact": f"输入证据 {e.evidence_id} 提供了以下信息(模拟摘录)"}
        for e in evidence[:5]
    ]
    refs = [
        {
            "ref_id": s.ref_id,
            "source_type": s.source_type,
            "source_id": s.source_id,
            "version": s.version,
            "relevance": "演示模式按关键词命中的知识片段(模拟关联)",
        }
        for s in snippets
    ]
    hypotheses = []
    if evidence:
        hypotheses.append(
            {
                "title": "【模拟分析】待人工确认的候选方向",
                "rationale": "演示数据:依据为当前输入证据的表象整理,不代表真实模型判断。",
                "evidence_ids": [e.evidence_id for e in evidence[:3]],
                "reference_ids": [s.ref_id for s in snippets[:2]],
                "missing_information": ["演示数据:需人工补充检查结果后重新分析"],
                "verification": "演示数据:按下一步检查逐项人工验证",
                "priority": "证据不足",
            }
        )
    report = {
        "summary": f"【演示数据 / 模拟分析】故障「{inp.title[:80]}」的表象整理。此为 demo 模式生成的模拟报告,不代表真实模型能力。",
        "observed_facts": facts,
        "suggested_category": inp.category_name or (categories[0] if categories else None),
        "suggested_tags": ["演示数据"],
        "hypotheses": hypotheses,
        "missing_information": ["演示数据:证据不足时应补充检查记录后再分析"],
        "next_checks": [
            {
                "description": "演示数据:示例只读检查(仅展示,不会执行)",
                "command": "kubectl get pods -A | grep -v Running",
                "related_hypothesis": hypotheses[0]["title"] if hypotheses else "",
            }
        ],
        "references": refs,
        "insufficient_evidence": len(evidence) < 2,
    }
    return AnalysisOutcome(
        status=AiAnalysisStatus.success,
        report=report,
        incomplete_reasons=[],
        error_code="",
        error_message="",
        usage=None,  # 模拟分析不虚构 usage
        call_count=0,  # 未调用真实模型
        latency_ms=0,
        is_demo=True,
    )


def run_analysis(
    db: Session,
    user: User,
    fault: Fault,
    records: list[FaultRecord],
    settings: Settings,
    trigger_type: str = "manual",
    use_cache: bool = True,
    input_overlay: dict | None = None,
) -> tuple[AiAnalysis, bool]:
    """执行一次分析并落库;返回 (持久化的 AiAnalysis, 是否复用缓存)。

    input_overlay:未保存的表单编辑(覆盖已保存故障字段);分析不回写故障。
    空输入不调用模型;缓存命中(同输入/知识/配置版本)复用原记录,复用前重新校验权限与状态。
    """
    adapter = build_adapter(settings)  # 构造即复检 LOCAL_ONLY/local 目标
    inp = input_from_overlay(fault, input_overlay) if input_overlay else input_from_fault(fault)
    if not has_substance(inp, records):
        raise EmptyInput("输入为空或无实质内容,未触发分析;请补充问题描述、日志或检查记录")

    evidence = build_evidence_items(inp, records)
    input_hash = compute_input_hash(inp, records)
    config_ver = settings.ai_config_version

    # 正式搜索与右侧助手共用同一检索与来源校验逻辑(混合检索;向量不可用自动降级)
    retrieval = search_knowledge_hybrid(db, user, _query_from_input(inp), settings)
    hits, knowledge_ver = retrieval.hits, retrieval.knowledge_version
    snippets = _snippets_from_hits(hits)
    cat_rows = db.query(Category).order_by(Category.sort_order, Category.id).all()
    categories = [c.name for c in cat_rows]
    # 未保存分类覆盖参与分析时,分类名按覆盖后的 category_id 解析(不沿用数据库旧分类)
    if inp.category_id is not None:
        name = next((c.name for c in cat_rows if c.id == inp.category_id), None)
        if name is not None:
            inp.category_name = name

    key = _cache_key(input_hash, knowledge_ver, config_ver, fault.id, user.id)
    if use_cache:
        with _cache_lock:
            entry = _cache.get(key)
            cache_alive = bool(entry and time.monotonic() - entry[0] < CACHE_TTL_SECONDS)
            cached_id = entry[1] if cache_alive else None
        if cached_id is not None:
            cached_row = db.get(AiAnalysis, cached_id)
            if (
                cached_row is not None
                and cached_row.fault_id == fault.id  # 行归属复核:不得跨故障/跨用户复用
                and cached_row.created_by == user.id
                and cached_row.report
                and cached_row.status != AiAnalysisStatus.failed
            ):
                # 复用前重新校验:先按本次检索清单校验,再实时列级复核来源状态/版本
                # (检索完成与缓存返回之间的并发窗口内来源可能被废弃/替换)
                report_model, verify_reasons = verify_report(
                    AnalysisReport.model_validate(cached_row.report),
                    evidence,
                    snippets,
                    categories,
                )
                report_model, stale_reasons = revalidate_report_sources(db, report_model)
                if not verify_reasons and not stale_reasons:
                    return cached_row, True  # 同故障同用户同输入/知识/配置:复用原分析记录
                if stale_reasons and not verify_reasons:
                    # 来源实时复核失效:返回清理后的当前视图(与历史读取同一契约),
                    # 不写回历史记录(保留分析当时的结果),失效引用移除并标注不完整
                    return _cached_response_view(cached_row, report_model, stale_reasons), True
            # 记录已删、归属不符或报告与当前上下文不一致:丢弃缓存重新分析
            with _cache_lock:
                _cache.pop(key, None)

    _acquire_slot(user.id, settings)  # 频率与并发限制;缓存命中不占额度
    try:
        # 模型请求发出前的片段复核:失效来源剔除,仍有效来源保留
        # 查询相关片段/按查询在当前字段重定位;不把失效知识发给模型
        snippets, _dropped_refs = _revalidate_snippets(
            db, user, snippets, _query_from_input(inp)
        )
        outcome = _analyze_uncached(db, inp, evidence, snippets, categories, settings, adapter)
    finally:
        _release_slot(user.id)

    # 模型返回后重新读取来源状态/版本(调用期间可能被废弃/替换,审查 P2 #9);
    # 失效引用移除并计入不完整原因,缓存与历史展示遵循同一契约。
    if outcome.report is not None:
        report_model = AnalysisReport.model_validate(outcome.report)
        report_model, stale_reasons = revalidate_report_sources(db, report_model)
        if stale_reasons:
            outcome = AnalysisOutcome(
                status=(
                    AiAnalysisStatus.incomplete
                    if outcome.status == AiAnalysisStatus.success
                    else outcome.status
                ),
                report=report_model.model_dump(),
                incomplete_reasons=[*outcome.incomplete_reasons, *stale_reasons],
                error_code=outcome.error_code,
                error_message=outcome.error_message,
                usage=outcome.usage,
                call_count=outcome.call_count,
                latency_ms=outcome.latency_ms,
                is_demo=outcome.is_demo,
            )

    row = _persist(
        db,
        user,
        fault,
        trigger_type,
        input_hash,
        config_ver,
        knowledge_ver,
        outcome,
        input_snapshot=snapshot_dict(inp, records),
    )

    if outcome.report is not None and outcome.status != AiAnalysisStatus.failed:
        with _cache_lock:
            _cache[key] = (time.monotonic(), row.id)
    return row, False


def _cached_response_view(
    row: AiAnalysis, report_model: AnalysisReport, stale_reasons: list[str]
) -> AiAnalysis:
    """缓存复用时返回清理后的当前视图:响应副本,不写回数据库(与历史读取契约一致)。"""
    data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    data["report"] = report_model.model_dump()
    if data["status"] == AiAnalysisStatus.success:
        data["status"] = AiAnalysisStatus.incomplete
    data["incomplete_reasons"] = [*row.incomplete_reasons, *stale_reasons]
    return AiAnalysis(**data)  # 瞬态实例:仅用于响应序列化,不加入会话


def _query_from_input(inp: AnalysisInput) -> str:
    """检索查询:标题/描述/错误日志中的关键片段(截断,避免超长)。"""
    parts = [inp.title, inp.description[:400], inp.error_logs[:400], inp.service]
    return " ".join(p for p in parts if p)[:800]


def _analyze_uncached(
    db: Session,
    inp: AnalysisInput,
    evidence: list[EvidenceItem],
    snippets: list[KnowledgeSnippet],
    categories: list[str],
    settings: Settings,
    adapter,
) -> AnalysisOutcome:
    if isinstance(adapter, DemoAdapter):
        return _demo_outcome(inp, evidence, snippets, categories)

    user_prompt = _build_user_prompt(inp, evidence, snippets, categories)
    # 上下文长度限制(脱敏后)
    max_chars = settings.ai_max_context_chars
    if len(user_prompt) > max_chars:
        user_prompt = user_prompt[:max_chars] + "\n…[上下文已截断]"

    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_prompt),
    ]
    # 共用 JSON 生成/有限修复(语法错与结构错共享一次预算,修复继续声明 JSON 模式,
    # usage/call_count 逐次累计,调用失败不外抛——见 app.ai.json_out)
    completion = complete_json(
        adapter,
        messages,
        max_output_tokens=settings.ai_max_output_tokens,
        validate=AnalysisReport.model_validate,
        json_mode=True,
        subject="分析",
    )
    if completion.value is None:
        return AnalysisOutcome(
            status=AiAnalysisStatus.failed,
            report=None,
            incomplete_reasons=[],
            error_code=completion.error_code,
            error_message=completion.error_message,
            usage=completion.usage,
            call_count=completion.call_count,
            latency_ms=completion.latency_ms,
            is_demo=False,
        )

    report, reasons = verify_report(completion.value, evidence, snippets, categories)
    return AnalysisOutcome(
        status=AiAnalysisStatus.incomplete if reasons else AiAnalysisStatus.success,
        report=report.model_dump(),
        incomplete_reasons=reasons,
        error_code="",
        error_message="",
        usage=completion.usage,
        call_count=completion.call_count,
        latency_ms=completion.latency_ms,
        is_demo=False,
    )


def _persist(
    db: Session,
    user: User,
    fault: Fault,
    trigger_type: str,
    input_hash: str,
    config_ver: str,
    knowledge_ver: str,
    outcome: AnalysisOutcome,
    input_snapshot: dict | None = None,
) -> AiAnalysis:
    row = AiAnalysis(
        fault_id=fault.id,
        created_by=user.id,
        trigger_type=trigger_type,
        input_hash=input_hash,
        input_snapshot=input_snapshot or {},
        model_config_version=config_ver,
        knowledge_version=knowledge_ver,
        is_demo=outcome.is_demo,
        status=outcome.status,
        report=outcome.report,
        incomplete_reasons=outcome.incomplete_reasons,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        usage=outcome.usage,
        call_count=outcome.call_count,
        latency_ms=outcome.latency_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
