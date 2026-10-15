"""关键词检索(正式知识:已审核案例 + 当前有效已发布 SOP)。

设计要点(AGENTS.md「业务可靠性」/ PROJECT_PROMPT §6):
- 不假设 PostgreSQL 默认分词覆盖中文:自建查询词——英文/数字词(错误码、组件名)
  原样匹配,CJK 字符串生成 2-gram 词参与匹配,评分按字段加权;
- 正式检索来源仅限:当前用户有权访问的「已通过案例」与「当前有效的已发布 SOP」;
  草稿、待审核、退回、历史版本(superseded)、废弃(deprecated)一律不进入候选;
- 查询与排序前过滤权限与状态;返回前再次校验(引用展示前的二次验证);
- 每条结果带来源类型、ID、版本、相关片段与定位链接;无匹配如实返回空,不编造。

本阶段为纯关键词检索;embedding/向量/混合检索属于第三阶段。
"""

import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Case, CaseStatus, Role, Sop, SopStatus, User

MAX_QUERY_CHARS = 200
MAX_RESULTS = 8
SNIPPET_RADIUS = 60

# CJK 统一表意文字范围(含扩展 A 基本够用;不做分词词典)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*")


@dataclass
class QueryTerm:
    text: str  # 归一化后的匹配词
    weight: float  # 基础权重(长词/错误码更高)


@dataclass
class SearchHit:
    ref_id: str
    source_type: str  # case | sop
    source_id: int
    version: int | None
    title: str
    snippet: str
    link: str
    status: str
    score: float


def extract_terms(query: str) -> list[QueryTerm]:
    """从查询提取匹配词:ASCII 词原样 + CJK 连串取 2-gram(单字降权)。"""
    if not query or not query.strip():
        return []
    q = query.strip()[:MAX_QUERY_CHARS]
    terms: dict[str, QueryTerm] = {}

    for m in _WORD_RE.finditer(q):
        word = m.group(0)
        # 错误码/组件名等 ASCII 词权重更高(通常更关键)
        weight = 2.0 if len(word) >= 3 else 1.0
        terms.setdefault(word.lower(), QueryTerm(word.lower(), weight))

    for m in _CJK_RE.finditer(q):
        run = m.group(0)
        if len(run) == 1:
            terms.setdefault(run, QueryTerm(run, 0.5))
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            terms.setdefault(bigram, QueryTerm(bigram, 1.0))

    return list(terms.values())


def _score_text(text: str, lowered_terms: list[tuple[str, float]], field_weight: float) -> tuple[float, list[str]]:
    if not text:
        return 0.0, []
    low = text.lower()
    score = 0.0
    matched: list[str] = []
    for term, weight in lowered_terms:
        count = low.count(term)
        if count:
            score += weight * field_weight * min(count, 3)
            matched.append(term)
    return score, matched


