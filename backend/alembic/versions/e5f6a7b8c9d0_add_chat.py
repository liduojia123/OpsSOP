"""新增 AI 对话表,并扩展工具调用记录

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-10-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_conversations_user_id", "chat_conversations", ["user_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("chat_conversations.id"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("notes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("error_code", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("usage", postgresql.JSONB()),
        sa.Column("model_config_version", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("knowledge_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])

    op.add_column("ai_tool_invocations", sa.Column("conversation_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_ai_tool_invocations_conversation_id", "ai_tool_invocations", ["conversation_id"]
    )
    op.create_foreign_key(
        "fk_ai_tool_invocations_conversation",
        "ai_tool_invocations",
        "chat_conversations",
        ["conversation_id"],
        ["id"],
    )
    op.alter_column("ai_tool_invocations", "fault_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.alter_column("ai_tool_invocations", "fault_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint("fk_ai_tool_invocations_conversation", "ai_tool_invocations", type_="foreignkey")
    op.drop_index("ix_ai_tool_invocations_conversation_id", table_name="ai_tool_invocations")
    op.drop_column("ai_tool_invocations", "conversation_id")
    op.drop_index("ix_chat_messages_conversation_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conversations_user_id", table_name="chat_conversations")
    op.drop_table("chat_conversations")
