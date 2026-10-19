"""embedding 索引构建与状态管理(第三阶段第二批)。

设计要点(AGENTS.md「业务可靠性」/ 本批要求):
- 只纳入已通过案例与当前有效已发布 SOP;单元行可定位来源类型/ID/版本与内容指纹;
- 配置指纹绑定 模式|端点|模型|文本版本(不含密钥):相同维度不同模型不混用,
  配置/文本版本变化后旧代次标「需要重建」,不静默继续使用;
- 构建 = 新代次:数据库 CAS 领取(build_attempt 递增,领取后立即提交,
  远程模型调用不持有行锁);落库前逐条复核来源;结果按 attempt+指纹条件提交
  (配置切换/被接管后旧任务不得提交为当前有效结果);
- 重建失败丢弃半成品代次,不标记为可用;与当前配置兼容的完整旧代次保留可用,
  不兼容则标需要重建;部分成功明确记录覆盖范围与未纳入项,不宣称完整构建成功;
- 同指纹同内容直接复用既有向量(不重复计费、不重复记录);
- 状态查询为纯读取(不触发模型调用);登录/启动/页面加载不会自动构建。

合成向量(测试夹具)只验证协议、存储与状态机制,不代表真实语义检索能力。
"""

import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.embeddings import (
    RETRY_BACKOFF_SECONDS,
    EmbeddingAdapter,
    EmbeddingError,
    build_embedding_adapter,
)
from app.ai.json_out import aggregate_usage
from app.config import Settings
from app.models import (
    Case,
    CaseStatus,
    EmbeddingIndexState,
    EmbeddingIndexStatus,
    EmbeddingIndexUnit,
    Sop,
    SopStatus,
    utcnow,
)
from app.search.index_text import (
    INDEX_TEXT_VERSION,
    IndexUnitDraft,
    collect_index_units,
    config_fingerprint,
    is_unit_current,
)

# 构建执行者的过期时限:超过后允许新任务接管(防进程崩溃卡死)
BUILD_STALE_SECONDS = 30 * 60


class IndexBuildRejected(Exception):
    """构建前置条件不满足(未配置/pgvector 缺失/配置错误):明确原因,不静默降级。"""


class IndexBuildConflict(Exception):
    """构建冲突:已有构建在途,或本任务已被接管/配置切换,结果不得提交。"""


@dataclass
class BuildClaim:
    attempt: int
    generation: int
    fingerprint: str
    model: str


@dataclass
class BuildOutcome:
    result: str  # success | partial | failed
    built_units: int = 0
    reused_units: int = 0
    embedded_calls: int = 0
    usage: dict | None = None
    skipped: list[str] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""


def ensure_pgvector(db: Session) -> None:
    """确认 pgvector 扩展可用;缺失/权限不足给出明确原因,不用普通数组冒充。"""
    try:
        row = db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
    except Exception as exc:  # 数据库连接/权限异常
        raise IndexBuildRejected(
            f"无法检查 pgvector 扩展状态: {type(exc).__name__};请确认数据库可达且当前用户有系统目录读取权限"
        ) from exc
    if row is None:
        raise IndexBuildRejected(
            "数据库缺少 pgvector 扩展(extname=vector);请改用含 pgvector 的镜像"
            "(如 pgvector/pgvector:pg16)并重新执行迁移;不得以普通 JSON 数组冒充向量索引"
        )


def _fingerprint(settings: Settings) -> str:
    return config_fingerprint(
        settings.embedding_mode, settings.embedding_base_url, settings.embedding_model
    )


def _ensure_state(db: Session) -> EmbeddingIndexState:
    state = db.get(EmbeddingIndexState, 1)
    if state is not None:
        return state
    state = EmbeddingIndexState(id=1, status=EmbeddingIndexStatus.pending.value)
    db.add(state)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        state = db.get(EmbeddingIndexState, 1)
        if state is None:  # 极端竞态下再次缺失:重试一次
            raise IndexBuildConflict("索引状态初始化冲突,请重试")
    return state


