"""AI 生成 SOP 草稿的固定 Schema(第三阶段第一批,独立于故障分析报告 Schema)。

覆盖现有 SOP 内容字段:title / applicable_env / symptom / preconditions /
check_steps / handling_steps / recovery_verification / rollback_steps / notes,
外加明确的待补信息(missing_information)与可核对的来源标注(sources)。

校验原则:
- 结构由 Pydantic 严格校验;sources 的 evidence_id 由后端分配并二次校验
  (app.ai.sop_draft.verify_draft_content),无效来源移除并记录不完整原因;
- 材料缺失的字段必须标注「待补 / 待人工确认」,不得用看似完整的内容掩盖缺失;
- 猜测性回滚必须标注「待验证 / 待人工确认」,不得写成已验证方案;
- title/source_case_id/创建者/维护人/doc_id/version/status 等由后端决定,模型不输出。
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 与 app.models.Sop 内容字段一致(模型可整理的字段)
CONTENT_FIELDS = (
    "applicable_env",
    "symptom",
    "preconditions",
    "check_steps",
    "handling_steps",
    "recovery_verification",
    "rollback_steps",
    "notes",
)

# 关键内容字段(有直接来源材料):生成文本若无有效来源映射,必须明确「待人工确认」,
# 不把全局证据清单当作字段来源映射(2026-10-15 复审 P2-4)
KEY_SOURCE_FIELDS = ("applicable_env", "symptom", "handling_steps", "recovery_verification")

# 材料缺失/推测性内容的标注词(命中其一即视为已明确标注)
PENDING_MARKERS = ("待补", "待人工确认", "待验证")


class SopFieldSource(BaseModel):
    """关键内容的来源标注:目标字段 + 来源证据 ID(由后端分配,校验后保留)。"""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(description="目标 SOP 字段名,如 handling_steps")
    evidence_ids: list[str] = Field(default_factory=list, description="来源材料的 evidence_id")


class SopDraftContent(BaseModel):
    """SOP 草稿内容(模型输出契约;不含 doc_id/version/status 等后端字段)。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255, description="SOP 标题")

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("标题不能为空")
        return v
    applicable_env: str = Field(default="", description="适用环境及版本(不得超出来源范围)")
    symptom: str = Field(default="", description="现象")
    preconditions: str = Field(default="", description="前置条件")
    check_steps: str = Field(default="", description="排查步骤")
    handling_steps: str = Field(default="", description="处理方法(仅整理来源材料中已执行的处理)")
    recovery_verification: str = Field(default="", description="恢复验证(仅整理来源材料中已验证的结果)")
    rollback_steps: str = Field(default="", description="回滚方式(推测性内容必须标注待验证)")
    notes: str = Field(default="", description="注意事项")
    missing_information: list[str] = Field(
        default_factory=list, description="待补信息(人工核对时需要补充的项)"
    )
    sources: list[SopFieldSource] = Field(
        default_factory=list, description="关键内容的来源标注(evidence_id 必须来自来源材料)"
    )
