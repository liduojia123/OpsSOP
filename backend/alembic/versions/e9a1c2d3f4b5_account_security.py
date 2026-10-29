"""Persist revoked sessions and shared login rate limits.

Revision ID: e9a1c2d3f4b5
Revises: d4c5b6a7c8e9
"""
from alembic import op
import sqlalchemy as sa

revision = "e9a1c2d3f4b5"
down_revision = "d4c5b6a7c8e9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "revoked_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_sessions_expires_at", "revoked_sessions", ["expires_at"])
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("account_key", "source_key", "created_at"):
        op.create_index(f"ix_login_attempts_{column}", "login_attempts", [column])


def downgrade():
    op.drop_table("login_attempts")
    op.drop_table("revoked_sessions")
