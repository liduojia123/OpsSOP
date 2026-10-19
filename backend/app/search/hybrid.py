"""向量候选检索与混合排序(第三阶段第三批)。

设计要点(AGENTS.md「业务可靠性」/ PROJECT_PROMPT §6):
- 向量查询复用第二批 embedding 适配层:查询文本最终请求边界脱敏、长度/超时/有限重试/
  频率限制;disabled 零调用;失败不循环、不切服务;
- pgvector 距离计算在数据库内完成(ORDER BY embedding <=> :qvec),不把向量读入 Python 排序;
- 只使用与当前配置兼容的活动索引(配置指纹/模型/维度/文本版本);一次请求绑定一个代次,
  代次切换最多重读一次,仍不一致则降级;不混代次/不同配置的候选;
- 排序与截断前满足权限与正式知识状态(仅已通过案例 + 当前有效已发布 SOP);
  索引陈旧(内容指纹与当前来源不一致)的向量候选直接弃用;
- 融合用 RRF(不相加不同量纲的关键词分数与向量距离),按来源去重、稳定排序、top_k 有上限,
  不展示未经校准的置信百分比;不引入 reranker/复杂检索框架;
- 向量路径不可用时返回关键词结果与安全降级原因;空结果与失败可区分;不自动重建索引;
- 片段从受控的当前来源字段生成;索引 text_excerpt 不是可信正文。

合成 embedding 只验证机制与排序口径,不代表真实语义检索质量。
"""

import hashlib
import time
from dataclasses import dataclass, field

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text as sql_text
from sqlalchemy.orm import Session

from app.ai.embeddings import (
    RETRY_BACKOFF_SECONDS,
    EmbeddingError,
    build_embedding_adapter,
)
from app.ai.sanitize import sanitize_text
from app.config import Settings
from app.models import Role, User
from app.search.index_text import INDEX_TEXT_VERSION, _unit_text_now
from app.search.keyword import (
    MAX_RESULTS,
    SearchHit,
    finalize_hits,
    knowledge_version,
    search_knowledge,
    source_display_fields,
    _make_snippet,
    extract_terms,
)

# RRF 常数(k):标准取值 60,平滑排名贡献
RRF_K = 60
# 向量候选池上限(融合前);融合后 top_k = MAX_RESULTS
MAX_VECTOR_CANDIDATES = 20
# 有界补取上限:SQL 前置过滤后仍可能被内容指纹陈旧等剔除,按需扩大窗口补取,
# 但单次请求最多考察这么多向量行(明确的查询成本上限,复审 P2-2)
MAX_VECTOR_FETCH = 60
# 查询文本长度上限(脱敏后截断;远小于索引文本上限)
MAX_QUERY_TEXT_CHARS = 500
# 代次切换时的重读上限(不反复产生 embedding 调用:查询向量只算一次)
MAX_GENERATION_RELOADS = 1
# 向量候选余弦距离上限:超过即视为与查询明显不相关,剔除(不强行凑结果;
# 阈值为保守值,语义效果以真实 embedding 联调为准)
MAX_COSINE_DISTANCE = 0.6

# 与 embedding_index 相同的列级快照读取(避免循环 import:就地定义最小字段)
from app.search.embedding_index import (  # noqa: E402
    _read_state,
    effective_status,
    ensure_pgvector,
)


@dataclass
class RetrievalResult:
    """一次检索的结果:命中 + 实际检索方式 + 降级信息(如实标注,不冒充语义检索)。"""

    hits: list[SearchHit]
    knowledge_version: str
    retrieval_mode: str = "keyword"  # keyword | hybrid
    degraded: bool = False
    degrade_reason: str = ""
    notes: list[str] = field(default_factory=list)  # 运行说明(如陈旧向量候选被剔除)


