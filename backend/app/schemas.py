"""API 输入/输出模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import CaseStatus, FaultStatus, FeedbackKind, RecordKind, Role, SopStatus
from app.security import MAX_PASSWORD_BYTES


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _check_password_bytes(v: str) -> str:
    """bcrypt 只对前 72 字节有效;按字节拒绝超长密码,避免静默截断。"""
    if len(v.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码过长:最多 {MAX_PASSWORD_BYTES} 字节(UTF-8,中文按 3 字节计)")
    return v


def _check_not_blank(v: str) -> str:
    """人工确认字段拒绝纯空白。"""
    if not v.strip():
        raise ValueError("不能为空或纯空白")
    return v.strip()


# ===== 认证与用户 =====

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)


class UserOut(ORMModel):
    id: int
    username: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime


class UserOptionOut(ORMModel):
    """下拉选择用的最小用户信息(如 SOP 维护人),不含口令等敏感字段。"""

    id: int
    username: str
    display_name: str
    role: Role


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    role: Role

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("display_name")
    @classmethod
    def _display_name_not_blank(cls, v: str | None) -> str | None:
        return _check_not_blank(v) if v is not None else v

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str | None) -> str | None:
        return _check_password_bytes(v) if v is not None else v


# ===== 分类 =====

class CategoryOut(ORMModel):
    id: int
    name: str
    description: str
    is_system: bool
    sort_order: int


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    sort_order: int | None = None


# ===== 故障 =====

class FaultBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    occurred_at: datetime | None = None
    environment: str = ""
    service: str = ""
    component_version: str = ""
    recent_changes: str = ""
    error_logs: str = ""
    attempted_actions: str = ""
    category_id: int | None = None
    tags: list[str] = Field(default_factory=list)


class FaultCreate(FaultBase):
    # 创建只允许 draft/open;resolved 只能由 /resolve 人工确认产生
    status: FaultStatus = FaultStatus.draft

    @field_validator("status")
    @classmethod
    def _no_resolved_on_create(cls, v: FaultStatus) -> FaultStatus:
        if v == FaultStatus.resolved:
            raise ValueError("创建故障不能直接标记为已解决;请走人工确认解决(/resolve)流程")
        return v


class FaultUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    occurred_at: datetime | None = None
    environment: str | None = None
    service: str | None = None
    component_version: str | None = None
    recent_changes: str | None = None
    error_logs: str | None = None
    attempted_actions: str | None = None
    category_id: int | None = None
    tags: list[str] | None = None
    status: FaultStatus | None = None


class FaultRecordOut(ORMModel):
    id: int
    fault_id: int
    kind: RecordKind
    content: str
    created_by: int
    created_at: datetime


class FaultRecordCreate(BaseModel):
    kind: RecordKind
    content: str = Field(min_length=1)


class FaultOut(ORMModel):
    id: int
    title: str
    description: str
    occurred_at: datetime | None
    environment: str
    service: str
    component_version: str
    recent_changes: str
    error_logs: str
    attempted_actions: str
    category_id: int | None
    tags: list
    status: FaultStatus
    actual_root_cause: str
    handling_steps: str
    recovery_verification: str
    resolved_at: datetime | None
    created_by: int
    created_at: datetime
    updated_at: datetime


class FaultDetailOut(FaultOut):
    records: list[FaultRecordOut] = Field(default_factory=list)


class ResolveFaultRequest(BaseModel):
    """人工确认解决:实际根因、处理步骤、恢复验证均须人工填写,拒绝纯空白。"""

    actual_root_cause: str = Field(min_length=1)
    handling_steps: str = Field(min_length=1)
    recovery_verification: str = Field(min_length=1)

    @field_validator("actual_root_cause", "handling_steps", "recovery_verification")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        return _check_not_blank(v)


# ===== 案例 =====

class CaseOut(ORMModel):
    id: int
    fault_id: int
    title: str
    symptom: str
    environment: str
    service: str
    component_version: str
    category_id: int | None
    tags: list
    confirmed_root_cause: str
    handling_steps: str
    recovery_verification: str
    status: CaseStatus
    review_comment: str
    reviewed_by: int | None
    reviewed_at: datetime | None
    created_by: int
    created_at: datetime
    updated_at: datetime


class CaseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    symptom: str | None = None
    category_id: int | None = None
    tags: list[str] | None = None
    confirmed_root_cause: str | None = None
    handling_steps: str | None = None
    recovery_verification: str | None = None

    @field_validator("confirmed_root_cause", "handling_steps", "recovery_verification")
    @classmethod
    def _not_blank_if_set(cls, v: str | None) -> str | None:
        # 显式传入的确认字段不允许清空为纯空白(None 表示不修改)
        return _check_not_blank(v) if v is not None else v


class ReviewRequest(BaseModel):
    comment: str = ""


# ===== SOP =====

class SopBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    category_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    applicable_env: str = ""
    symptom: str = ""
    preconditions: str = ""
    check_steps: str = ""
    handling_steps: str = ""
    recovery_verification: str = ""
    rollback_steps: str = ""
    notes: str = ""
    source_case_id: int | None = None
    maintainer_id: int | None = None


class SopCreate(SopBase):
    pass


class SopUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    category_id: int | None = None
    tags: list[str] | None = None
    applicable_env: str | None = None
    symptom: str | None = None
    preconditions: str | None = None
    check_steps: str | None = None
    handling_steps: str | None = None
    recovery_verification: str | None = None
    rollback_steps: str | None = None
    notes: str | None = None
    source_case_id: int | None = None
    maintainer_id: int | None = None


class SopOut(ORMModel):
    id: int
    doc_id: str
    version: int
    title: str
    category_id: int | None
    tags: list
    applicable_env: str
    symptom: str
    preconditions: str
    check_steps: str
    handling_steps: str
    recovery_verification: str
    rollback_steps: str
    notes: str
    source_case_id: int | None
    maintainer_id: int | None
    status: SopStatus
    review_comment: str
    reviewed_by: int | None
    reviewed_at: datetime | None
    created_by: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


class SopFeedbackCreate(BaseModel):
    kind: FeedbackKind
    comment: str = ""


class SopFeedbackOut(ORMModel):
    id: int
    sop_id: int
    kind: FeedbackKind
    comment: str
    created_by: int
    created_at: datetime


# ===== 工作台 =====

class CategoryStat(BaseModel):
    category_id: int | None
    category_name: str
    count: int


class WorkbenchOut(BaseModel):
    my_open_faults: list[FaultOut]
    pending_case_reviews: list[CaseOut]
    pending_sop_reviews: list[SopOut]
    recent_sops: list[SopOut]
    # 有效 SOP 总数(recent_sops 最多取 8 条,列表长度不能当总数用)
    published_sop_count: int = 0
    category_stats: list[CategoryStat]
    my_returned_cases: list[CaseOut]
    my_returned_sops: list[SopOut]


# ===== 审计 =====

class BackupOut(BaseModel):
    name: str
    created_at: str | None = None
    size_bytes: int
    sha256: str = ""
    trigger: str = ""
    retention_days: int | None = None


class BackupRunOut(BackupOut):
    pruned: list[str] = []


class BackupConfigOut(BaseModel):
    enabled: bool
    directory: str
    hour: int
    retention_days: int
    audit_retention_days: int
    next_run: str
    # 最近备份成功/失败状态:备份失败时 /health 仍为 ok,管理员与监控从这里发现异常
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_reason: str | None = None
    backup_age_hours: float | None = None


class AuditEventOut(ORMModel):
    id: int
    actor_id: int | None
    action: str
    target_type: str
    target_id: str
    detail: dict
    created_at: datetime


# ===== 系统状态 =====

class AiStatusOut(BaseModel):
    """AI 模式状态。仅暴露配置状态与配置版本,不调用模型、不返回密钥。"""

    ai_mode: str
    embedding_mode: str
    local_only: bool
    message: str
    # 模型配置版本:前端绑定分析请求/响应,配置变化后旧报告标记过期
    model_config_version: str = ""


# ===== AI 分析=====

class AnalysisInputPatch(BaseModel):
    """左栏未保存的表单编辑:分析时覆盖已保存故障字段,不回写数据库。"""

    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=20000)
    environment: str | None = Field(default=None, max_length=64)
    service: str | None = Field(default=None, max_length=128)
    component_version: str | None = Field(default=None, max_length=128)
    recent_changes: str | None = Field(default=None, max_length=20000)
    error_logs: str | None = Field(default=None, max_length=50000)
    attempted_actions: str | None = Field(default=None, max_length=20000)
    tags: list[str] | None = None
    category_id: int | None = None


class AnalyzeRequest(BaseModel):
    """触发一次分析。input 为左栏当前表单(含未保存编辑);缺省字段沿用已保存故障。"""

    trigger_type: str = Field(default="manual", pattern="^(auto|manual)$")
    use_cache: bool = True
    input: AnalysisInputPatch | None = None


class AiAnalysisOut(ORMModel):
    """一条分析记录(报告或失败状态)。usage 缺失时为 null(前端显示未知)。"""

    id: int
    fault_id: int
    created_by: int
    trigger_type: str
    input_hash: str
    model_config_version: str
    knowledge_version: str
    is_demo: bool
    status: str
    report: dict | None
    incomplete_reasons: list
    error_code: str
    error_message: str
    usage: dict | None
    call_count: int
    latency_ms: int
    created_at: datetime


class AnalyzeResponse(BaseModel):
    """分析响应:结果 + 过期判定信息(输入哈希/配置版本随响应下发,前端据此标记过期)。"""

    analysis: AiAnalysisOut
    reused: bool = False  # 命中缓存复用已有记录
    input_substance: bool = True  # False 表示空输入未触发模型


class SearchHitOut(BaseModel):
    """知识检索命中:来源类型/ID/版本/片段/定位链接。"""

    ref_id: str
    source_type: str
    source_id: int
    version: int | None
    title: str
    snippet: str
    link: str
    status: str
    score: float


class SearchResponse(BaseModel):
    """检索响应:结果 + 实际检索方式与降级信息(如实标注,不把关键词结果冒充语义检索)。"""

    query: str
    hits: list[SearchHitOut]
    knowledge_version: str
    retrieval_mode: str = "keyword"  # keyword | hybrid(实际执行的检索方式)
    degraded: bool = False  # True = 向量路径不可用/失败,已降级关键词
    degrade_reason: str = ""  # 安全的降级原因(不含密钥/堆栈)


# ===== AI 生成 SOP 草稿=====

class SopGenerateAiRequest(BaseModel):
    """AI 生成 SOP 草稿请求。

    request_token 为一次「点击生成」的幂等键:同一次请求重复提交/重试沿用同一 token
    (不重复创建草稿、不重复计费);用户主动再次生成使用新 token,可创建另一份新草稿。
    """

    request_token: str = Field(min_length=8, max_length=64)


class SopGenerationRunOut(ORMModel):
    """一次 AI 生成运行记录(与故障分析历史分开,不混用)。

    usage 缺失时为 null(前端显示「用量未知」);error_message 为脱敏后的安全原因。
    """

    id: int
    case_id: int
    created_by: int
    request_token: str
    attempt: int  # 执行领取计数(标识最近一次执行尝试)
    input_hash: str
    model_config_version: str
    is_demo: bool
    status: str  # running | success | failed
    sop_id: int | None
    missing_information: list
    incomplete_reasons: list
    field_sources: list  # 校验后的字段来源映射 [{target, evidence_ids}]
    error_code: str
    error_message: str
    usage: dict | None
    call_count: int
    latency_ms: int
    created_at: datetime
    updated_at: datetime


class SopGenerateAiResponse(BaseModel):
    """生成响应:运行记录 + 成功时的 draft。HTTP 201 不是成功判据,须检查 run.status。"""

    run: SopGenerationRunOut
    sop: SopOut | None = None
    reused: bool = False  # 幂等重复返回既有结果(未重复计费)


# ===== embedding 索引管理=====

class EmbeddingIndexStatusOut(BaseModel):
    """索引状态快照:实际数量/配置标识/更新时间/安全失败原因;不含密钥、不伪造进度。"""

    status: str  # unconfigured | pending | building | ready | needs_rebuild | failed
    embedding_mode: str
    embedding_model: str
    config_fingerprint: str  # 不含明文密钥
    text_version: str
    active_generation: int
    active_fingerprint: str
    active_model: str
    embedding_dims: int
    unit_count: int  # 活动代次实际单元数
    total_rows: int  # 含非活动代次的行数(排查用)
    building_generation: int
    build_attempt: int
    error_code: str
    error_message: str  # 已脱敏的安全原因
    built_at: datetime | None
    updated_at: datetime | None
    egress_notice: str = ""  # cloud 模式出网范围说明


class EmbeddingIndexBuildOut(BaseModel):
    """构建/重建结果:result=success|partial|failed;失败原因见 status 快照。"""

    result: str
    built_units: int
    reused_units: int  # 同指纹同内容复用的向量数(不重复计费)
    embedded_calls: int  # 实际 embedding 调用批次次数
    usage: dict | None  # 实际用量;缺失为 null(未知,不虚构)
    skipped: list[str]  # 构建期间变化/移除而未纳入的来源(部分成功必须明示覆盖范围)
    status: EmbeddingIndexStatusOut
