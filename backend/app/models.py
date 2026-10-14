"""ORM 模型:原始证据、人工确认、案例、SOP 分表存储。"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    operator = "operator"  # 运维人员
    reviewer = "reviewer"  # 审核人员
    admin = "admin"  # 管理员


class FaultStatus(str, enum.Enum):
    draft = "draft"  # 草稿
    open = "open"  # 处理中
    resolved = "resolved"  # 已解决(已人工确认根因)


class RecordKind(str, enum.Enum):
    check_result = "check_result"  # 检查结果
    handling_note = "handling_note"  # 处理记录


class CaseStatus(str, enum.Enum):
    pending_review = "pending_review"  # 待审核
    approved = "approved"  # 已通过(正式诊断依据)
    returned = "returned"  # 退回


class SopStatus(str, enum.Enum):
    draft = "draft"  # 草稿
    submitted = "submitted"  # 待审核
    returned = "returned"  # 退回
    published = "published"  # 已发布(当前有效版本)
    superseded = "superseded"  # 历史版本(曾发布,被新版本取代)
    deprecated = "deprecated"  # 已废弃


class FeedbackKind(str, enum.Enum):
    effective = "effective"  # 有效
    ineffective = "ineffective"  # 无效
    suspected_outdated = "suspected_outdated"  # 疑似过期


def _enum_column(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum_column(Role, "role"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Fault(Base):
    __tablename__ = "faults"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    environment: Mapped[str] = mapped_column(String(64), default="")
    service: Mapped[str] = mapped_column(String(128), default="")
    component_version: Mapped[str] = mapped_column(String(128), default="")
    recent_changes: Mapped[str] = mapped_column(Text, default="")
    error_logs: Mapped[str] = mapped_column(Text, default="")
    attempted_actions: Mapped[str] = mapped_column(Text, default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[FaultStatus] = mapped_column(
        _enum_column(FaultStatus, "fault_status"), default=FaultStatus.draft
    )
    # 人工确认解决时填写;与 AI 推测严格分开
    actual_root_cause: Mapped[str] = mapped_column(Text, default="")
    handling_steps: Mapped[str] = mapped_column(Text, default="")
    recovery_verification: Mapped[str] = mapped_column(Text, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    category: Mapped[Category | None] = relationship()
    creator: Mapped[User] = relationship()
    records: Mapped[list["FaultRecord"]] = relationship(
        back_populates="fault", cascade="all, delete-orphan", order_by="FaultRecord.id"
    )


class FaultRecord(Base):
    """追加的检查结果与处理记录(用户原始证据,追加式)。"""

    __tablename__ = "fault_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    fault_id: Mapped[int] = mapped_column(ForeignKey("faults.id"), index=True)
    kind: Mapped[RecordKind] = mapped_column(_enum_column(RecordKind, "record_kind"))
    content: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    fault: Mapped[Fault] = relationship(back_populates="records")
    creator: Mapped[User] = relationship()


class Case(Base):
    """已解决案例:人工确认的根因/处理/恢复,与故障原始证据分表。"""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    fault_id: Mapped[int] = mapped_column(ForeignKey("faults.id"), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    symptom: Mapped[str] = mapped_column(Text, default="")
    environment: Mapped[str] = mapped_column(String(64), default="")
    service: Mapped[str] = mapped_column(String(128), default="")
    component_version: Mapped[str] = mapped_column(String(128), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    confirmed_root_cause: Mapped[str] = mapped_column(Text, default="")
    handling_steps: Mapped[str] = mapped_column(Text, default="")
    recovery_verification: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[CaseStatus] = mapped_column(
        _enum_column(CaseStatus, "case_status"), default=CaseStatus.pending_review
    )
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    fault: Mapped[Fault] = relationship()
    category: Mapped[Category | None] = relationship()
    creator: Mapped[User] = relationship(foreign_keys=[created_by])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewed_by])


class Sop(Base):
    """SOP 版本:每行一个版本,doc_id 标识同一份文档。"""

    __tablename__ = "sops"
    __table_args__ = (UniqueConstraint("doc_id", "version", name="uq_sop_doc_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    applicable_env: Mapped[str] = mapped_column(Text, default="")  # 适用环境及版本
    symptom: Mapped[str] = mapped_column(Text, default="")  # 现象
    preconditions: Mapped[str] = mapped_column(Text, default="")  # 前置条件
    check_steps: Mapped[str] = mapped_column(Text, default="")  # 排查步骤
    handling_steps: Mapped[str] = mapped_column(Text, default="")  # 处理方法
    recovery_verification: Mapped[str] = mapped_column(Text, default="")  # 恢复验证
    rollback_steps: Mapped[str] = mapped_column(Text, default="")  # 回滚方式
    notes: Mapped[str] = mapped_column(Text, default="")  # 注意事项
    source_case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"))  # 来源案例
    maintainer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # 维护人
    status: Mapped[SopStatus] = mapped_column(
        _enum_column(SopStatus, "sop_status"), default=SopStatus.draft
    )
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    category: Mapped[Category | None] = relationship()
    source_case: Mapped[Case | None] = relationship()
    maintainer: Mapped[User | None] = relationship(foreign_keys=[maintainer_id])
    creator: Mapped[User] = relationship(foreign_keys=[created_by])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewed_by])


class SopFeedback(Base):
    __tablename__ = "sop_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    sop_id: Mapped[int] = mapped_column(ForeignKey("sops.id"), index=True)
    kind: Mapped[FeedbackKind] = mapped_column(_enum_column(FeedbackKind, "feedback_kind"))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sop: Mapped[Sop] = relationship()
    creator: Mapped[User] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    actor: Mapped[User | None] = relationship()


class AiAnalysisStatus(str, enum.Enum):
    success = "success"  # 分析成功且引用完整
    incomplete = "incomplete"  # 成功但部分引用无效被移除/格式修复后不完整
    failed = "failed"  # 模型调用失败或输出无法解析(不回退预设答案)


class AiAnalysis(Base):
    """AI 分析历史:与原始输入(faults/records)、人工确认(faults 确认字段/cases)分表。

    每条记录绑定输入快照哈希、模型配置版本与知识版本;新输入或配置变化后旧报告过期。
    不存储密钥;usage 仅在模型接口实际返回时记录。
    """

    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    fault_id: Mapped[int] = mapped_column(ForeignKey("faults.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # trigger_type 而非 trigger:避免与 SQL 关键字冲突
    trigger_type: Mapped[str] = mapped_column(String(16), default="manual")  # auto | manual
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # 分析时的输入快照
    model_config_version: Mapped[str] = mapped_column(String(16), default="")
    knowledge_version: Mapped[str] = mapped_column(String(16), default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[AiAnalysisStatus] = mapped_column(
        _enum_column(AiAnalysisStatus, "ai_analysis_status"), default=AiAnalysisStatus.failed
    )
    report: Mapped[dict | None] = mapped_column(JSONB)  # 校验后的报告(AnalysisReport)
    incomplete_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    error_code: Mapped[str] = mapped_column(String(32), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict | None] = mapped_column(JSONB)  # 模型返回的 usage;缺失为 null
    call_count: Mapped[int] = mapped_column(Integer, default=0)  # 实际模型调用次数
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    fault: Mapped[Fault] = relationship()
    creator: Mapped[User] = relationship()