def _is_building_fresh(updated_at) -> bool:
    if updated_at is None:
        return False
    updated = updated_at
    if updated.tzinfo is None:
        from datetime import timezone

        updated = updated.replace(tzinfo=timezone.utc)
    return (utcnow() - updated).total_seconds() < BUILD_STALE_SECONDS


@dataclass
class _StateView:
    """状态行的列级只读快照(绕过 identity map:并发窗口必须读到最新值)。"""

    status: str
    active_generation: int
    active_fingerprint: str
    active_model: str
    active_dims: int
    active_text_version: str
    unit_count: int
    built_at: object
    building_generation: int
    build_attempt: int
    error_code: str
    error_message: str
    updated_at: object


_STATE_COLUMNS = (
    EmbeddingIndexState.status,
    EmbeddingIndexState.active_generation,
    EmbeddingIndexState.active_fingerprint,
    EmbeddingIndexState.active_model,
    EmbeddingIndexState.active_dims,
    EmbeddingIndexState.active_text_version,
    EmbeddingIndexState.unit_count,
    EmbeddingIndexState.built_at,
    EmbeddingIndexState.building_generation,
    EmbeddingIndexState.build_attempt,
    EmbeddingIndexState.error_code,
    EmbeddingIndexState.error_message,
    EmbeddingIndexState.updated_at,
)


def _read_state(db: Session) -> _StateView | None:
    row = db.execute(
        select(*_STATE_COLUMNS).where(EmbeddingIndexState.id == 1)
    ).first()
    return _StateView(*row) if row is not None else None


def _recover_if_stale(
    db: Session,
    settings: Settings,
    observed_attempt: int,
    observed_generation: int,
) -> bool:
    """过期中断执行者的条件回收(复审 P2-1/P2-3 执行者契约):

    仅当仍是读取时看到的那个过期 building(attempt + 代次 + 状态 + 过期时间)才写入;
    未命中(已被新执行者领取/已完成)返回 False,调用方重新读取;
    绝不覆盖新领取者的 building 状态。
    """
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=BUILD_STALE_SECONDS)
    row = db.execute(
        select(EmbeddingIndexState.active_generation, EmbeddingIndexState.active_fingerprint).where(
            EmbeddingIndexState.id == 1
        )
    ).first()
    active_gen = row[0] if row else 0
    active_fp = row[1] if row else ""
    if active_gen > 0 and active_fp == _fingerprint(settings):
        new_status = EmbeddingIndexStatus.ready.value  # 兼容旧代次保留可用
    elif active_gen > 0:
        new_status = EmbeddingIndexStatus.needs_rebuild.value  # 不兼容:不混用
    else:
        new_status = EmbeddingIndexStatus.failed.value
    res = db.execute(
        update(EmbeddingIndexState)
        .where(
            EmbeddingIndexState.id == 1,
            EmbeddingIndexState.build_attempt == observed_attempt,
            EmbeddingIndexState.building_generation == observed_generation,
            EmbeddingIndexState.status == EmbeddingIndexStatus.building.value,
            EmbeddingIndexState.updated_at < stale_cutoff,
        )
        .values(
            status=new_status,
            error_code="interrupted",
            error_message="构建中断(执行者超时未完成);半成品未激活,可重新发起构建恢复",
            updated_at=now,
        )
    )
    db.commit()
    return res.rowcount == 1


def effective_status(view: _StateView, settings: Settings) -> str:
    """计算有效状态:未配置/待构建/构建中/可用/需要重建/失败(纯计算,不写库)。"""
    if settings.embedding_mode == "disabled" or not settings.embedding_model:
        return EmbeddingIndexStatus.unconfigured.value
    if view.status == EmbeddingIndexStatus.building.value and _is_building_fresh(view.updated_at):
        return EmbeddingIndexStatus.building.value
    if view.active_generation > 0:
        if view.active_fingerprint == _fingerprint(settings):
            return EmbeddingIndexStatus.ready.value
        return EmbeddingIndexStatus.needs_rebuild.value
    if view.error_code:
        return EmbeddingIndexStatus.failed.value
    return EmbeddingIndexStatus.pending.value


