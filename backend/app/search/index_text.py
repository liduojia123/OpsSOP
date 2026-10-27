"""embedding 索引文本组织。

索引单元与纳入范围:
- 单元 = 一个来源文档(已通过案例 / 当前有效已发布 SOP),unit_key="doc";
- 文本 = 来源知识字段的结构化拼接;先脱敏后截断(MAX_INDEX_TEXT_CHARS);
- 不复制整份故障历史(不纳入追加记录),不写入凭据或无关内部材料;
- 待审核/退回案例、草稿/历史/废弃 SOP、未解决故障一律不纳入。

INDEX_TEXT_VERSION 为文本处理版本:文本组织规则变化时递增,
使旧向量进入「需要重建」而不是与新文本混用。
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.sanitize import sanitize_text
from app.models import Case, CaseStatus, Sop, SopStatus

# 文本处理版本:影响向量兼容性的文本组织变化必须递增
INDEX_TEXT_VERSION = "t1"

# 单条索引文本长度上限(脱敏后截断)
MAX_INDEX_TEXT_CHARS = 4000

UNIT_KEY_DOC = "doc"


def config_fingerprint(embedding_mode: str, embedding_base_url: str, embedding_model: str) -> str:
    """embedding 配置指纹:绑定模式/端点/模型/文本版本;不含明文密钥。

    相同维度的不同模型指纹不同 → 不混用;任何成分变化都会使旧代次进入「需要重建」。
    """
    raw = "|".join(
        [embedding_mode, embedding_base_url, embedding_model, INDEX_TEXT_VERSION]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _clip_text(text: str, limit: int = MAX_INDEX_TEXT_CHARS) -> str:
    """先脱敏后截断(截断不得绕过打码);limit 与 embedding 输入长度上限联动。"""
    return sanitize_text(text)[: max(1, min(limit, MAX_INDEX_TEXT_CHARS))]


def case_index_text(
    title: str,
    symptom: str,
    environment: str,
    service: str,
    component_version: str,
    confirmed_root_cause: str,
    handling_steps: str,
    recovery_verification: str,
    limit: int = MAX_INDEX_TEXT_CHARS,
) -> str:
    """案例索引文本:人工确认内容为主的结构化拼接(不含故障历史记录)。"""
    parts = [
        f"标题: {title}" if title else "",
        f"环境: {environment}" if environment else "",
        f"服务: {service}" if service else "",
        f"组件版本: {component_version}" if component_version else "",
        f"现象: {symptom}" if symptom else "",
        f"人工确认根因: {confirmed_root_cause}" if confirmed_root_cause else "",
        f"处理步骤: {handling_steps}" if handling_steps else "",
        f"恢复验证: {recovery_verification}" if recovery_verification else "",
    ]
    return _clip_text("\n".join(p for p in parts if p), limit)


def sop_index_text(
    title: str,
    applicable_env: str,
    symptom: str,
    preconditions: str,
    check_steps: str,
    handling_steps: str,
    recovery_verification: str,
    rollback_steps: str,
    notes: str,
    limit: int = MAX_INDEX_TEXT_CHARS,
) -> str:
    """SOP 索引文本:当前有效已发布版本的内容字段结构化拼接。"""
    parts = [
        f"标题: {title}" if title else "",
        f"适用环境及版本: {applicable_env}" if applicable_env else "",
        f"现象: {symptom}" if symptom else "",
        f"前置条件: {preconditions}" if preconditions else "",
        f"排查步骤: {check_steps}" if check_steps else "",
        f"处理方法: {handling_steps}" if handling_steps else "",
        f"恢复验证: {recovery_verification}" if recovery_verification else "",
        f"回滚方式: {rollback_steps}" if rollback_steps else "",
        f"注意事项: {notes}" if notes else "",
    ]
    return _clip_text("\n".join(p for p in parts if p), limit)


class IndexUnitDraft:
    """一个待嵌入的索引单元(来源定位 + 文本 + 指纹)。"""

    __slots__ = ("source_type", "source_id", "source_version", "unit_key", "text", "content_hash")

    def __init__(
        self,
        source_type: str,
        source_id: int,
        source_version: int | None,
        text: str,
    ):
        self.source_type = source_type
        self.source_id = source_id
        self.source_version = source_version
        self.unit_key = UNIT_KEY_DOC
        self.text = text
        self.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_index_units(db: Session, max_text_chars: int = MAX_INDEX_TEXT_CHARS) -> list[IndexUnitDraft]:
    """枚举当前应纳入索引的来源(列级读取):已通过案例 + 当前有效已发布 SOP。"""
    units: list[IndexUnitDraft] = []
    case_rows = db.execute(
        select(
            Case.id,
            Case.title,
            Case.symptom,
            Case.environment,
            Case.service,
            Case.component_version,
            Case.confirmed_root_cause,
            Case.handling_steps,
            Case.recovery_verification,
        ).where(Case.status == CaseStatus.approved)
    ).all()
    for row in case_rows:
        text = case_index_text(
            title=row.title or "",
            symptom=row.symptom or "",
            environment=row.environment or "",
            service=row.service or "",
            component_version=row.component_version or "",
            confirmed_root_cause=row.confirmed_root_cause or "",
            handling_steps=row.handling_steps or "",
            recovery_verification=row.recovery_verification or "",
            limit=max_text_chars,
        )
        if text.strip():
            units.append(IndexUnitDraft("case", row.id, None, text))

    sop_rows = db.execute(
        select(
            Sop.id,
            Sop.version,
            Sop.title,
            Sop.applicable_env,
            Sop.symptom,
            Sop.preconditions,
            Sop.check_steps,
            Sop.handling_steps,
            Sop.recovery_verification,
            Sop.rollback_steps,
            Sop.notes,
        ).where(Sop.status == SopStatus.published)
    ).all()
    for row in sop_rows:
        text = sop_index_text(
            title=row.title or "",
            applicable_env=row.applicable_env or "",
            symptom=row.symptom or "",
            preconditions=row.preconditions or "",
            check_steps=row.check_steps or "",
            handling_steps=row.handling_steps or "",
            recovery_verification=row.recovery_verification or "",
            rollback_steps=row.rollback_steps or "",
            notes=row.notes or "",
            limit=max_text_chars,
        )
        if text.strip():
            units.append(IndexUnitDraft("sop", row.id, row.version, text))

    units.sort(key=lambda u: (u.source_type, u.source_id))
    return units


def _unit_text_now(
    db: Session, source_type: str, source_id: int, max_text_chars: int
) -> str | None:
    """按库内当前内容重建单个来源的索引文本;来源消失/不合规返回 None。"""
    if source_type == "case":
        row = db.execute(
            select(
                Case.status,
                Case.title,
                Case.symptom,
                Case.environment,
                Case.service,
                Case.component_version,
                Case.confirmed_root_cause,
                Case.handling_steps,
                Case.recovery_verification,
            ).where(Case.id == source_id)
        ).first()
        if row is None or row[0] != CaseStatus.approved:
            return None
        return case_index_text(
            title=row.title or "",
            symptom=row.symptom or "",
            environment=row.environment or "",
            service=row.service or "",
            component_version=row.component_version or "",
            confirmed_root_cause=row.confirmed_root_cause or "",
            handling_steps=row.handling_steps or "",
            recovery_verification=row.recovery_verification or "",
            limit=max_text_chars,
        )
    if source_type == "sop":
        row = db.execute(
            select(
                Sop.status,
                Sop.version,
                Sop.title,
                Sop.applicable_env,
                Sop.symptom,
                Sop.preconditions,
                Sop.check_steps,
                Sop.handling_steps,
                Sop.recovery_verification,
                Sop.rollback_steps,
                Sop.notes,
            ).where(Sop.id == source_id)
        ).first()
        if row is None or row[0] != SopStatus.published:
            return None
        return sop_index_text(
            title=row.title or "",
            applicable_env=row.applicable_env or "",
            symptom=row.symptom or "",
            preconditions=row.preconditions or "",
            check_steps=row.check_steps or "",
            handling_steps=row.handling_steps or "",
            recovery_verification=row.recovery_verification or "",
            rollback_steps=row.rollback_steps or "",
            notes=row.notes or "",
            limit=max_text_chars,
        )
    return None


def is_unit_current(
    db: Session, draft: IndexUnitDraft, max_text_chars: int = MAX_INDEX_TEXT_CHARS
) -> bool:
    """落库前复核来源:状态仍合规且内容指纹未变(构建期间的删除/变化不得落为有效索引)。"""
    text_now = _unit_text_now(db, draft.source_type, draft.source_id, max_text_chars)
    if text_now is None:
        return False
    return hashlib.sha256(text_now.encode("utf-8")).hexdigest() == draft.content_hash
