"""AI 分析输出的固定 Schema(模型输出契约)。

字段与 PROJECT_PROMPT.md 要求一致:
summary / observed_facts / suggested_category / suggested_tags /
hypotheses / missing_information / next_checks / references。

校验原则:
- 结构由 Pydantic 严格校验;证据 ID、来源 ID/版本由后端二次校验(app.ai.verify);
- 允许证据不足(insufficient_evidence=True)与无引用(references=[]);
- 不要求置信百分比;优先级用「优先排查/次优先/证据不足」等表达;
- 检查命令只展示不执行;引用 ID 必须来自后端本次下发的片段清单。
"""

from pydantic import BaseModel, ConfigDict, Field


class ObservedFact(BaseModel):
    """已知事实:必须绑定输入证据 ID(后端分配)。"""

    model_config = ConfigDict(extra="forbid")

    evidence_ids: list[str] = Field(default_factory=list, description="引用的输入证据 ID")
    fact: str = Field(min_length=1, description="事实描述(不含推测)")


class Hypothesis(BaseModel):
    """候选原因:依据、缺失信息、验证方法三要素齐全;不输出概率。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, description="候选原因简述")
    rationale: str = Field(min_length=1, description="依据(基于哪些事实/知识)")
    evidence_ids: list[str] = Field(default_factory=list, description="支持的输入证据 ID")
    reference_ids: list[str] = Field(default_factory=list, description="支持的知识来源 ID")
    missing_information: list[str] = Field(default_factory=list, description="尚缺的信息")
    verification: str = Field(min_length=1, description="验证方法")
    priority: str = Field(
        default="次优先",
        description="优先级表达:优先排查 | 次优先 | 证据不足(不使用概率)",
    )


class NextCheck(BaseModel):
    """下一步检查:命令只展示与复制,应用不执行。"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, description="检查目的与操作说明")
    command: str = Field(default="", description="只读检查命令(仅展示,不执行)")
    related_hypothesis: str = Field(default="", description="关联的候选原因标题(可空)")


class KnowledgeReference(BaseModel):
    """相关知识引用:source_id/version 必须与后端下发片段一致。"""

    model_config = ConfigDict(extra="forbid")

    ref_id: str = Field(min_length=1, description="后端下发的来源片段 ID,如 case:12#s1")
    source_type: str = Field(description="case | sop")
    source_id: int
    version: int | None = Field(
        default=None,
        description='SOP 当前版本整数(如 2);案例为 null;不得写 "v2" 等字符串(不宽松转换)',
    )
    relevance: str = Field(default="", description="与当前问题的关联说明")


class AnalysisReport(BaseModel):
    """固定分析报告 Schema。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, description="问题摘要")
    observed_facts: list[ObservedFact] = Field(default_factory=list)
    suggested_category: str | None = Field(
        default=None, description="建议分类名(须为系统分类之一;不确定时为 null)"
    )
    suggested_tags: list[str] = Field(default_factory=list, max_length=8)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    next_checks: list[NextCheck] = Field(default_factory=list)
    references: list[KnowledgeReference] = Field(default_factory=list)
    insufficient_evidence: bool = Field(
        default=False, description="证据不足、无法给出判断时为 true"
    )


# 规范化输出(Schema 之外的运行元数据由后端补充,不信任模型)
class EvidenceItem(BaseModel):
    """后端分配稳定 ID 的输入证据片段。"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source: str = Field(description="证据来源字段: description | error_logs | record:<id> 等")
    text: str


class KnowledgeSnippet(BaseModel):
    """通过权限/状态校验的知识片段。"""

    model_config = ConfigDict(extra="forbid")

    ref_id: str = Field(description="稳定来源 ID: <type>:<id> 或 <type>:<id>#v<version>")
    source_type: str = Field(description="case | sop")
    source_id: int
    version: int | None = None
    title: str
    snippet: str = Field(description="相关片段(定位用)")
    link: str = Field(description="前端定位链接路径")
    status: str = Field(description="来源当前状态(展示用)")
