"""收尾回归:模型出站脱敏与有界 SSH 读取。"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai import chat_service
from app.ai.llm import LLMResult
from app.config import Settings
from app.monitoring import ssh
from app import backup
from app.models import Category


@pytest.mark.parametrize("streaming", [False, True])
def test_chat_redacts_question_and_knowledge_title_before_model(db, operator, monkeypatch, streaming):
    captured = []

    class Adapter:
        def chat(self, request):
            captured.extend(message.content for message in request.messages)
            return LLMResult(text=json.dumps({"answer": "先检查连接", "citations": []}),
                             usage=None, model="fake", latency_ms=1)

    monkeypatch.setattr(chat_service, "resolve_effective_ai", lambda *args: None)
    monkeypatch.setattr(chat_service, "build_adapter", lambda *args, **kwargs: Adapter())
    monkeypatch.setattr(chat_service, "build_executor", lambda *args, **kwargs: None)
    hit = SimpleNamespace(ref_id="case:1", title="token=KnowledgeSecret", source_type="case",
                          source_id=1, version=None, snippet="连接失败")
    monkeypatch.setattr(chat_service, "search_knowledge_hybrid",
                        lambda *args: SimpleNamespace(hits=[hit], knowledge_version="test"))
    monkeypatch.setattr(chat_service, "stream_session_factory", lambda: db)
    conversation = chat_service.create_conversation(db, operator)
    question = '连接失败，password=QuestionSecret'
    settings = Settings(ai_mode="local", ai_base_url="http://127.0.0.1:9", ai_model="fake")
    if streaming:
        events = list(chat_service.ask_stream(conversation.id, operator.id, question, settings))
        assert events[-1]["type"] == "done"
        assert events[0]["message"]["content"] == question
    else:
        user_row, assistant = chat_service.ask(db, operator, conversation, question, settings)
        assert user_row.content == question
        assert assistant.status == "ok"
    assert captured
    assert "QuestionSecret" not in "\n".join(captured)
    assert "KnowledgeSecret" not in "\n".join(captured)


def test_ssh_wait_for_exit_is_bounded(monkeypatch):
    class Channel:
        def settimeout(self, value):
            pass

        def recv_ready(self):
            return False

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return False

        def recv_exit_status(self):
            pytest.fail("必须先确认退出状态就绪,不能无期限等待")

    channel = Channel()
    closed = []
    client = SimpleNamespace(exec_command=lambda *args, **kwargs: (
        None, SimpleNamespace(channel=channel, read=lambda size: b""),
        SimpleNamespace(read=lambda size: b"")), close=lambda: closed.append(True))
    monkeypatch.setattr(ssh, "_connect", lambda *args, **kwargs: client)
    with pytest.raises(ssh.SshError) as exc:
        ssh.run_command(ssh.SshTarget("host", 22, "user", "pem"), "uptime", timeout=0.02)
    assert exc.value.code == "timeout"
    assert closed == [True]


def test_platform_dump_uses_same_snapshot_as_metadata(db, tmp_path, monkeypatch):
    """取完元信息后另一个事务提交,归档仍须保持原时点。"""
    engine = db.get_bind()
    monkeypatch.setattr(backup, "SessionLocal", lambda: Session(engine))
    with engine.begin() as conn:
        conn.execute(Category.__table__.insert().values(name="snapshot-before"))

    def snapshot(session):
        count = session.scalar(text("SELECT count(*) FROM categories"))
        with engine.begin() as conn:
            conn.execute(Category.__table__.insert().values(name="snapshot-after"))
        return {"counts": {"categories": count}}

    monkeypatch.setattr(backup, "_snapshot", snapshot)
    current = Settings()
    settings = current.model_copy(update={"postgres_db": current.postgres_test_db,
                                          "backup_dir": str(tmp_path), "backup_enabled": True})
    result = backup.run_backup(settings, "test", db)
    import subprocess
    import os
    env = {**os.environ, "PGPASSWORD": settings.postgres_password}
    contents = subprocess.run(["pg_restore", "--data-only", "--table=categories", "-f", "-", result["path"]],
                              capture_output=True, text=True, env=env, check=True).stdout
    assert result["source_snapshot"]["counts"]["categories"] == 1
    assert "snapshot-before" in contents
    assert "snapshot-after" not in contents
