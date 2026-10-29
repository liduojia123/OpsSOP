"""新增监控、服务器、命令模板、面板与 AI 工具调用表

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monitoring_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prometheus_url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("ai_tools_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "monitored_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("private_key_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("host_key_fingerprint", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("prometheus_instance", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("host", "port", "username", name="uq_server_target"),
    )
    op.create_table(
        "ssh_command_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("param_schema", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ssh_command_templates_key", "ssh_command_templates", ["key"], unique=True)
    op.create_table(
        "dashboard_panels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("promql", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="line"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "ai_tool_invocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("analysis_id", sa.Integer(), sa.ForeignKey("ai_analyses.id")),
        sa.Column("fault_id", sa.Integer(), sa.ForeignKey("faults.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("result_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_tool_invocations_analysis_id", "ai_tool_invocations", ["analysis_id"])
    op.create_index("ix_ai_tool_invocations_fault_id", "ai_tool_invocations", ["fault_id"])
    op.create_index("ix_ai_tool_invocations_user_id", "ai_tool_invocations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_tool_invocations_user_id", table_name="ai_tool_invocations")
    op.drop_index("ix_ai_tool_invocations_fault_id", table_name="ai_tool_invocations")
    op.drop_index("ix_ai_tool_invocations_analysis_id", table_name="ai_tool_invocations")
    op.drop_table("ai_tool_invocations")
    op.drop_table("dashboard_panels")
    op.drop_index("ix_ssh_command_templates_key", table_name="ssh_command_templates")
    op.drop_table("ssh_command_templates")
    op.drop_table("monitored_servers")
    op.drop_table("monitoring_config")