def get_status(db: Session, settings: Settings) -> dict:
    """索引状态快照:不触发任何模型调用;除过期中断执行者的条件回收外不写库。

    回收写入按读取时的 attempt/代次/状态/过期时间做原子条件更新(复审 P2-1);
    未命中(被并发领取/已完成)则重新读取,绝不把新领取者的 building 覆盖成失败。
    """
    view: _StateView | None = None
    for _ in range(3):
        _ensure_state(db)
        view = _read_state(db)
        if (
            view is None
            or view.status != EmbeddingIndexStatus.building.value
            or _is_building_fresh(view.updated_at)
            or settings.embedding_mode == "disabled"
            or not settings.embedding_model
        ):
            break
        if _recover_if_stale(db, settings, view.build_attempt, view.building_generation):
            if view.building_generation > 0:
                db.execute(
                    delete(EmbeddingIndexUnit).where(
                        EmbeddingIndexUnit.generation == view.building_generation
                    )
                )
                db.commit()
        # 回收成功或未命中都重新读取(未命中 = 状态已变化,不得按旧读覆盖)

    status = effective_status(view, settings)
    active_rows = db.execute(
        select(func.count()).select_from(EmbeddingIndexUnit).where(
            EmbeddingIndexUnit.generation == view.active_generation
        )
        if view.active_generation > 0
        else select(func.count()).select_from(EmbeddingIndexUnit).where(
            EmbeddingIndexUnit.generation == -1
        )
    ).scalar() or 0
    total_rows = db.execute(
        select(func.count()).select_from(EmbeddingIndexUnit)
    ).scalar() or 0

    return {
        "status": status,
        "embedding_mode": settings.embedding_mode,
        "embedding_model": settings.embedding_model,
        "config_fingerprint": _fingerprint(settings),
        "text_version": INDEX_TEXT_VERSION,
        "active_generation": view.active_generation,
        "active_fingerprint": view.active_fingerprint,
        "active_model": view.active_model,
        "embedding_dims": view.active_dims,
        "unit_count": int(active_rows),
        "total_rows": int(total_rows),
        "building_generation": view.building_generation,
        "build_attempt": view.build_attempt,
        "error_code": view.error_code,
        "error_message": view.error_message,
        "built_at": view.built_at,
        "updated_at": view.updated_at,
    }


def _claim_build(db: Session, settings: Settings) -> BuildClaim | None:
    """原子领取构建任务(CAS 条件更新):并发至多一个执行者。

    building_generation 单调递增(每次领取新代次),互不覆盖;
    领取后立即提交,远程模型调用期间不持有数据库行锁。
    """
    now = utcnow()
    fp = _fingerprint(settings)
    stale_cutoff = now - timedelta(seconds=BUILD_STALE_SECONDS)
    state = _ensure_state(db)
    stmt = (
        update(EmbeddingIndexState)
        .where(
            EmbeddingIndexState.id == 1,
            or_(
                EmbeddingIndexState.status != EmbeddingIndexStatus.building.value,
                EmbeddingIndexState.updated_at < stale_cutoff,
            ),
        )
        .values(
            status=EmbeddingIndexStatus.building.value,
            building_generation=EmbeddingIndexState.building_generation + 1,
            build_attempt=EmbeddingIndexState.build_attempt + 1,
            build_fingerprint=fp,
            error_code="",
            error_message="",
            updated_at=now,
        )
        .returning(EmbeddingIndexState.build_attempt, EmbeddingIndexState.building_generation)
    )
    row = db.execute(stmt).first()
    db.commit()  # 尽快释放行锁
    if row is None:
        return None
    return BuildClaim(
        attempt=int(row[0]),
        generation=int(row[1]),
        fingerprint=fp,
        model=settings.embedding_model,
    )


def _touch_build(db: Session, claim: BuildClaim) -> None:
    """批次间刷新执行者心跳(防止长构建被误判中断)。"""
    db.execute(
        update(EmbeddingIndexState)
        .where(
            EmbeddingIndexState.id == 1,
            EmbeddingIndexState.build_attempt == claim.attempt,
        )
        .values(updated_at=utcnow())
    )
    db.commit()


