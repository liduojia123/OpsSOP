"""有界工具调用循环:最多 3 轮,由模型决定查什么,后端只执行只读工具。

- 厂商不支持 tools 时(400/bad_response)返回 unsupported,由调用方降级为普通分析;
- 每轮把所有 tool_calls 执行完再继续;达到轮次上限后要求模型不再调用工具;
- usage/call_count/latency 逐次累计,缺失按未知处理(不伪造)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.llm import ChatMessage, LLMError, LLMRequest, LLMResult
from app.ai.tools import TOOL_SPECS, ToolExecutor

MAX_TOOL_ROUNDS = 3


def _tool_specs(executor: ToolExecutor) -> list[dict]:
    """工具清单由执行器按上下文决定:分析只有监控工具,对话还有业务数据工具。"""
    getter = getattr(executor, "specs", None)
    return getter() if callable(getter) else TOOL_SPECS


def iter_chat_stream(adapter, request: LLMRequest):
    """统一的逐段调用:支持 chat_stream 的适配器边生成边返回,否则整段返回一次。

    生成器返回值(StopIteration.value)为完整 LLMResult。
    """
    streamer = getattr(adapter, "chat_stream", None)
    if streamer is None:
        result = adapter.chat(request)
        if result.text:
            yield result.text
        return result
    generator = streamer(request)
    while True:
        try:
            piece = next(generator)
        except StopIteration as stop:
            return stop.value
        yield piece


@dataclass
class ToolLoopResult:
    text: str
    messages: list[ChatMessage] = field(default_factory=list)
    used_tools: bool = False
    unsupported: str = ""
    usages: list[dict | None] = field(default_factory=list)
    call_count: int = 0
    latency_ms: int = 0


def run_tool_loop(
    adapter,
    messages: list[ChatMessage],
    executor: ToolExecutor,
    *,
    max_output_tokens: int,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> ToolLoopResult:
    working = list(messages)
    usages: list[dict | None] = []
    call_count = 0
    latency_ms = 0

    for round_index in range(max_rounds):
        try:
            result = adapter.chat(
                LLMRequest(
                    messages=working,
                    max_output_tokens=max_output_tokens,
                    tools=_tool_specs(executor),
                    tool_choice="auto",
                )
            )
        except LLMError as exc:
            usages.append(None)
            call_count += 1
            if round_index == 0 and exc.code in ("bad_request", "bad_response"):
                return ToolLoopResult(
                    text="",
                    messages=working,
                    used_tools=False,
                    unsupported=exc.message,
                    usages=usages,
                    call_count=call_count,
                    latency_ms=latency_ms,
                )
            raise
        usages.append(result.usage)
        call_count += 1
        latency_ms += result.latency_ms
        if not result.tool_calls:
            return ToolLoopResult(
                text=result.text,
                messages=working,
                used_tools=bool(executor.invocations),
                usages=usages,
                call_count=call_count,
                latency_ms=latency_ms,
            )

        working.append(
            ChatMessage(
                role="assistant",
                content=result.text or "",
                tool_calls=result.tool_calls,
            )
        )
        for call in result.tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            outcome = executor.execute(name, function.get("arguments"))
            working.append(
                ChatMessage(
                    role="tool",
                    content=outcome.content,
                    tool_call_id=str(call.get("id") or ""),
                    name=name,
                )
            )

    working.append(
        ChatMessage(
            role="user",
            content=(
                "已达到实时查询轮次上限。请停止调用工具,基于以上信息直接输出最终 JSON 报告。"
            ),
        )
    )
    # 保留 tools 定义并显式禁止调用:部分厂商在缺少 tools 时会拒绝 tool 角色消息
    result = adapter.chat(
        LLMRequest(
            messages=working,
            max_output_tokens=max_output_tokens,
            tools=_tool_specs(executor),
            tool_choice="none",
        )
    )
    usages.append(result.usage)
    call_count += 1
    latency_ms += result.latency_ms
    return ToolLoopResult(
        text=result.text,
        messages=working,
        used_tools=bool(executor.invocations),
        usages=usages,
        call_count=call_count,
        latency_ms=latency_ms,
    )


def run_tool_loop_stream(
    adapter,
    messages: list[ChatMessage],
    executor: ToolExecutor,
    *,
    max_output_tokens: int,
    max_rounds: int = MAX_TOOL_ROUNDS,
):
    """流式工具循环:与 run_tool_loop 相同的轮次/额度约束,但逐段转发模型文本。

    yield 事件:
      {"type": "text", "text": str}          模型本轮内容增量(可能是最终 JSON 的一部分)
      {"type": "tools_start", "count": int}  本轮将执行只读工具,调用方应清空已展示的草稿
      {"type": "tool", "call": dict}         一次工具调用结果(已脱敏/截断)
      {"type": "done", "result": ToolLoopResult}
    第一轮因厂商不支持 tools 返回 400 时以 unsupported 结束,由调用方降级。
    """
    working = list(messages)
    usages: list[dict | None] = []
    call_count = 0
    latency_ms = 0

    for round_index in range(max_rounds):
        request = LLMRequest(
            messages=working,
            max_output_tokens=max_output_tokens,
            tools=_tool_specs(executor),
            tool_choice="auto",
        )
        text_parts: list[str] = []
        produced = False
        result: LLMResult | None = None
        generator = iter_chat_stream(adapter, request)
        try:
            while True:
                try:
                    piece = next(generator)
                except StopIteration as stop:
                    result = stop.value
                    break
                if not piece:
                    continue
                produced = True
                text_parts.append(piece)
                yield {"type": "text", "text": piece}
        except LLMError as exc:
            usages.append(None)
            call_count += 1
            if (
                round_index == 0
                and not produced
                and exc.code in ("bad_request", "bad_response")
            ):
                yield {
                    "type": "done",
                    "result": ToolLoopResult(
                        text="",
                        messages=working,
                        used_tools=False,
                        unsupported=exc.message,
                        usages=usages,
                        call_count=call_count,
                        latency_ms=latency_ms,
                    ),
                }
                return
            raise

        if result is None:  # 适配器未返回完整结果时用已收文本兜底
            result = LLMResult(
                text="".join(text_parts), usage=None, model="", latency_ms=0
            )
        usages.append(result.usage)
        call_count += 1
        latency_ms += result.latency_ms
        if not result.tool_calls:
            yield {
                "type": "done",
                "result": ToolLoopResult(
                    text=result.text or "".join(text_parts),
                    messages=working,
                    used_tools=bool(executor.invocations),
                    usages=usages,
                    call_count=call_count,
                    latency_ms=latency_ms,
                ),
            }
            return

        working.append(
            ChatMessage(
                role="assistant",
                content=result.text or "",
                tool_calls=result.tool_calls,
            )
        )
        yield {"type": "tools_start", "count": len(result.tool_calls)}
        for call in result.tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            outcome = executor.execute(name, function.get("arguments"))
            working.append(
                ChatMessage(
                    role="tool",
                    content=outcome.content,
                    tool_call_id=str(call.get("id") or ""),
                    name=name,
                )
            )
            if executor.invocations:
                yield {"type": "tool", "call": executor.invocations[-1]}

    working.append(
        ChatMessage(
            role="user",
            content=(
                "已达到实时查询轮次上限。请停止调用工具,基于以上信息直接输出最终 JSON 报告。"
            ),
        )
    )
    # 保留 tools 定义并显式禁止调用:部分厂商在缺少 tools 时会拒绝 tool 角色消息
    request = LLMRequest(
        messages=working,
        max_output_tokens=max_output_tokens,
        tools=_tool_specs(executor),
        tool_choice="none",
    )
    text_parts = []
    generator = iter_chat_stream(adapter, request)
    result = None
    while True:
        try:
            piece = next(generator)
        except StopIteration as stop:
            result = stop.value
            break
        if not piece:
            continue
        text_parts.append(piece)
        yield {"type": "text", "text": piece}
    if result is None:
        result = LLMResult(text="".join(text_parts), usage=None, model="", latency_ms=0)
    usages.append(result.usage)
    call_count += 1
    latency_ms += result.latency_ms
    yield {
        "type": "done",
        "result": ToolLoopResult(
            text=result.text or "".join(text_parts),
            messages=working,
            used_tools=bool(executor.invocations),
            usages=usages,
            call_count=call_count,
            latency_ms=latency_ms,
        ),
    }