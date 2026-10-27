"""关键词检索(正式知识:已审核案例 + 当前有效已发布 SOP)。

检索范围与一致性约束:
- 不假设 PostgreSQL 默认分词覆盖中文:自建查询词——英文/数字词(错误码、组件名)
  原样匹配,CJK 字符串生成 2-gram 词参与匹配,评分按字段加权;
- 正式检索来源仅限:当前用户有权访问的「已通过案例」与「当前有效的已发布 SOP」;
  草稿、待审核、退回、历史版本(superseded)、废弃(deprecated)一律不进入候选;
- 查询与排序前过滤权限与状态;返回前再次校验(引用展示前的二次验证);
- 每条结果带来源类型、ID、版本、相关片段与定位链接;无匹配如实返回空,不编造。

关键词路径为向量/混合检索的基础通道:hybrid.py 在其上融合向量候选,
两者共用本模块的来源过滤与返回前复核;关键词能力独立可用,向量不可用时自动降级回本路径。
"""

import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Case, CaseStatus, Role, Sop, SopStatus, User

MAX_QUERY_CHARS = 200
MAX_RESULTS = 8
SNIPPET_RADIUS = 60
# 片段进入模型上下文前的长度上限(与 ai.service.MAX_SNIPPET_CHARS 同值;此处独立定义避免循环 import)
MAX_INDEX_SNIPPET_CHARS = 800

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
    (防止 db.get() 返回旧实例,使失效命中或旧正文进入模型上下文)。
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


def finalize_hits(hits: list[SearchHit], db: Session, user: User) -> list[SearchHit]:
    """批量返回前复核(正式搜索与右侧助手共用同一来源校验逻辑)。"""
    return [h for h in (_finalize_hit(h, db, user) for h in hits) if h is not None]


def _best_field_hit(obj, fields, lowered: list[tuple[str, float]]) -> tuple[float, str, list[str]]:
    """对来源对象的字段组打分,返回 (总分, 最佳命中字段文本, 最佳命中词)。

    「最佳」=加权得分最高的字段,而非第一个出现命中的字段:
    多词查询下弱词命中标题不得掩盖错误码等强词命中排查/处理字段,
    否则查询相关依据(如排查指令)会从片段中消失。
    正式检索(search_knowledge)与发模型前片段复核(source_snippet_for_query)
    共用这一提取逻辑,保证助手依据与搜索展示依据不分叉。
    """
    score = 0.0
    best_score = -1.0
    best_field_text = ""
    best_field_matched: list[str] = []
    for field_name, field_weight in fields:
        text = getattr(obj, field_name, "") or ""
        if field_name == "title" and getattr(obj, "tags", None):
            text = text + " " + " ".join(str(t) for t in obj.tags)
        s, matched = _score_text(text, lowered, field_weight)
        if s > 0:
            score += s
            if matched and s > best_score:
                best_score, best_field_text, best_field_matched = s, text, matched
    return score, best_field_text, best_field_matched


def _source_field_texts(obj, fields) -> str:
    """来源对象当前字段拼接(用于旧片段可验证性检查)。"""
    return "\n".join((getattr(obj, name, "") or "") for name, _ in fields)


def _read_source_fields(db: Session, source_type: str, source_id: int):
    """列级直读当前来源字段(绕过会话 identity map 陈旧实例,与 _finalize_hit 同理由)。

    检索与发模型之间的窗口内内容变更必须在这里被看到,否则旧正文会被
    「仍可验证」误判为有效(来源内容变化场景)。
    """
    from types import SimpleNamespace

    if source_type == "case":
        cols = [
            Case.title,
            Case.symptom,
            Case.environment,
            Case.service,
            Case.component_version,
            Case.confirmed_root_cause,
            Case.handling_steps,
            Case.recovery_verification,
            Case.tags,
        ]
        fields = _CASE_FIELDS
        id_col = Case.id
    elif source_type == "sop":
        cols = [
            Sop.title,
            Sop.symptom,
            Sop.check_steps,
            Sop.handling_steps,
            Sop.recovery_verification,
            Sop.preconditions,
            Sop.applicable_env,
            Sop.rollback_steps,
            Sop.notes,
            Sop.tags,
        ]
        fields = _SOP_FIELDS
        id_col = Sop.id
    else:
        return None, None
    row = db.execute(select(*cols).where(id_col == source_id)).first()
    if row is None:
        return None, None
    obj = SimpleNamespace(**dict(row._mapping))
    return obj, fields


def source_snippet_for_query(
    db: Session,
    source_type: str,
    source_id: int,
    query: str,
    old_snippet: str = "",
) -> tuple[str, str] | None:
    """按查询在当前来源字段上定位相关片段(与正式检索同一提取逻辑)。

    - 旧命中片段仍能在当前来源中验证(原文未变)→ 保留其命中位置,
      不把查询相关依据退化成通用现象前缀;
    - 来源已变化/旧片段不再可验证 → 以当前字段按查询重新定位(不沿用旧正文);
    - 查询无词命中时回退现象/标题(与 search_knowledge 的片段底稿一致)。
    返回 (标题, 片段);来源不存在返回 None。
    """
    terms = extract_terms(query)
    lowered = [(t.text, t.weight) for t in terms]
    matched_terms = [t.text for t in terms]

    obj, fields = _read_source_fields(db, source_type, source_id)
    if obj is None:
        return None

    title = obj.title or ""
    if old_snippet:
        # 旧片段(去省略号)仍是当前来源的原文子集 → 来源未变或该内容仍有效,保留
        core = old_snippet.strip("…")
        if core and core in _source_field_texts(obj, fields):
            return title, old_snippet[:MAX_INDEX_SNIPPET_CHARS]

    _score, best_field_text, best_field_matched = _best_field_hit(obj, fields, lowered)
    fallback = getattr(obj, "symptom", "") or title
    snippet_src = best_field_text or fallback
    return title, _make_snippet(snippet_src, best_field_matched or matched_terms)[
        :MAX_INDEX_SNIPPET_CHARS
    ]


def source_display_fields(db: Session, source_type: str, source_id: int) -> tuple[str, str] | None:
    """按当前来源读取 (标题, 片段底稿) 供检索结果展示。

    片段从受控的当前来源字段生成;索引 text_excerpt 不作为可信正文。
    """
    if source_type == "case":
        row = db.execute(
            select(Case.title, Case.symptom, Case.confirmed_root_cause).where(Case.id == source_id)
        ).first()
        if row is None:
            return None
        return row[0] or "", (row[1] or row[2] or row[0] or "")
    if source_type == "sop":
        row = db.execute(
            select(Sop.title, Sop.symptom, Sop.check_steps).where(Sop.id == source_id)
        ).first()
        if row is None:
            return None
        return row[0] or "", (row[1] or row[2] or row[0] or "")
    return None


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
        score, best_field_text, best_field_matched = _best_field_hit(case, _CASE_FIELDS, lowered)
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
        score, best_field_text, best_field_matched = _best_field_hit(sop, _SOP_FIELDS, lowered)
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
