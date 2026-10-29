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

from pgvector.sqlalchemy import Vector

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


class UserAiKey(Base):
    """用户自填的模型厂商配置(每个用户每个厂商一条)。

    api_key_encrypted 为服务端加密后的密文;明文 Key 不落库、不出接口。
    base_url 允许用户覆盖厂商默认端点(如国内/国际、Token Plan 专属端点)。
    """

    __tablename__ = "user_ai_keys"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_ai_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # deepseek | minimax | mimo
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    base_url: Mapped[str] = mapped_column(String(300), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship()


class RevokedSession(Base):
    __tablename__ = "revoked_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


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


class SopGenStatus(str, enum.Enum):
    """AI 生成 SOP 草稿的运行状态(与故障分析历史分开,见 SopAiGenerationRun)。"""

    running = "running"  # 生成中(进程崩溃遗留记录超时后允许同 token 接管重试)
    success = "success"  # 生成成功并创建 draft
    failed = "failed"  # 模型失败/格式失败/来源变化;不创建草稿,只留安全元信息


class SopAiGenerationRun(Base):
    """AI 生成 SOP 草稿记录。

    与故障分析历史(ai_analyses)分开,不伪装成故障分析历史:
    记录来源案例与输入快照标识、模型配置版本、真/演示模式、生成状态、
    调用次数/延迟/实际 usage、待补信息、失败类型与安全原因。
    request_token 做幂等:同一次请求重复提交/重试不重复创建草稿、不重复计费。
    attempt 为执行领取计数:每次原子领取(首建=1,重试/过期接管 +1)递增,
    结果提交按 attempt 条件更新,防止被接管后的旧执行者覆盖新结果;
    记录中的输入/配置字段标识最近一次领取(执行尝试)的实际输入与配置。
    """

    __tablename__ = "sop_ai_generation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    request_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)  # 执行领取计数(结果提交条件)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # 最小标识(证据 ID 清单等)
    model_config_version: Mapped[str] = mapped_column(String(16), default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=SopGenStatus.running.value)
    sop_id: Mapped[int | None] = mapped_column(ForeignKey("sops.id"))  # 成功时创建的 draft
    missing_information: Mapped[list] = mapped_column(JSONB, default=list)
    incomplete_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    field_sources: Mapped[list] = mapped_column(JSONB, default=list)  # 校验后的字段来源映射
    error_code: Mapped[str] = mapped_column(String(32), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")  # 已脱敏的安全原因
    usage: Mapped[dict | None] = mapped_column(JSONB)  # 模型返回的 usage;缺失为 null
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[Case] = relationship()
    creator: Mapped[User] = relationship(foreign_keys=[created_by])
    sop: Mapped[Sop | None] = relationship(foreign_keys=[sop_id])


class EmbeddingIndexStatus(str, enum.Enum):
    """embedding 索引状态。

    未配置/待构建/构建中/可用/需要重建/失败;
    实际展示以「配置 + 状态行」计算的有效状态为准(app.search.embedding_index)。
    """

    unconfigured = "unconfigured"  # 未配置(embedding disabled 或未配模型)
    pending = "pending"  # 待构建(已配置,尚无成功代次)
    building = "building"  # 构建中
    ready = "ready"  # 可用(活动代次完整且与当前配置指纹一致)
    needs_rebuild = "needs_rebuild"  # 需要重建(活动代次与当前配置不兼容,不混用)
    failed = "failed"  # 失败(无可用代次;原因见 error 字段)


class EmbeddingIndexState(Base):
    """embedding 索引元数据(单行,id=1)。

    向量候选只认 active_generation;不同模型/维度/文本版本不混用:
    单元行携带 config_fingerprint 与维度,活动代次指纹与当前配置不一致时标 needs_rebuild。
    build_attempt 为构建领取计数(结果条件提交),防止被接管后的旧任务覆盖新代次。
    """

    __tablename__ = "embedding_index_state"

    id: Mapped[int] = mapped_column(primary_key=True)  # 恒为 1
    status: Mapped[str] = mapped_column(String(16), default=EmbeddingIndexStatus.pending.value)
    # 活动(可用)代次的描述
    active_generation: Mapped[int] = mapped_column(Integer, default=0)
    active_fingerprint: Mapped[str] = mapped_column(String(16), default="")
    active_model: Mapped[str] = mapped_column(String(128), default="")
    active_dims: Mapped[int] = mapped_column(Integer, default=0)
    active_text_version: Mapped[str] = mapped_column(String(16), default="")
    unit_count: Mapped[int] = mapped_column(Integer, default=0)  # 活动代次实际单元数
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 构建(领取)中的描述;构建完成后并入活动描述或被丢弃
    building_generation: Mapped[int] = mapped_column(Integer, default=0)
    build_attempt: Mapped[int] = mapped_column(Integer, default=0)
    build_fingerprint: Mapped[str] = mapped_column(String(16), default="")
    error_code: Mapped[str] = mapped_column(String(32), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")  # 已脱敏的安全原因
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DemoSeedRecord(Base):
    """显式演示内容的稳定标识映射。

    演示内容通过专用 CLI 显式写入,不在启动/登录/页面打开时自动创建。
    (scenario_key, entity_role) 唯一定位一个演示实体,避免依赖可编辑标题;
    重复执行时据此跳过已有实体,不覆盖人工修改。
    """

    __tablename__ = "demo_seed_records"
    __table_args__ = (
        UniqueConstraint("scenario_key", "entity_role", name="uq_demo_seed_scenario_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_key: Mapped[str] = mapped_column(String(64), index=True)
    entity_role: Mapped[str] = mapped_column(String(32))  # fault | case | sop | sop_v1 | sop_v2
    entity_type: Mapped[str] = mapped_column(String(16))  # fault | case | sop
    entity_id: Mapped[int] = mapped_column(Integer)
    seed_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EmbeddingIndexUnit(Base):
    """embedding 索引单元(一个来源文档一单元;向量为 pgvector 列)。

    只纳入已通过案例与当前有效已发布 SOP;每行可定位来源类型/ID/SOP 版本与内容指纹。
    generation 为构建代次:活动代次之外的行不是有效候选(旧代次保留或清理由构建流程决定)。
    """

    __tablename__ = "embedding_index_units"
    __table_args__ = (
        UniqueConstraint(
            "generation", "source_type", "source_id", "unit_key", name="uq_emb_unit_gen_source"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, index=True)
    source_type: Mapped[str] = mapped_column(String(8))  # case | sop
    source_id: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[int | None] = mapped_column(Integer)  # SOP 版本;案例为 null
    unit_key: Mapped[str] = mapped_column(String(32), default="doc")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # 文本指纹
    text_excerpt: Mapped[str] = mapped_column(Text, default="")  # 脱敏摘要(定位用)
    config_fingerprint: Mapped[str] = mapped_column(String(16))
    embedding_dims: Mapped[int] = mapped_column(Integer, default=0)
    # pgvector 向量列:维度不限定(由模型决定并记录在 embedding_dims),不混用不同维度
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