def _finalize_success(db: Session, claim: BuildClaim, unit_count: int, dims: int) -> bool:
    """条件提交:仅本执行者(attempt+代次+指纹)可激活新代次;成功后清理非活动代次。"""
    now = utcnow()
    res = db.execute(
        update(EmbeddingIndexState)
        .where(
            EmbeddingIndexState.id == 1,
            EmbeddingIndexState.build_attempt == claim.attempt,
            EmbeddingIndexState.status == EmbeddingIndexStatus.building.value,
            EmbeddingIndexState.building_generation == claim.generation,
            EmbeddingIndexState.build_fingerprint == claim.fingerprint,
        )
        .values(
            status=EmbeddingIndexStatus.ready.value,
            active_generation=claim.generation,
            active_fingerprint=claim.fingerprint,
            active_model=claim.model,
            active_dims=dims,
            active_text_version=INDEX_TEXT_VERSION,
            unit_count=unit_count,
            built_at=now,
            updated_at=now,
            error_code="",
            error_message="",
        )
    )
    if res.rowcount != 1:
        return False
    # 成功替换:清掉旧/其他代次,不留重复记录(同内容也不会随多次构建累积)
    db.execute(
        delete(EmbeddingIndexUnit).where(EmbeddingIndexUnit.generation != claim.generation)
    )
    db.commit()
    return True


def _finalize_failure(
    db: Session,
    claim: BuildClaim,
    error_code: str,
    error_message: str,
    current_fingerprint: str | None = None,
) -> bool:
    """失败收尾:丢弃本代次半成品(不激活),结束本执行者租约,按活动代次兼容性决定状态。

    状态判定以当前配置指纹为准(配置切换后的退出不得沿用领取时指纹做兼容判断);
    写入按 attempt+代次条件更新:已被接管则不动新执行者状态(执行者契约)。
    """
    db.execute(
        delete(EmbeddingIndexUnit).where(EmbeddingIndexUnit.generation == claim.generation)
    )
    row = db.execute(
        select(
            EmbeddingIndexState.active_generation,
            EmbeddingIndexState.active_fingerprint,
        ).where(EmbeddingIndexState.id == 1)
    ).first()
    active_gen = row[0] if row else 0
    active_fp = row[1] if row else ""
    decision_fp = claim.fingerprint if current_fingerprint is None else current_fingerprint
    if active_gen > 0 and active_fp == decision_fp:
        new_status = EmbeddingIndexStatus.ready.value  # 兼容旧代次保留可用
    elif active_gen > 0:
        new_status = EmbeddingIndexStatus.needs_rebuild.value  # 不兼容:不混用
    else:
        new_status = EmbeddingIndexStatus.failed.value
    res = db.execute(
        update(EmbeddingIndexState)
        .where(
            EmbeddingIndexState.id == 1,
            EmbeddingIndexState.build_attempt == claim.attempt,
            EmbeddingIndexState.building_generation == claim.generation,
        )
        .values(
            status=new_status,
            error_code=error_code,
            error_message=error_message,
            updated_at=utcnow(),
        )
    )
    db.commit()
    return res.rowcount == 1


def _reusable_vectors(db: Session, fingerprint: str) -> dict[tuple, tuple[str, list[float], int]]:
    """同配置指纹的既有向量(任意代次)可直接复用:同内容不重复计费。

    返回 {(source_type, source_id, unit_key): (content_hash, vector, dims)}。
    """
    rows = db.execute(
        select(
            EmbeddingIndexUnit.source_type,
            EmbeddingIndexUnit.source_id,
            EmbeddingIndexUnit.unit_key,
            EmbeddingIndexUnit.content_hash,
            EmbeddingIndexUnit.embedding,
            EmbeddingIndexUnit.embedding_dims,
        ).where(EmbeddingIndexUnit.config_fingerprint == fingerprint)
    ).all()
    out: dict[tuple, tuple[str, list[float], int]] = {}
    for st, sid, key, chash, vec, dims in rows:
        k = (st, sid, key)
        if k not in out:
            vector = vec.tolist() if hasattr(vec, "tolist") else [float(x) for x in vec]
            out[k] = (chash, vector, int(dims or len(vector)))
    return out