def _vector_distance_query(
    db: Session, query_vec: list[float], generation: int, limit: int, user: User
) -> list[tuple]:
    """pgvector 距离查询:在数据库内完成排序(ORDER BY embedding <=> :qvec)。

    当前状态、权限与有效版本约束在距离排名与候选 LIMIT 之前过滤(复审 P2-2):
    已废弃/换版/无权来源不占候选池。谓词与 search_knowledge/_finalize_hit 同源——
    正式知识仅「已通过案例(operator 可见性谓词)」与「当前有效已发布 SOP 版本」。
    """
    stmt = sql_text(
        """
        SELECT u.source_type, u.source_id, u.source_version, u.content_hash, u.embedding_dims,
               (u.embedding <=> :qvec) AS dist
        FROM embedding_index_units u
        WHERE u.generation = :gen
          AND (
            (u.source_type = 'case' AND EXISTS (
                SELECT 1 FROM cases c
                WHERE c.id = u.source_id
                  AND c.status = 'approved'
                  AND (:is_operator = 0 OR c.created_by = :user_id OR c.status = 'approved')
            ))
            OR (u.source_type = 'sop' AND EXISTS (
                SELECT 1 FROM sops s
                WHERE s.id = u.source_id
                  AND s.status = 'published'
                  AND s.version = u.source_version
            ))
          )
        ORDER BY u.embedding <=> :qvec
        LIMIT :k
        """
    ).bindparams(bindparam("qvec", type_=Vector()))
    rows = db.execute(
        stmt,
        {
            "qvec": query_vec,
            "gen": generation,
            "k": limit,
            "is_operator": 1 if user.role == Role.operator else 0,
            "user_id": user.id,
        },
    ).all()
    return rows


def _index_compatibility_error(view, settings: Settings) -> str:
    """活动索引与当前配置的兼容性检查;不兼容返回原因(调用方降级)。"""
    from app.search.embedding_index import _fingerprint

    if settings.embedding_mode == "disabled" or not settings.embedding_model:
        return "embedding 未启用(EMBEDDING_MODE=disabled),仅关键词检索"
    if view is None or view.active_generation <= 0:
        return "向量索引尚未构建,仅关键词检索"
    status = effective_status(view, settings)
    if status != "ready":
        return f"向量索引状态为 {status}(未构建/需要重建/失败),仅关键词检索"
    if view.active_fingerprint != _fingerprint(settings):
        return "索引由不同模型/端点/文本版本构建(配置指纹不一致),仅关键词检索"
    if view.active_text_version != INDEX_TEXT_VERSION:
        return "索引文本版本与当前实现不一致,仅关键词检索"
    return ""


def _build_vector_hits(
    db: Session,
    user: User,
    rows: list[tuple],
    query_terms: list[str],
    expected_dims: int,
    text_limit: int,
) -> tuple[list[SearchHit], list[str]]:
    """向量候选 → SearchHit:逐条来源复核 + 内容指纹陈旧识别;片段取当前来源。"""
    notes: list[str] = []
    hits: list[SearchHit] = []
    for source_type, source_id, source_version, content_hash, dims, dist in rows:
        if dist is not None and float(dist) > MAX_COSINE_DISTANCE:
            continue  # 明显不相关的向量候选:不强行返回(允许空结果)
        if int(dims) != expected_dims:
            notes.append(f"{source_type}:{source_id} 向量维度与活动索引不一致,已剔除")
            continue
        # 陈旧索引识别:行内指纹须与当前来源重建文本一致(内容更新后旧向量不是最新依据);
        # 重建文本长度口径与索引构建一致(embedding_max_input_chars)
        text_now = _unit_text_now(db, source_type, int(source_id), text_limit)
        if text_now is None or hashlib.sha256(text_now.encode("utf-8")).hexdigest() != content_hash:
            notes.append(f"{source_type}:{source_id} 索引内容已过期(来源已变化/移除),向量候选已剔除")
            continue
        disp = source_display_fields(db, source_type, int(source_id))
        if disp is None:
            continue
        title, snippet_src = disp
        hits.append(
            SearchHit(
                ref_id=f"{source_type}:{source_id}",
                source_type=source_type,
                source_id=int(source_id),
                version=int(source_version) if source_version is not None else None,
                title=title,
                snippet=_make_snippet(snippet_src, query_terms),
                link=f"/{('cases' if source_type == 'case' else 'sops')}/{source_id}",
                status="approved" if source_type == "case" else "published",
                score=0.0,  # 融合阶段赋 RRF 分;不使用未经校准的置信百分比
            )
        )
    return hits, notes


