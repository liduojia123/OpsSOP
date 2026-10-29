"""管理员查看 AI 对话(90 天留存范围)。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_admin
from app.models import ChatConversation, ChatMessage, User
from app.schemas import (
    AdminChatConversationOut,
    ChatConversationDetailOut,
    ChatConversationOut,
    ChatMessageOut,
)

router = APIRouter(prefix="/api/admin/chat", tags=["chat-admin"])


@router.get("/conversations", response_model=list[AdminChatConversationOut])
def list_all(
    user_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[AdminChatConversationOut]:
    count_sq = (
        select(func.count(ChatMessage.id))
        .where(ChatMessage.conversation_id == ChatConversation.id)
        .correlate(ChatConversation)
        .scalar_subquery()
    )
    stmt = (
        select(ChatConversation, User.username, count_sq.label("message_count"))
        .join(User, User.id == ChatConversation.user_id)
        .order_by(ChatConversation.updated_at.desc())
        .limit(limit)
    )
    if user_id is not None:
        stmt = stmt.where(ChatConversation.user_id == user_id)
    rows = db.execute(stmt).all()
    return [
        AdminChatConversationOut(
            **ChatConversationOut.model_validate(conversation).model_dump(),
            username=username,
            message_count=int(message_count or 0),
        )
        for conversation, username, message_count in rows
    ]


@router.get("/conversations/{conversation_id}", response_model=ChatConversationDetailOut)
def detail(
    conversation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> ChatConversationDetailOut:
    conversation = db.get(ChatConversation, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.id)
    ).all()
    return ChatConversationDetailOut(
        conversation=ChatConversationOut.model_validate(conversation),
        messages=[ChatMessageOut.model_validate(row) for row in messages],
    )