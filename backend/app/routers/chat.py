"""AI 运维对话接口。

- 会话与消息按用户隔离;管理员可查看全部会话(90 天留存)。
- 提问只触发只读检索与只读工具;不执行任何变更。
- 助手回答可经人工确认沉淀为故障记录或 SOP 草稿。
- 流式接口使用 SSE:回答文本边生成边推送,消息仍在服务端完整落库。
"""

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.chat_service import (
    ChatError,
    ChatRateLimited,
    ChatUnavailable,
    ask,
    ask_stream,
    count_messages,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_latest_messages,
    get_messages,
    get_messages_before,
    has_older_messages,
    list_conversations,
    message_for_user,
    to_fault_record,
    to_sop_draft,
)
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, record_audit, require_operator
from app.models import ChatConversation, ChatMessage, User
from app.schemas import (
    ChatAskRequest,
    ChatAskResponse,
    ChatConversationDetailOut,
    ChatConversationOut,
    ChatCreateRequest,
    ChatMessageOut,
    ChatMessagesPageOut,
    ChatSedimentFaultRequest,
    ChatSedimentSopRequest,
    FaultRecordOut,
    SopOut,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/conversations", response_model=ChatConversationOut, status_code=status.HTTP_201_CREATED)
def create(
    body: ChatCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatConversationOut:
    row = create_conversation(db, user, body.title)
    return ChatConversationOut.model_validate(row)


@router.get("/conversations", response_model=list[ChatConversationOut])
def list_mine(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChatConversationOut]:
    return [ChatConversationOut.model_validate(row) for row in list_conversations(db, user)]


@router.get("/conversations/{conversation_id}", response_model=ChatConversationDetailOut)
def detail(
    conversation_id: int,
    limit: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatConversationDetailOut:
    """会话详情;limit>0 时只返回最新 limit 条(升序)并附带总数与是否有更早。"""
    try:
        conversation = get_conversation(db, user, conversation_id)
    except ChatError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc
    total = count_messages(db, conversation)
    if limit > 0:
        rows = get_latest_messages(db, conversation, min(limit, 200))
    else:
        rows = get_messages(db, conversation)
    return ChatConversationDetailOut(
        conversation=ChatConversationOut.model_validate(conversation),
        messages=[ChatMessageOut.model_validate(row) for row in rows],
        total_messages=total,
        has_more=len(rows) < total,
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ChatMessagesPageOut,
)
def messages_page(
    conversation_id: int,
    before_id: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatMessagesPageOut:
    """「加载更早消息」:返回 before_id 之前最新的 limit 条(升序);before_id<=0 时从最新开始。"""
    try:
        conversation = get_conversation(db, user, conversation_id)
    except ChatError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc
    limit = max(1, min(limit, 200))
    if before_id > 0:
        rows = get_messages_before(db, conversation, before_id, limit)
    else:
        rows = get_latest_messages(db, conversation, limit)
    return ChatMessagesPageOut(
        messages=[ChatMessageOut.model_validate(row) for row in rows],
        total_messages=count_messages(db, conversation),
        has_more=has_older_messages(db, conversation, rows[0].id) if rows else False,
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def remove(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    try:
        delete_conversation(db, user, conversation_id)
    except ChatError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ChatAskResponse,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    conversation_id: int,
    body: ChatAskRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatAskResponse:
    try:
        conversation = get_conversation(db, user, conversation_id)
        user_row, assistant_row = ask(db, user, conversation, body.content, get_settings())
    except ChatRateLimited as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.message) from exc
    except ChatUnavailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    except ChatError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    record_audit(
        db,
        user,
        "chat.message",
        "chat",
        str(conversation.id),
        {
            "status": assistant_row.status,
            "tools": len(assistant_row.tool_calls or []),
            "citations": len(assistant_row.citations or []),
        },
    )
    db.commit()
    return ChatAskResponse(
        user_message=ChatMessageOut.model_validate(user_row),
        assistant_message=ChatMessageOut.model_validate(assistant_row),
    )


@router.post("/conversations/{conversation_id}/messages/stream")
def send_message_stream(
    conversation_id: int,
    body: ChatAskRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """流式回答(SSE)。

    前置错误(会话不属于当前用户)仍返回 HTTP 404;问答过程中的限流、AI 未启用
    等以 error 事件推送,由前端在对话内提示。
    """
    try:
        get_conversation(db, user, conversation_id)
    except ChatError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc

    def _sse(events: Iterator[dict]) -> Iterator[str]:
        for event in events:
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        _sse(ask_stream(conversation_id, user.id, body.content, get_settings())),
        media_type="text/event-stream",
        headers={
            # 禁止中间层缓存/缓冲,保证逐段到达前端
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/messages/{message_id}/to-fault-record", response_model=FaultRecordOut)
def sediment_to_fault(
    message_id: int,
    body: ChatSedimentFaultRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> FaultRecordOut:
    try:
        message_for_user(db, user, message_id)
        record = to_fault_record(db, user, message_id, body.fault_id)
    except ChatError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    record_audit(db, user, "chat.to_fault_record", "fault", record.fault_id, {"message_id": message_id})
    db.commit()
    return FaultRecordOut.model_validate(record)


@router.post("/messages/{message_id}/to-sop-draft", response_model=SopOut)
def sediment_to_sop(
    message_id: int,
    body: ChatSedimentSopRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> SopOut:
    try:
        message_for_user(db, user, message_id)
        sop = to_sop_draft(db, user, message_id, body.title)
    except ChatError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    record_audit(db, user, "chat.to_sop_draft", "sop", sop.id, {"message_id": message_id})
    db.commit()
    return SopOut.model_validate(sop)