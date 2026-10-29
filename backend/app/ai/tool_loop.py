"""有界工具调用循环:最多 3 轮,由模型决定查什么,后端只执行只读工具。

- 厂商不支持 tools 时(400/bad_response)返回 unsupported,由调用方降级为普通分析;
- 每轮把所有 tool_calls 执行完再继续;达到轮次上限后要求模型不再调用工具;
- usage/call_count/latency 逐次累计,缺失按未知处理(不伪造)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.llm import ChatMessage, LLMError, LLMRequest
from app.ai.tools import TOOL_SPECS, ToolExecutor

MAX_TOOL_ROUNDS = 3


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
                    tools=TOOL_SPECS,
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
            tools=TOOL_SPECS,
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