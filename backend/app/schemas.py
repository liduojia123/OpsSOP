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
    username: str
    password: str


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
    display_name: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)

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
    """AI 模式状态。第一阶段仅暴露配置状态,不调用模型。"""

    ai_mode: str
    embedding_mode: str
    local_only: bool
    message: str
