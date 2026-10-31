"""对话消息记录回答所用模型:厂商与模型名

Revision ID: h2b3c4d5e6f7
Revises: g1a2b3c4d5e6
Create Date: 2026-10-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "h2b3c4d5e6f7"
down_revision: Union[str, None] = "g1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("model_provider", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "chat_messages",
        sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "model_name")
    op.drop_column("chat_messages", "model_provider")