def _fetch_vector_hits(
    db: Session,
    user: User,
    query_vec: list[float],
    generation: int,
    query_terms: list[str],
    expected_dims: int,
    text_limit: int,
) -> tuple[list[SearchHit], list[str], int]:
    """向量候选获取:状态/权限/有效版本已在 SQL 前置过滤;内容指纹陈旧等
    无法完全 SQL 过滤的候选被剔除时按需扩大窗口补取(有界,复审 P2-2),
    避免失效近邻耗尽候选池;返回 (候选, 说明, 首轮 SQL 行数)。
    """
    notes: list[str] = []
    rows: list[tuple] = []
    hits: list[SearchHit] = []
    limit = MAX_VECTOR_CANDIDATES
    while True:
        rows = _vector_distance_query(db, query_vec, generation, limit, user)
        hits, notes = _build_vector_hits(
            db, user, rows, query_terms, expected_dims, text_limit
        )
        if (
            len(hits) >= MAX_VECTOR_CANDIDATES
            or len(rows) < limit
            or limit >= MAX_VECTOR_FETCH
        ):
            return hits[:MAX_VECTOR_CANDIDATES], notes, len(rows)
        limit = min(limit * 2, MAX_VECTOR_FETCH)


def _rrf_fuse(
    keyword_hits: list[SearchHit], vector_hits: list[SearchHit], top_k: int
) -> list[SearchHit]:
    """RRF 融合:各列表内按名次贡献 1/(k+rank);来源去重、稳定排序。"""
    rrf: dict[tuple, float] = {}
    chosen: dict[tuple, SearchHit] = {}
    for rank, h in enumerate(keyword_hits):
        key = (h.source_type, h.source_id, h.version)
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
        chosen.setdefault(key, h)
    for rank, h in enumerate(vector_hits):
        key = (h.source_type, h.source_id, h.version)
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
        if key not in chosen:
            chosen[key] = h
    ordered = sorted(
        chosen.items(),
        key=lambda kv: (-rrf[kv[0]], kv[0][0], kv[0][1], kv[0][2] if kv[0][2] is not None else -1),
    )
    out: list[SearchHit] = []
    for key, h in ordered[:top_k]:
        out.append(
            SearchHit(
                ref_id=h.ref_id,
                source_type=h.source_type,
                source_id=h.source_id,
                version=h.version,
                title=h.title,
                snippet=h.snippet,
                link=h.link,
                status=h.status,
                score=rrf[key],
            )
        )
    return out


