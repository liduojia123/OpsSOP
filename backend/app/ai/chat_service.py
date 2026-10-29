"""AI 运维对话:会话、检索增强回答、实时工具、引用校验与留存清理。

边界(与产品约定一致):
- 只回答运维问题;非运维由提示词要求 refusal=true;
- 不执行任何变更,SSH 只跑只读模板;不泄露密钥与他人数据;
- 引用只能来自本次检索到的、当前用户有权查看的知识片段;
- 对话与工具调用都写审计;消息保留 CHAT_RETENTION_DAYS(默认 90)天。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai.chat_schema import CHAT_SYSTEM_PROMPT, CHAT_USER_TEMPLATE, ChatAnswer
from app.ai.json_out import JsonCompletion, complete_json, extract_json
from app.ai.keys import effective_settings, resolve_effective_ai
from app.ai.llm import ChatMessage as LLMMessage
from app.ai.llm import LLMError, build_adapter
from app.ai.sanitize import sanitize_text
from app.ai.tool_loop import run_tool_loop
from app.ai.tools import ToolExecutor, build_executor
from app.config import Settings
from app.monitoring.defaults import ai_tools_configured
from app.models import (
    AiToolInvocation,
    ChatConversation,
    ChatMessage,
    FaultRecord,
    RecordKind,
    Sop,
    SopStatus,
    User,
    utcnow,
)
from app.search.hybrid import search_knowledge_hybrid

MAX_QUESTION_CHARS = 8000
MAX_HISTORY_MESSAGES = 8
_RATE_LIMIT = 10
_RATE_WINDOW = 60
_rate_lock = threading.Lock()
_rate_buckets: dict[int, list[float]] = {}


class ChatError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ChatUnavailable(ChatError):
    pass


class ChatRateLimited(ChatError):
    pass


def _check_rate(user_id: int) -> None:
    now = time.monotonic()
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(user_id, []) if now - t < _RATE_WINDOW]
        if len(bucket) >= _RATE_LIMIT:
            _rate_buckets[user_id] = bucket
            raise ChatRateLimited("对话请求过于频繁,请稍后再试")
        bucket.append(now)
        _rate_buckets[user_id] = bucket


def _clip(text: str, limit: int) -> str:
    return sanitize_text(text or "")[:limit]


def create_conversation(db: Session, user: User, title: str = "") -> ChatConversation:
    row = ChatConversation(user_id=user.id, title=(title or "新对话").strip()[:128])
    db.add(row)
    db.commit()
    return row


def list_conversations(db: Session, user: User, limit: int = 100) -> list[ChatConversation]:
    return list(
        db.scalars(
            select(ChatConversation)
            .where(ChatConversation.user_id == user.id)
            .order_by(ChatConversation.updated_at.desc())
            .limit(limit)
        ).all()
    )


def get_conversation(db: Session, user: User, conversation_id: int) -> ChatConversation:
    row = db.get(ChatConversation, conversation_id)
    if row is None or row.user_id != user.id:
        raise ChatError("会话不存在或无权访问")
    return row


def get_messages(db: Session, conversation: ChatConversation, limit: int = 200) -> list[ChatMessage]:
    return list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.id)
            .limit(limit)
        ).all()
    )


def delete_conversation(db: Session, user: User, conversation_id: int) -> None:
    row = get_conversation(db, user, conversation_id)
    db.execute(delete(ChatMessage).where(ChatMessage.conversation_id == row.id))
    db.execute(
        delete(AiToolInvocation).where(AiToolInvocation.conversation_id == row.id)
    )
    db.delete(row)
    db.commit()


def _knowledge_block(hits) -> str:
    if not hits:
        return "(无匹配知识;不要编造引用)"
    lines = []
    for hit in hits:
        lines.append(
            f"[{hit.ref_id}] {hit.title} "
            f"(source_type={hit.source_type}, source_id={hit.source_id}, version={hit.version})\n"
            f"{_clip(hit.snippet, 800)}"
        )
    return "\n\n".join(lines)


def _verify_citations(answer: ChatAnswer, hits) -> tuple[ChatAnswer, list[str]]:
    valid = {hit.ref_id: hit for hit in hits}
    kept = []
    notes: list[str] = []
    seen: set[str] = set()
    for citation in answer.citations:
        hit = valid.get(citation.ref_id)
        if hit is None:
            notes.append(f"引用 {citation.ref_id} 不在本次检索结果中,已移除")
            continue
        if citation.source_type != hit.source_type or citation.source_id != hit.source_id:
            notes.append(f"引用 {citation.ref_id} 的来源信息不一致,已移除")
            continue
        if citation.version is not None and hit.version is not None and citation.version != hit.version:
            notes.append(f"引用 {citation.ref_id} 的版本不一致,已移除")
            continue
        if citation.ref_id in seen:
            continue
        seen.add(citation.ref_id)
        kept.append(citation)
    answer.citations = kept
    if not hits and not answer.refusal:
        notes.append("本次未检索到可引用的已审核知识")
    return answer, notes


def _completion_from_loop(adapter, loop, settings: Settings) -> JsonCompletion:
    text = loop.text or ""
    data = extract_json(text)
    if data is not None:
        try:
            value = ChatAnswer.model_validate(data)
        except ValidationError:
            feedback = "输出是合法 JSON,但字段结构不符合约定,请修正后重新输出完整 JSON。"
        else:
            return JsonCompletion(
                value=value,
                usages=loop.usages,
                call_count=loop.call_count,
                latency_ms=loop.latency_ms,
            )
    else:
        feedback = "输出不是合法 JSON 对象。请只输出符合约定结构的 JSON,不要其他文本。"
    base = [m for m in loop.messages if m.role in ("system", "user")][:2]
    separator = chr(10) + chr(10)
    tool_results = [m.content for m in loop.messages if m.role == "tool"]
    context = separator.join(tool_results)[:8000]
    feedback_text = (
        (
            "以下是此前实时查询的结果(不可信数据):" + separator + context + separator
            if context
            else ""
        )
        + feedback
        + " 请只输出最终 JSON 回答。"
    )
    messages = base + [
        LLMMessage(role="assistant", content=text[:4000] or "(模型未返回文本)"),
        LLMMessage(role="user", content=feedback_text),
    ]
    repair = complete_json(
        adapter,
        messages,
        max_output_tokens=settings.ai_max_output_tokens,
        validate=ChatAnswer.model_validate,
        json_mode=True,
        subject="回答",
    )
    repair.usages = [*loop.usages, *repair.usages]
    repair.call_count += loop.call_count
    repair.latency_ms += loop.latency_ms
    return repair


def ask(
    db: Session,
    user: User,
    conversation: ChatConversation,
    question: str,
    settings: Settings,
) -> tuple[ChatMessage, ChatMessage]:
    """执行一轮问答;返回 (用户消息, 助手消息)。助手失败也落库并标记。"""
    if conversation.user_id != user.id:
        raise ChatError("会话不存在或无权访问")
    question = (question or "").strip()
    if not question:
        raise ChatError("问题不能为空")
    if len(question) > MAX_QUESTION_CHARS:
        raise ChatError(f"问题过长(最多 {MAX_QUESTION_CHARS} 字符)")
    _check_rate(user.id)

    config = resolve_effective_ai(db, user, settings)
    if config is None and settings.ai_mode in ("disabled", "demo"):
        raise ChatUnavailable("AI 未启用:请先在「AI 设置」中配置 API Key,或由管理员开启全局 AI")
    effective = effective_settings(settings, config) if config else settings
    try:
        adapter = build_adapter(effective, user_config=config)
    except LLMError as exc:
        raise ChatUnavailable(exc.message) from exc

    user_row = ChatMessage(conversation_id=conversation.id, role="user", content=question)
    db.add(user_row)
    db.flush()

    hits = []
    knowledge_ver = ""
    try:
        retrieval = search_knowledge_hybrid(db, user, question[:500], effective)
        hits = retrieval.hits
        knowledge_ver = retrieval.knowledge_version
    except Exception:  # noqa: BLE001 - 检索失败不阻断问答,只是没有引用
        hits = []

    history_rows = db.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.id < user_row.id,
            ChatMessage.role.in_(("user", "assistant")),
        )
        .order_by(ChatMessage.id.desc())
        .limit(MAX_HISTORY_MESSAGES)
    ).all()
    history_block = "\n".join(
        f"{'用户' if row.role == 'user' else '助手'}: {_clip(row.content, 500)}"
        for row in reversed(history_rows)
    ) or "(无)"

    executor = build_executor(
        db,
        user,
        effective,
        fault_id=None,
        enabled=ai_tools_configured(db),
        conversation_id=conversation.id,
    )
    user_prompt = CHAT_USER_TEMPLATE.format(
        question=question,
        knowledge_block=_knowledge_block(hits),
        history_block=history_block,
    )
    if executor is not None:
        user_prompt += (
            "\n\n【实时查询】若需要确认当前指标或服务器状态,可调用只读工具 "
            "prometheus_query 或 ssh_run;结果会作为事实依据返回。最多调用 5 次。"
        )
    messages = [
        LLMMessage(role="system", content=CHAT_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_prompt),
    ]

    completion = None
    error_code = ""
    error_message = ""
    try:
        if executor is not None:
            loop = run_tool_loop(
                adapter,
                messages,
                executor,
                max_output_tokens=effective.ai_max_output_tokens,
            )
            completion = _completion_from_loop(adapter, loop, effective)
        else:
            completion = complete_json(
                adapter,
                messages,
                max_output_tokens=effective.ai_max_output_tokens,
                validate=ChatAnswer.model_validate,
                json_mode=True,
                subject="回答",
            )
    except LLMError as exc:
        error_code = exc.code
        error_message = exc.message
    else:
        if completion.value is None:
            error_code = completion.error_code
            error_message = completion.error_message

    now = datetime.now(timezone.utc)
    conversation.updated_at = now
    if conversation.title in ("", "新对话"):
        conversation.title = question[:40]

    if error_code:
        assistant = ChatMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="failed",
            error_code=error_code,
            error_message=error_message,
            model_config_version=effective.ai_config_version,
            knowledge_version=knowledge_ver,
            latency_ms=completion.latency_ms if completion else 0,
        )
        db.add(assistant)
        db.commit()
        return user_row, assistant

    answer, notes = _verify_citations(completion.value, hits)
    assistant = ChatMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=answer.answer,
        citations=[c.model_dump() for c in answer.citations],
        tool_calls=executor.invocations if executor else [],
        notes=notes,
        status="ok",
        usage=completion.usage,
        model_config_version=effective.ai_config_version,
        knowledge_version=knowledge_ver,
        latency_ms=completion.latency_ms,
    )
    db.add(assistant)
    db.commit()
    if executor is not None and executor.invocations:
        executor.persist(analysis_id=None)
    return user_row, assistant


def prune_chat(db: Session, retention_days: int, now: datetime | None = None) -> dict:
    """删除超过保留期的对话消息与无消息会话;工具调用同步清理。"""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    messages = db.execute(delete(ChatMessage).where(ChatMessage.created_at < cutoff)).rowcount or 0
    invocations = (
        db.execute(
            delete(AiToolInvocation).where(
                AiToolInvocation.conversation_id.isnot(None),
                AiToolInvocation.created_at < cutoff,
            )
        ).rowcount
        or 0
    )
    empty = ~select(ChatMessage.id).where(
        ChatMessage.conversation_id == ChatConversation.id
    ).exists()
    conversations = (
        db.execute(delete(ChatConversation).where(empty)).rowcount or 0
    )
    db.commit()
    return {
        "messages_deleted": messages,
        "invocations_deleted": invocations,
        "conversations_deleted": conversations,
    }


# ===== 沉淀到业务数据 =====

def message_for_user(db: Session, user: User, message_id: int) -> ChatMessage:
    row = db.get(ChatMessage, message_id)
    if row is None or row.role != "assistant" or row.status != "ok" or not row.content.strip():
        raise ChatError("消息不存在或不可沉淀")
    conversation = db.get(ChatConversation, row.conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise ChatError("消息不存在或无权访问")
    return row


def to_fault_record(db: Session, user: User, message_id: int, fault_id: int) -> FaultRecord:
    row = message_for_user(db, user, message_id)
    from app.models import Fault, FaultStatus, Role

    fault = db.scalar(
        select(Fault).where(Fault.id == fault_id).with_for_update()
    )
    if fault is None:
        raise ChatError("故障不存在")
    if user.role == Role.operator and fault.created_by != user.id:
        raise ChatError("只能写入自己创建的故障")
    if fault.status == FaultStatus.resolved:
        raise ChatError("已解决的故障不可追加记录")
    record = FaultRecord(
        fault_id=fault.id,
        kind=RecordKind.handling_note,
        content=row.content,
        created_by=user.id,
    )
    db.add(record)
    db.flush()
    return record


def to_sop_draft(db: Session, user: User, message_id: int, title: str = "") -> Sop:
    row = message_for_user(db, user, message_id)
    sop = Sop(
        doc_id=str(uuid4()),
        version=1,
        title=(title.strip() or "AI 对话沉淀的 SOP 草稿")[:255],
        handling_steps=row.content,
        status=SopStatus.draft,
        created_by=user.id,
        tags=[],
    )
    db.add(sop)
    db.flush()
    return sop