def _embed_batched(
    adapter: EmbeddingAdapter,
    drafts: list[IndexUnitDraft],
    settings: Settings,
    usages: list,
    calls_box: list,
) -> dict[tuple, list[float]]:
    """分批嵌入(有限重试一次)。

    usages/calls_box 为共享累加器:失败抛出时也能如实汇报实际调用次数与用量
    (失败调用的用量不可知,记 None → 整次未知)。
    """
    vectors: dict[tuple, list[float]] = {}
    batch_size = max(1, settings.embedding_batch_size)
    seen_dims: int | None = None
    for start in range(0, len(drafts), batch_size):
        batch = drafts[start : start + batch_size]
        texts = [d.text for d in batch]
        calls_box[0] += 1
        try:
            result = adapter.embed(texts)
        except EmbeddingError as exc:
            usages.append(None)  # 失败调用的用量不可知:任一次缺失则整次未知
            # 429 限流直接失败:上游已要求放慢,同步立即重发同一批次只会再次增加请求。
            if exc.retryable and exc.code != "rate_limited":
                time.sleep(RETRY_BACKOFF_SECONDS)  # 有退避的有限重试一次
                calls_box[0] += 1
                result = adapter.embed(texts)  # 仍失败则向上抛
            else:
                raise
        usages.append(result.usage)
        if len(result.vectors) != len(batch):
            raise EmbeddingError(
                "bad_response",
                f"embedding 返回条数与输入不一致(输入 {len(batch)},返回 {len(result.vectors)})",
            )
        for d, vec in zip(batch, result.vectors):
            if seen_dims is None:
                seen_dims = len(vec)
            elif len(vec) != seen_dims:
                raise EmbeddingError(
                    "bad_response", f"embedding 维度不一致({seen_dims} vs {len(vec)})"
                )
            vectors[(d.source_type, d.source_id, d.unit_key)] = vec
    return vectors


