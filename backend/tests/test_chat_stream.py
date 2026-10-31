"""流式回答:answer 增量提取、SSE 适配器、工具循环与接口事件序列。"""

import json

import pytest

from app.ai import chat_service
from app.ai.answer_stream import AnswerStreamExtractor
from app.ai.llm import (
    ChatMessage,
    LLMCapabilities,
    LLMRequest,
    LLMResult,
    OpenAICompatAdapter,
    OpenAICompatEndpoint,
)
from app.ai.tool_loop import run_tool_loop_stream
from app.ai.tools import ToolOutcome
from tests.conftest import TestingSessionLocal


@pytest.fixture(autouse=True)
def _stream_uses_test_database(monkeypatch):
    """流式生成器使用独立会话,必须指向测试库。"""
    monkeypatch.setattr(chat_service, "stream_session_factory", TestingSessionLocal)

# ===== answer 增量提取 =====


def _feed_all(extractor: AnswerStreamExtractor, chunks: list[str]) -> str:
    return "".join(extractor.feed(chunk) for chunk in chunks)


def test_extract_answer_handles_split_escapes():
    raw = r'{"answer": "第一行\n第二行 \"引号\" \u4e2d\u6587", "citations": []}'
    extractor = AnswerStreamExtractor()
    # 每 2 个字符一段:覆盖转义符/\\uXXXX 被拆分到不同 chunk 的情况
    pieces = [raw[i : i + 2] for i in range(0, len(raw), 2)]
    assert _feed_all(extractor, pieces) == '第一行\n第二行 "引号" 中文'
    assert extractor.finished


def test_extract_answer_skips_code_fence_and_decodes_surrogate_pair():
    raw = r'```json' + "\n" + r'{"answer": "\ud83d\ude00 完成", "citations": []}' + "\n```"
    extractor = AnswerStreamExtractor()
    assert _feed_all(extractor, list(raw)) == "😀 完成"


def test_extract_answer_absent_outputs_nothing():
    extractor = AnswerStreamExtractor()
    assert extractor.feed('{"citations": []}') == ""
    assert not extractor.finished


# ===== OpenAI 兼容流式解析 =====


class _FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        yield from self._lines


class _FakeStreamClient:
    """替换 httpx.Client:按脚本返回 SSE 行,并记录请求体。"""

    responses: list[tuple[int, list[str]]] = []
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, json=None, headers=None):
        _FakeStreamClient.calls.append({"method": method, "url": url, "body": json})
        status_code, lines = _FakeStreamClient.responses.pop(0)
        return _FakeStreamResponse(status_code, lines)


def _adapter() -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        OpenAICompatEndpoint(base_url="https://api.example.com/v1", model="m", api_key="sk-x"),
        LLMCapabilities(),
    )


