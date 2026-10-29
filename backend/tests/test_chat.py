"""AI 对话:权限隔离、非运维拒答、引用校验、管理员查看、沉淀与留存。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.ai import chat_service
from app.ai.chat_schema import ChatAnswer
from app.ai.llm import LLMResult
from app.ai.providers import UserAiConfig
from app.models import (
    ChatConversation,
    ChatMessage,
    Fault,
    FaultRecord,
    Role,
    Sop,
    User,
)


class FakeAdapter:
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
    adapter = FakeAdapter(payloads)
    monkeypatch.setattr(chat_service, "build_adapter", lambda settings, user_config=None: adapter)
    return adapter


def _answer(**overrides):
    payload = {
        "answer": "先检查磁盘与负载,再确认服务日志。",
        "citations": [],
        "insufficient_evidence": False,
        "refusal": False,
        "suggested_actions": ["df -h", "uptime"],
    }
    payload.update(overrides)
    return payload


def _create(client, headers, title="新对话"):
    resp = client.post("/api/chat/conversations", json={"title": title}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_ask_returns_answer_and_persists(client, db, operator, op_headers, monkeypatch):
    _enable_fake_ai(monkeypatch, [_answer()])
    conversation_id = _create(client, op_headers)

    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "磁盘满了怎么排查?"},
        headers=op_headers,
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["assistant_message"]["status"] == "ok"
    assert "磁盘" in body["assistant_message"]["content"]
    assert body["assistant_message"]["usage"]["total_tokens"] == 20

    detail = client.get(f"/api/chat/conversations/{conversation_id}", headers=op_headers).json()
    assert len(detail["messages"]) == 2
    assert detail["conversation"]["title"] == "磁盘满了怎么排查?"


def test_chat_fails_clearly_when_ai_disabled(client, operator, op_headers):
    conversation_id = _create(client, op_headers)
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "帮我看看负载"},
        headers=op_headers,
    )
    assert resp.status_code == 400
    assert "AI 未启用" in resp.json()["detail"]


def test_conversation_is_isolated_per_user(client, db, op_headers, adm_headers, monkeypatch):
    _enable_fake_ai(monkeypatch, [_answer()])
    conversation_id = _create(client, op_headers)

    # 其他用户(管理员接口走 require_admin,这里用 reviewer 登录)不能读他人会话
    from app.security import hash_password

    other = User(
        username="other_chat",
        display_name="其他",
        password_hash=hash_password("test-password-1"),
        role=Role.reviewer,
    )
    db.add(other)
    db.commit()
    login = client.post("/api/auth/login", json={"username": "other_chat", "password": "test-password-1"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get(f"/api/chat/conversations/{conversation_id}", headers=headers).status_code == 404


def test_refusal_is_stored(client, op_headers, monkeypatch):
    _enable_fake_ai(
        monkeypatch,
        [_answer(answer="只能回答运维相关问题。", refusal=True, suggested_actions=[])],
    )
    conversation_id = _create(client, op_headers)
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "今天天气怎么样?"},
        headers=op_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["assistant_message"]["content"] == "只能回答运维相关问题。"


def test_verify_citations_drops_unknown_and_mismatched():
    # 用 SearchHit 结构构造最小对象
    from app.search.keyword import SearchHit

    hit = SearchHit(
        ref_id="case:1",
        source_type="case",
        source_id=1,
        version=None,
        title="案例",
        snippet="内容",
        link="/cases/1",
        status="approved",
        score=1.0,
    )
    answer = ChatAnswer(
        answer="a",
        citations=[
            {"ref_id": "case:1", "source_type": "case", "source_id": 1, "version": None, "relevance": "r"},
            {"ref_id": "case:2", "source_type": "case", "source_id": 2, "version": None, "relevance": "r"},
            {"ref_id": "case:1", "source_type": "sop", "source_id": 9, "version": None, "relevance": "r"},
        ],
    )
    verified, notes = chat_service._verify_citations(answer, [hit])
    assert [c.ref_id for c in verified.citations] == ["case:1"]
    assert any("不在本次检索结果" in n for n in notes)


def test_admin_can_read_all_conversations(client, db, op_headers, adm_headers, monkeypatch):
    _enable_fake_ai(monkeypatch, [_answer()])
    conversation_id = _create(client, op_headers)
    client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "如何排查 504?"},
        headers=op_headers,
    )
    assert client.get("/api/admin/chat/conversations", headers=op_headers).status_code == 403
    listed = client.get("/api/admin/chat/conversations", headers=adm_headers)
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["id"] == conversation_id)
    assert row["username"] == "op_user"
    assert row["message_count"] == 2

    detail = client.get(f"/api/admin/chat/conversations/{conversation_id}", headers=adm_headers)
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 2


def test_sediment_to_fault_record(client, db, operator, op_headers, monkeypatch):
    _enable_fake_ai(monkeypatch, [_answer()])
    fault = Fault(title="沉淀目标", created_by=operator.id)
    db.add(fault)
    db.commit()
    conversation_id = _create(client, op_headers)
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "磁盘排查"},
        headers=op_headers,
    )
    message_id = resp.json()["assistant_message"]["id"]

    sediment = client.post(
        f"/api/chat/messages/{message_id}/to-fault-record",
        json={"fault_id": fault.id},
        headers=op_headers,
    )
    assert sediment.status_code == 200, sediment.text
    assert sediment.json()["kind"] == "handling_note"
    db.expire_all()
    record = db.query(FaultRecord).filter(FaultRecord.fault_id == fault.id).one()
    assert "磁盘" in record.content


def test_sediment_to_sop_draft(client, db, op_headers, monkeypatch):
    _enable_fake_ai(monkeypatch, [_answer()])
    conversation_id = _create(client, op_headers)
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "生成一个排查步骤"},
        headers=op_headers,
    )
    message_id = resp.json()["assistant_message"]["id"]
    sediment = client.post(
        f"/api/chat/messages/{message_id}/to-sop-draft",
        json={"title": "磁盘排查草稿"},
        headers=op_headers,
    )
    assert sediment.status_code == 200, sediment.text
    assert sediment.json()["status"] == "draft"
    db.expire_all()
    sop = db.query(Sop).filter(Sop.title == "磁盘排查草稿").one()
    assert "磁盘" in sop.handling_steps


def test_failed_assistant_message_is_persisted(client, op_headers, monkeypatch):
    class BrokenAdapter:
        def chat(self, request):
            from app.ai.llm import LLMError

            raise LLMError("timeout", "模型调用超时")

    monkeypatch.setattr(
        chat_service,
        "resolve_effective_ai",
        lambda db, user, settings: UserAiConfig("deepseek", "https://api.example.com/v1", "m", "k"),
    )
    monkeypatch.setattr(chat_service, "build_adapter", lambda settings, user_config=None: BrokenAdapter())
    conversation_id = _create(client, op_headers)
    resp = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": "排查一下"},
        headers=op_headers,
    )
    assert resp.status_code == 201
    assistant = resp.json()["assistant_message"]
    assert assistant["status"] == "failed"
    assert assistant["error_code"] == "timeout"


def test_prune_chat_removes_expired_only(db, operator):
    now = datetime.now(timezone.utc)
    old_conv = ChatConversation(user_id=operator.id, title="旧")
    new_conv = ChatConversation(user_id=operator.id, title="新")
    db.add_all([old_conv, new_conv])
    db.flush()
    db.add(ChatMessage(conversation_id=old_conv.id, role="user", content="旧消息", created_at=now - timedelta(days=100)))
    db.add(ChatMessage(conversation_id=new_conv.id, role="user", content="新消息", created_at=now - timedelta(days=1)))
    db.commit()

    result = chat_service.prune_chat(db, 90, now=now)
    assert result["messages_deleted"] == 1
    db.expire_all()
    assert db.get(ChatConversation, old_conv.id) is None
    assert db.get(ChatConversation, new_conv.id) is not None