def _make_snippet(text: str, terms: list[str]) -> str:
    """定位片段:以第一个命中位置为中心截取。"""
    if not text:
        return ""
    low = text.lower()
    pos = -1
    hit_len = 0
    for term in terms:
        i = low.find(term)
        if i >= 0 and (pos < 0 or i < pos):
            pos, hit_len = i, len(term)
    if pos < 0:
        return text[: SNIPPET_RADIUS * 2]
    start = max(0, pos - SNIPPET_RADIUS)
    end = min(len(text), pos + hit_len + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


# 字段权重:标题/现象 > 根因/处理 > 日志/正文 > 标签
_CASE_FIELDS = [
    ("title", 3.0),
    ("symptom", 2.5),
    ("confirmed_root_cause", 2.0),
    ("handling_steps", 1.5),
    ("recovery_verification", 1.2),
    ("service", 1.5),
    ("environment", 1.0),
    ("component_version", 1.0),
]

_SOP_FIELDS = [
    ("title", 3.0),
    ("symptom", 2.5),
    ("check_steps", 2.0),
    ("handling_steps", 2.0),
    ("recovery_verification", 1.5),
    ("preconditions", 1.0),
    ("applicable_env", 1.0),
    ("rollback_steps", 1.0),
    ("notes", 1.0),
]


def _case_visible(user: User):
    if user.role == Role.operator:
        return or_(Case.created_by == user.id, Case.status == CaseStatus.approved)
    return True


def knowledge_version(db: Session, user: User) -> str:
    """知识版本:参与缓存键;正式知识的 id/updated_at/status 变化即变。"""
    import hashlib

    case_rows = db.execute(
        select(Case.id, Case.updated_at, Case.status)
        .where(_case_visible(user))
        .order_by(Case.id)
    ).all()
    sop_rows = db.execute(
        select(Sop.id, Sop.version, Sop.updated_at, Sop.status).order_by(Sop.id, Sop.version)
    ).all()
    raw = ";".join(
        [f"c{r[0]}:{r[1].isoformat() if r[1] else ''}:{getattr(r[2], 'value', r[2])}" for r in case_rows]
        + [
            f"s{r[0]}v{r[1]}:{r[2].isoformat() if r[2] else ''}:{getattr(r[3], 'value', r[3])}"
            for r in sop_rows
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _finalize_hit(hit: SearchHit, db: Session, user: User) -> SearchHit | None:
    """返回前二次校验:来源仍须有效、当前用户可访问;否则丢弃。

    用列级 select 直读数据库,绕过会话 identity map 的陈旧实例——
    首次加载与本复核之间的独立事务变更(废弃/换版)必须在这里被看到
    (修复复审 P2:db.get() 返回旧实例,失效命中未被剔除,旧正文进入模型上下文)。
    """
    if hit.source_type == "case":
        row = db.execute(
            select(Case.status, Case.created_by).where(Case.id == hit.source_id)
        ).first()
        if row is None or row[0] != CaseStatus.approved:
            return None
        # 已通过案例对所有登录用户可读;此处仍复核可见性谓词
        if user.role == Role.operator and row[1] != user.id and row[0] != CaseStatus.approved:
            return None
    elif hit.source_type == "sop":
        row = db.execute(select(Sop.status, Sop.version).where(Sop.id == hit.source_id)).first()
        if row is None or row[0] != SopStatus.published:
            return None
        if hit.version is not None and row[1] != hit.version:
            return None  # 版本已变(不再是当前有效版本)
    else:
        return None
    return hit


def search_knowledge(db: Session, user: User, query: str) -> tuple[list[SearchHit], str]:
    """关键词检索正式知识。返回 (命中列表, 知识版本)。无匹配返回空列表。"""
    terms = extract_terms(query)
    if not terms:
        return [], knowledge_version(db, user)
    lowered = [(t.text, t.weight) for t in terms]
    matched_terms = [t.text for t in terms]

    hits: list[SearchHit] = []

    # 案例:仅已通过(正式诊断依据);排序前过滤状态与可见性
    case_stmt = select(Case).where(_case_visible(user), Case.status == CaseStatus.approved)
    for case in db.scalars(case_stmt).all():
        score = 0.0
        all_matched: list[str] = []
        best_field_text = ""
        best_field_matched: list[str] = []
        for field_name, field_weight in _CASE_FIELDS:
            text = getattr(case, field_name, "") or ""
            if field_name == "title" and case.tags:
                text = text + " " + " ".join(str(t) for t in case.tags)
            s, matched = _score_text(text, lowered, field_weight)
            if s > 0:
                score += s
                all_matched.extend(matched)
                if not best_field_matched and matched:
                    best_field_text, best_field_matched = text, matched
        if score <= 0:
            continue
        snippet_src = best_field_text or case.symptom or case.title
        hits.append(
            SearchHit(
                ref_id=f"case:{case.id}",
                source_type="case",
                source_id=case.id,
                version=None,
                title=case.title,
                snippet=_make_snippet(snippet_src, best_field_matched or matched_terms),
                link=f"/cases/{case.id}",
                status="approved",
                score=score,
            )
        )

    # SOP:仅当前有效已发布版本;superseded/deprecated/草稿/待审/退回不进入
    sop_stmt = select(Sop).where(Sop.status == SopStatus.published)
    for sop in db.scalars(sop_stmt).all():
        score = 0.0
        all_matched = []
        best_field_text = ""
        best_field_matched = []
        for field_name, field_weight in _SOP_FIELDS:
            text = getattr(sop, field_name, "") or ""
            if field_name == "title" and sop.tags:
                text = text + " " + " ".join(str(t) for t in sop.tags)
            s, matched = _score_text(text, lowered, field_weight)
            if s > 0:
                score += s
                all_matched.extend(matched)
                if not best_field_matched and matched:
                    best_field_text, best_field_matched = text, matched
        if score <= 0:
            continue
        snippet_src = best_field_text or sop.symptom or sop.title
        hits.append(
            SearchHit(
                ref_id=f"sop:{sop.id}",
                source_type="sop",
                source_id=sop.id,
                version=sop.version,
                title=sop.title,
                snippet=_make_snippet(snippet_src, best_field_matched or matched_terms),
                link=f"/sops/{sop.id}",
                status="published",
                score=score,
            )
        )

    hits.sort(key=lambda h: (-h.score, h.source_type, h.source_id))
    hits = hits[:MAX_RESULTS]

    # 返回前二次校验
    verified = [h for h in (_finalize_hit(h, db, user) for h in hits) if h is not None]
    return verified, knowledge_version(db, user)