def _run_stream(adapter, request):
    generator = adapter.chat_stream(request)
    pieces = []
    while True:
        try:
            pieces.append(next(generator))
        except StopIteration as stop:
            return pieces, stop.value


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def test_adapter_stream_accumulates_text_usage_and_tool_calls(monkeypatch):
    calls = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "prometheus_", "arguments": '{"que'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"name": "query", "arguments": 'ry":"up"}'}}]}}
            ]
        },
        {
            "choices": [{"delta": {"content": '{"answer": "ok"}'}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    ]
    _FakeStreamClient.responses = [(200, [_sse(item) for item in calls] + ["data: [DONE]"])]
    _FakeStreamClient.calls = []
    monkeypatch.setattr("app.ai.llm.httpx.Client", _FakeStreamClient)

    request = LLMRequest(messages=[ChatMessage(role="user", content="hi")])
    pieces, result = _run_stream(_adapter(), request)

    assert "".join(pieces) == '{"answer": "ok"}'
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert result.tool_calls[0]["function"]["name"] == "prometheus_query"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"query": "up"}
    body = _FakeStreamClient.calls[-1]["body"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_adapter_stream_retries_without_stream_options_on_400(monkeypatch):
    _FakeStreamClient.responses = [
        (400, []),
        (200, [_sse({"choices": [{"delta": {"content": "hi"}}]})]),
    ]
    _FakeStreamClient.calls = []
    monkeypatch.setattr("app.ai.llm.httpx.Client", _FakeStreamClient)

    pieces, result = _run_stream(
        _adapter(), LLMRequest(messages=[ChatMessage(role="user", content="hi")])
    )

    assert "".join(pieces) == "hi"
    assert "stream_options" not in _FakeStreamClient.calls[-1]["body"]
    assert _FakeStreamClient.calls[-1]["body"]["stream"] is True


def test_adapter_stream_accepts_non_sse_gateway_response(monkeypatch):
    body = {"choices": [{"message": {"content": "整段返回"}}], "model": "gateway"}
    _FakeStreamClient.responses = [(200, [json.dumps(body, ensure_ascii=False)])]
    _FakeStreamClient.calls = []
    monkeypatch.setattr("app.ai.llm.httpx.Client", _FakeStreamClient)

    pieces, result = _run_stream(
        _adapter(), LLMRequest(messages=[ChatMessage(role="user", content="hi")])
    )
    assert "".join(pieces) == "整段返回"
    assert result.text == "整段返回"


# ===== 工具循环流式版本 =====


class _StubExecutor:
    def __init__(self):
        self.invocations: list[dict] = []

    def execute(self, name: str, arguments):
        self.invocations.append(
            {
                "tool": name,
                "arguments": {"query": "up"},
                "status": "ok",
                "result_excerpt": "1",
                "error": "",
                "latency_ms": 3,
            }
        )
        return ToolOutcome(content="[evidence: tool:1] 1", evidence_id="tool:1")


class _ScriptedStreamingAdapter:
    """每轮:(文本分片, tool_calls)。分片逐段产出,模拟真实流式。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)

    def chat_stream(self, request):
        pieces, tool_calls = self.rounds.pop(0)
        for piece in pieces:
            yield piece
        return LLMResult(
            text="".join(pieces),
            usage={"total_tokens": 1},
            model="fake",
            latency_ms=2,
            tool_calls=tool_calls,
        )


def test_tool_loop_stream_emits_text_tools_and_done():
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "prometheus_query", "arguments": '{"query": "up"}'},
    }
    adapter = _ScriptedStreamingAdapter(
        [(["先查一下指标"], [tool_call]), (['{"answer": "ok"}'], [])]
    )
    executor = _StubExecutor()
    events = list(
        run_tool_loop_stream(
            adapter,
            [ChatMessage(role="user", content="q")],
            executor,
            max_output_tokens=100,
        )
    )
    kinds = [event["type"] for event in events]
    assert kinds == ["text", "tools_start", "tool", "text", "done"]
    assert events[1]["count"] == 1
    assert events[2]["call"]["tool"] == "prometheus_query"
    assert events[-1]["result"].text == '{"answer": "ok"}'
    assert events[-1]["result"].used_tools is True
    assert events[-1]["result"].call_count == 2


# ===== 流式接口 =====


class _FakeAdapter:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def chat(self, request):
        return LLMResult(
            text=json.dumps(self.payloads.pop(0), ensure_ascii=False),
            usage={"total_tokens": 20},
            model="fake",
            latency_ms=5,
        )


def _enable_fake_ai(monkeypatch, payloads):
    from app.ai.providers import UserAiConfig

    monkeypatch.setattr(
        chat_service,
        "resolve_effective_ai",
        lambda db, user, settings: UserAiConfig(
            provider="deepseek",
            base_url="https://api.example.com/v1",
            model="fake-model",
            api_key="sk-fake",
        ),
    )
    monkeypatch.setattr(
        chat_service, "build_adapter", lambda settings, user_config=None: _FakeAdapter(payloads)
    )


def _answer(answer: str = "先检查磁盘与负载。", **overrides):
    payload = {
        "answer": answer,
        "citations": [],
        "insufficient_evidence": False,
        "refusal": False,
        "suggested_actions": [],
    }
    payload.update(overrides)
    return payload


def _read_sse(client, url: str, payload: dict, headers: dict) -> list[dict]:
    events: list[dict] = []
    with client.stream("POST", url, json=payload, headers=headers, timeout=30) as resp:
        assert resp.status_code == 200, resp.read()
        assert resp.headers["content-type"].startswith("text/event-stream")
        buffer = ""
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                data = "\n".join(
                    line[5:].lstrip()
                    for line in raw.split("\n")
                    if line.startswith("data:")
                )
                if data:
                    events.append(json.loads(data))
    return events


def _create(client, headers, title="新对话"):
    resp = client.post("/api/chat/conversations", json={"title": title}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_stream_endpoint_emits_delta_and_persists(client, db, operator, op_headers, monkeypatch):
    answer = "## 排查\n- 先看 `df -h`\n- 再看 **负载**"
    _enable_fake_ai(monkeypatch, [_answer(answer=answer)])
    conversation_id = _create(client, op_headers)

    events = _read_sse(
        client,
        f"/api/chat/conversations/{conversation_id}/messages/stream",
        {"content": "磁盘满了怎么排查?"},
        op_headers,
    )
    kinds = [event["type"] for event in events]
    assert kinds[0] == "user_message"
    assert "delta" in kinds
    assert kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == answer
    done = events[-1]
    assert done["assistant_message"]["content"] == answer
    assert done["assistant_message"]["usage"]["total_tokens"] == 20
    assert done["assistant_message"]["model_provider"] == "deepseek"
    assert done["assistant_message"]["model_name"] == "fake-model"
    assert done["title"] == "磁盘满了怎么排查?"

    detail = client.get(f"/api/chat/conversations/{conversation_id}", headers=op_headers).json()
    assert [row["role"] for row in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == answer


def test_stream_endpoint_reports_unavailable_as_error_event(client, operator, op_headers):
    conversation_id = _create(client, op_headers)
    events = _read_sse(
        client,
        f"/api/chat/conversations/{conversation_id}/messages/stream",
        {"content": "帮我看看负载"},
        op_headers,
    )
    assert events[0]["type"] == "error"
    assert "AI 未启用" in events[0]["message"]

    detail = client.get(f"/api/chat/conversations/{conversation_id}", headers=op_headers).json()
    assert detail["messages"] == []  # 未产生任何消息


def test_stream_endpoint_rejects_other_users_conversation(client, db, op_headers, monkeypatch):
    _enable_fake_ai(monkeypatch, [_answer()])
    conversation_id = _create(client, op_headers)

    from app.models import Role, User
    from app.security import hash_password

    db.add(
        User(
            username="stream_other",
            display_name="其他",
            password_hash=hash_password("test-password-1"),
            role=Role.reviewer,
        )
    )
    db.commit()
    login = client.post(
        "/api/auth/login", json={"username": "stream_other", "password": "test-password-1"}
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages/stream",
        json={"content": "hi"},
        headers=headers,
    )
    assert resp.status_code == 404