def _retrieval_version(keyword_ver: str, settings: Settings, generation: int, mode: str) -> str:
    """检索版本:并入知识版本、检索策略与活动索引标识(缓存失效条件)。"""
    from app.search.embedding_index import _fingerprint

    raw = f"{keyword_ver}|{mode}|{settings.embedding_mode}|{_fingerprint(settings)}|g{generation}|{INDEX_TEXT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def search_knowledge_hybrid(
    db: Session,
    user: User,
    query: str,
    settings: Settings,
    embed_adapter=None,
    keyword_only: bool = False,
) -> RetrievalResult:
    """混合检索统一入口(正式搜索 API 与右侧助手共同后端路径)。

    向量路径不可用/失败时降级为关键词,并给出安全降级原因;
    不自动重建索引;查询向量只生成一次(代次切换重读不重复调用)。
    keyword_only=True:服务端纯关键词路径(零 embedding 调用;AI 状态未知时使用,
    复审 P2-4),不视为降级——这是受控的约束模式,不是向量路径失败。
    """

    def _out(
        hits: list[SearchHit],
        generation: int,
        mode: str,
        degraded: bool = False,
        reason: str = "",
        notes: list[str] | None = None,
    ) -> RetrievalResult:
        """统一返回出口:任何路径(成功/限流/配置异常/维度异常/embedding 失败)
        都在返回前对当前来源做最后复核(复审 P2-1)——远程等待期间被废弃/换版的
        命中不得随降级结果绕过正式知识有效性边界。"""
        return RetrievalResult(
            hits=finalize_hits(hits, db, user),
            knowledge_version=_retrieval_version(kw_ver, settings, generation, mode),
            retrieval_mode=mode,
            degraded=degraded,
            degrade_reason=reason,
            notes=notes or [],
        )

    kw_hits, kw_ver = search_knowledge(db, user, query)
    query_terms = [t.text for t in extract_terms(query)]

    if keyword_only:
        # 明确的服务端纯关键词路径:不检查索引、不生成查询向量、零外发
        return _out(kw_hits, 0, "keyword")

    # ---- 向量路径前置检查(任何一项不满足即降级,零 embedding 调用)----
    try:
        ensure_pgvector(db)
    except Exception as exc:  # pgvector 缺失/不可查
        return _out(
            kw_hits, 0, "keyword", True,
            f"向量索引不可用({type(exc).__name__}),仅关键词检索",
        )
    view = _read_state(db)
    compat_error = _index_compatibility_error(view, settings)
    if compat_error:
        return _out(kw_hits, 0, "keyword", True, compat_error)
    bound_generation = view.active_generation
    expected_dims = view.active_dims

    if not query.strip():
        return _out(kw_hits, bound_generation, "hybrid")

    # ---- 查询向量:脱敏 + 长度限制 + 独立频率限制(与聊天配额分离,复审 P2-3);
    #      失败降级,不循环/不切服务 ----
    from app.ai import service as ai_service

    query_text = sanitize_text(query.strip())[:MAX_QUERY_TEXT_CHARS]
    try:
        ai_service._acquire_embed_slot(user.id, settings)
    except ai_service.RateLimited:
        return _out(
            kw_hits, bound_generation, "keyword", True,
            "向量检索触发频率限制,本次仅关键词检索",
        )
    try:
        adapter = embed_adapter
        if adapter is None:
            try:
                adapter = build_embedding_adapter(settings)
            except EmbeddingError as exc:
                return _out(
                    kw_hits, bound_generation, "keyword", True,
                    f"embedding 配置不可用({exc.code}),仅关键词检索",
                )
        try:
            result = adapter.embed([query_text])
        except EmbeddingError as exc:
            # 429 限流直接降级:上游已要求放慢,同步立即重发只会再次增加请求。
            if exc.retryable and exc.code != "rate_limited":
                time.sleep(RETRY_BACKOFF_SECONDS)  # 有退避的有限重试一次;仍失败即降级
                try:
                    result = adapter.embed([query_text])
                except EmbeddingError as exc2:
                    return _out(
                        kw_hits, bound_generation, "keyword", True,
                        f"查询向量生成失败({exc2.code}),仅关键词检索",
                    )
            else:
                return _out(
                    kw_hits, bound_generation, "keyword", True,
                    f"查询向量生成失败({exc.code}),仅关键词检索",
                )
    finally:
        ai_service._release_embed_slot(user.id)

    if len(result.vectors) != 1:
        return _out(
            kw_hits, bound_generation, "keyword", True,
            "查询向量返回异常(条数不符),仅关键词检索",
        )
    query_vec = result.vectors[0]
    if len(query_vec) != expected_dims:
        return _out(
            kw_hits, bound_generation, "keyword", True,
            f"查询向量维度({len(query_vec)})与活动索引({expected_dims})不一致,仅关键词检索",
        )

    # 向量生成等待期间来源可能失效:融合前对关键词结果再做一次当前来源复核
    kw_hits = finalize_hits(kw_hits, db, user)

    # ---- 向量候选:绑定代次;代次切换最多重读一次;不混代次;
    #      SQL 前置过滤 + 陈旧候选有界补取(复审 P2-2)----
    notes: list[str] = []
    vec_hits, drop_notes, row_count = _fetch_vector_hits(
        db,
        user,
        query_vec,
        bound_generation,
        query_terms,
        expected_dims,
        max(1, settings.embedding_max_input_chars),
    )
    notes.extend(drop_notes)
    if row_count == 0:
        current = _read_state(db)
        if current is not None and current.active_generation != bound_generation:
            # 查询期间重建切换代次:用同一查询向量重读一次(不重复 embedding 调用)
            if current.active_dims != expected_dims or current.active_fingerprint != (
                view.active_fingerprint
            ):
                return _out(
                    kw_hits, 0, "keyword", True,
                    "查询期间索引配置已变化,本次仅关键词检索",
                )
            bound_generation = current.active_generation
            vec_hits, drop_notes, _ = _fetch_vector_hits(
                db,
                user,
                query_vec,
                bound_generation,
                query_terms,
                expected_dims,
                max(1, settings.embedding_max_input_chars),
            )
            notes.extend(drop_notes)

    # 向量候选在融合前做来源复核(排序/截断前满足权限与状态)
    vec_hits = finalize_hits(vec_hits, db, user)

    fused = _rrf_fuse(kw_hits, vec_hits, top_k=MAX_RESULTS)
    # 统一出口再次复核(截断后仍可能有并发失效)
    return _out(fused, bound_generation, "hybrid", notes=notes)
