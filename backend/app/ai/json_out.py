"""模型 JSON 输出的共用提取、Schema 错误反馈与有限修复。

故障分析与 SOP 草稿生成共用本模块,不各自复制一套修复循环:
- `_extract_json` 提取合法 JSON 对象(容忍代码块围栏/前后杂文);
- `_format_schema_errors` 把结构错误整理为「字段路径: 期望类型」反馈;
- `complete_json` 执行「JSON 语法错误与 Schema 结构错误共享一次修复预算」:
  修复反馈含字段路径与期望类型;修复请求继续声明 JSON 模式;逐次累计实际
  usage/call_count;预算耗尽明确失败;任一次 usage 缺失保持未知(aggregate_usage)。
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from app.ai.llm import ChatMessage, LLMError, LLMRequest, LLMResult
from app.ai.sanitize import sanitize_text

# 格式修复重试上限:JSON 语法错误与 Schema 结构错误共享这一次预算(有限重试)
MAX_FORMAT_REPAIRS = 1


def extract_json(text: str) -> dict | None:
    """从模型输出提取 JSON 对象(容忍代码块围栏/前后杂文)。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    # 提取首个平衡花括号块
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                        return data if isinstance(data, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


# Schema 校验错误类型的「期望」提示(反馈给模型的修复指引;不回显输入值)
SCHEMA_ERROR_HINTS = {
    "int_parsing": '期望整数(如 2),不能是 "v2" 等字符串',
    "int_type": "期望整数",
    "string_type": "期望字符串",
    "bool_type": "期望布尔值 true/false",
    "list_type": "期望数组",
    "dict_type": "期望对象",
    "missing": "缺少必填字段",
    "extra_forbidden": "不允许多余字段",
    "string_too_short": "字符串不能为空",
    "none_not_allowed": "不允许 null",
}


def format_schema_errors(exc: ValidationError) -> str:
    """把 Schema 校验错误整理为「字段路径: 期望类型」反馈(脱敏,供一次修复重试)。"""
    lines = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        hint = SCHEMA_ERROR_HINTS.get(err.get("type", "")) or sanitize_text(
            str(err.get("msg", ""))
        )[:120]
        lines.append(f"{loc}: {hint}")
    return sanitize_text("; ".join(lines))


def aggregate_usage(call_usages: list[dict | None]) -> dict | None:
    """逐次记录的 usage 按字段汇总:

    只有每次调用都返回数值的字段才汇总为确定值;某字段任一次缺失时不把缺失当 0,
    直接不输出该字段(前端显示「未知」);任一次整份缺失/为空 → None(整体未知)。
    """
    if not call_usages or any(not u for u in call_usages):
        return None
    common: set[str] | None = None
    for u in call_usages:
        numeric = {
            k for k, v in u.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        common = numeric if common is None else common & numeric
    if not common:
        return None
    return {k: int(sum(int(u[k]) for u in call_usages)) for k in sorted(common)}


@dataclass
class JsonCompletion:
    """一次「调用 + 有限修复」的结果;value 为 None 时按 error_code/error_message 失败。"""

    value: Any | None
    usages: list[dict | None] = field(default_factory=list)
    call_count: int = 0
    latency_ms: int = 0
    error_code: str = ""
    error_message: str = ""

    @property
    def usage(self) -> dict | None:
        return aggregate_usage(self.usages)


def complete_json(
    adapter,
    messages: list[ChatMessage],
    max_output_tokens: int,
    validate: Callable[[dict], Any],
    json_mode: bool = True,
    subject: str = "分析",
    max_repairs: int = MAX_FORMAT_REPAIRS,
) -> JsonCompletion:
    """调用模型并解析为 validate(data) 对象;语法/结构错误共享 max_repairs 次修复预算。

    validate:对合法 JSON dict 做 Schema 校验(如 AnalysisReport.model_validate),
    抛 pydantic.ValidationError 时按结构错误进入修复;成功返回的值放入 JsonCompletion.value。
    """
    messages = list(messages)
    request = LLMRequest(messages=messages, max_output_tokens=max_output_tokens, json_mode=json_mode)

    call_count = 0
    usages: list[dict | None] = []
    start = time.monotonic()
    repair_attempts = 0
    fail_code = ""
    fail_message = ""
    try:
        while True:
            call_count += 1
            result: LLMResult = adapter.chat(request)
            usages.append(result.usage)  # 仅接口实际返回时记录;缺失为 None(显示未知)

            data = extract_json(result.text)
            if data is None:
                fail_code = "bad_response"
                fail_message = f"模型输出不是合法 JSON(已有限重试);未生成{subject}"
                feedback = "输出不是合法 JSON 对象。请只输出符合约定结构的 JSON,不要其他文本。"
            else:
                try:
                    value = validate(data)
                    return JsonCompletion(
                        value=value,
                        usages=usages,
                        call_count=call_count,
                        latency_ms=int((time.monotonic() - start) * 1000),
                    )
                except ValidationError as exc:
                    fail_code = "schema_mismatch"
                    # Pydantic 错误信息可能含用户输入片段,脱敏后展示
                    fail_message = (
                        f"模型输出不符合 Schema(已有限重试): {sanitize_text(str(exc))[:500]}"
                    )
                    feedback = (
                        "输出是合法 JSON,但字段结构不符合约定。请按下列字段路径与期望类型修正"
                        '(references 的 version 必须是整数或 null:案例为 null,'
                        'SOP 为整数版本号如 2,不能写 "v2" 等字符串):\n'
                        f"{format_schema_errors(exc)}\n"
                        "请重新输出修正后的完整 JSON 对象,不要输出 JSON 以外的任何文本。"
                    )
            if repair_attempts >= max_repairs:
                return JsonCompletion(
                    value=None,
                    usages=usages,
                    call_count=call_count,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    error_code=fail_code,
                    error_message=fail_message,
                )
            repair_attempts += 1
            messages.append(ChatMessage(role="assistant", content=result.text[:4000]))
            messages.append(ChatMessage(role="user", content=feedback))
            request = request.__class__(
                messages=messages,
                max_output_tokens=request.max_output_tokens,
                json_mode=request.json_mode,  # 修复请求继续声明 JSON 模式
            )
    except LLMError as exc:
        # 模型调用失败:失败调用的 usage 不可知(整次用量按未知处理),消息已由适配层脱敏
        usages.append(None)
        return JsonCompletion(
            value=None,
            usages=usages,
            call_count=call_count,
            latency_ms=int((time.monotonic() - start) * 1000),
            error_code=exc.code,
            error_message=exc.message,
        )
