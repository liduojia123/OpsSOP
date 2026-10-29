"""新增用户自填模型厂商配置表

Revision ID: b1c2d3e4f5a6
Revises: e9a1c2d3f4b5
Create Date: 2026-10-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "e9a1c2d3f4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_ai_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_ai_key"),
    )
    op.create_index("ix_user_ai_keys_user_id", "user_ai_keys", ["user_id"])
    op.create_index("ix_user_ai_keys_provider", "user_ai_keys", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_user_ai_keys_provider", table_name="user_ai_keys")
    op.drop_index("ix_user_ai_keys_user_id", table_name="user_ai_keys")
    op.drop_table("user_ai_keys")