def run_build(
    db: Session,
    settings: Settings,
    adapter: EmbeddingAdapter | None = None,
    force: bool = False,
) -> BuildOutcome:
    """执行一次索引构建/重建(新代次),返回构建结果与状态语义。

    adapter 可注入(测试用合成适配器);缺省按配置构造真实适配器。
    force=True(强制重建):绕过同指纹同内容的向量复用,整代重新嵌入并校验维度
    (刷新同端点同模型的旧向量/损坏向量/服务维度变化);失败不破坏兼容旧代次。
    模型失败:半成品已清理、原因已落状态行(脱敏)。
    """
    if settings.embedding_mode == "disabled" or not settings.embedding_model:
        raise IndexBuildRejected(
            "embedding 未配置(EMBEDDING_MODE=disabled 或未配 EMBEDDING_MODEL);无法构建索引"
        )
    ensure_pgvector(db)
    if adapter is None:
        try:
            adapter = build_embedding_adapter(settings)
        except EmbeddingError as exc:
            raise IndexBuildRejected(exc.message) from exc

    claim = _claim_build(db, settings)
    if claim is None:
        raise IndexBuildConflict("索引构建正在进行中;请稍后重试,或等待过期执行者被接管")

    usages: list[dict | None] = []
    calls_box: list[int] = [0]
    reusable: dict[tuple, list[float]] = {}
    try:
        text_limit = max(1, settings.embedding_max_input_chars)
        drafts = collect_index_units(db, max_text_chars=text_limit)
        if not force:
            # 普通构建:同指纹同内容复用既有向量(不重复计费;内容变化必须重新嵌入)
            reusable_raw = _reusable_vectors(db, claim.fingerprint)
            for d in drafts:
                key = (d.source_type, d.source_id, d.unit_key)
                stored = reusable_raw.get(key)
                if stored is not None and stored[0] == d.content_hash:
                    reusable[key] = stored[1]
        # force=True:不复用旧向量,全部重新嵌入(强制重建语义)
        to_embed = [
            d
            for d in drafts
            if (d.source_type, d.source_id, d.unit_key) not in reusable
        ]
        embedded = _embed_batched(adapter, to_embed, settings, usages, calls_box)
        _touch_build(db, claim)

        # 落库前逐条复核来源(构建期间删除/状态变化/内容变化不得落为有效索引)
        skipped: list[str] = []
        rows: list[tuple[IndexUnitDraft, list[float]]] = []
        seen_dims = 0
        for d in drafts:
            key = (d.source_type, d.source_id, d.unit_key)
            vec = embedded.get(key) or reusable.get(key)
            if vec is None:
                skipped.append(f"{d.source_type}:{d.source_id} 未取得向量,未纳入")
                continue
            if not is_unit_current(db, d, max_text_chars=text_limit):
                skipped.append(f"{d.source_type}:{d.source_id} 来源在构建期间变化/移除,未纳入")
                continue
            if seen_dims == 0:
                seen_dims = len(vec)
            elif len(vec) != seen_dims:
                raise EmbeddingError(
                    "bad_response", f"embedding 维度不一致({seen_dims} vs {len(vec)})"
                )
            rows.append((d, vec))

        for start in range(0, len(rows), 200):
            db.add_all(
                EmbeddingIndexUnit(
                    generation=claim.generation,
                    source_type=d.source_type,
                    source_id=d.source_id,
                    source_version=d.source_version,
                    unit_key=d.unit_key,
                    content_hash=d.content_hash,
                    text_excerpt=d.text[:300],
                    config_fingerprint=claim.fingerprint,
                    embedding_dims=len(vec),
                    embedding=vec,
                )
                for d, vec in rows[start : start + 200]
            )
            db.commit()

        # 构建期间配置切换:旧任务不得提交为当前有效结果(指纹与领取时不一致即拒绝),
        # 同时结束本执行者租约(复审 P2-3:不得遗留 building 阻塞新配置立即恢复)
        if _fingerprint(settings) != claim.fingerprint:
            _finalize_failure(
                db,
                claim,
                "config_switch",
                "构建期间配置已切换,本次结果未提交;请以当前配置重新发起构建",
                current_fingerprint=_fingerprint(settings),
            )
            raise IndexBuildConflict("构建结果未提交:配置在构建期间已切换;请以当前配置重新发起构建")

        ok = _finalize_success(db, claim, unit_count=len(rows), dims=seen_dims)
        if not ok:
            # 配置切换/被接管:本结果不得成为当前有效索引;清理本代次后明确冲突
            db.execute(
                delete(EmbeddingIndexUnit).where(
                    EmbeddingIndexUnit.generation == claim.generation
                )
            )
            db.commit()
            raise IndexBuildConflict("构建结果未提交:任务已被接管或配置已切换;可重新发起构建")

        partial = bool(skipped)
        if partial:
            state = db.get(EmbeddingIndexState, 1)
            if state is not None:
                state.error_code = "partial"
                state.error_message = (
                    f"有 {len(skipped)} 个来源在构建期间变化/移除,未纳入本次索引;可重新构建补齐"
                )
                db.commit()
        return BuildOutcome(
            result="partial" if partial else "success",
            built_units=len(rows),
            reused_units=len(reusable),
            embedded_calls=calls_box[0],
            usage=aggregate_usage(usages),
            skipped=skipped,
        )
    except EmbeddingError as exc:
        # 模型/响应失败:半成品清理 + 原因落状态行(脱敏);如实返回调用/用量,不虚构成功
        _finalize_failure(
            db, claim, exc.code, f"embedding 构建失败: {exc.message}",
            current_fingerprint=_fingerprint(settings),
        )
        return BuildOutcome(
            result="failed",
            built_units=0,
            reused_units=len(reusable),
            embedded_calls=calls_box[0],
            usage=aggregate_usage(usages),
            error_code=exc.code,
            error_message=exc.message,
        )
    except IndexBuildConflict:
        raise
    except Exception:
        # 非模型异常同样不得激活半成品;清理后原样抛出(不吞掉程序缺陷)
        _finalize_failure(
            db, claim, "internal", "构建执行异常",
            current_fingerprint=_fingerprint(settings),
        )
        raise
