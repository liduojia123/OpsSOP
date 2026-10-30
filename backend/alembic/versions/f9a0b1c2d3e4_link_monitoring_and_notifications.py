"""监控与业务联动、站内通知

- monitored_servers.service_names:服务器承载的服务名,用于把故障关联到实时指标;
- monitoring_config:告警转故障开关/服务标签/负责人/上次同步时间;
- faults.source / alert_fingerprint:标识告警来源并去重;
- notifications:审核流转与告警提醒。

Revision ID: f9a0b1c2d3e4
Revises: e5f6a7b8c9d0
Create Date: 2026-10-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "monitored_servers",
        sa.Column("service_names", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "monitoring_config",
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "monitoring_config",
        sa.Column(
            "alert_service_label",
            sa.String(length=64),
            nullable=False,
            server_default="service",
        ),
    )
    op.add_column(
        "monitoring_config",
        sa.Column("alert_owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column(
        "monitoring_config",
        sa.Column("alert_last_sync_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "faults",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
    )
    op.add_column(
        "faults",
        sa.Column(
            "alert_fingerprint", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    op.create_index("ix_faults_alert_fingerprint", "faults", ["alert_fingerprint"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("link", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("entity_type", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_kind", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_faults_alert_fingerprint", table_name="faults")
    op.drop_column("faults", "alert_fingerprint")
    op.drop_column("faults", "source")
    op.drop_column("monitoring_config", "alert_last_sync_at")
    op.drop_column("monitoring_config", "alert_owner_id")
    op.drop_column("monitoring_config", "alert_service_label")
    op.drop_column("monitoring_config", "alerts_enabled")
    op.drop_column("monitored_servers", "service_names")