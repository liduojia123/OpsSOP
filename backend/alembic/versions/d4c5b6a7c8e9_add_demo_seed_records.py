"""add demo seed records (Phase 4 batch 1)

Revision ID: d4c5b6a7c8e9
Revises: b8c7d6e5f4a3
Create Date: 2026-10-21 10:00:00.000000

显式演示内容 CLI 的稳定标识映射表:
- (scenario_key, entity_role) 唯一定位一个演示实体,不依赖可编辑标题;
- 重复执行 seed 时据此跳过既有实体,不覆盖人工修改。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4c5b6a7c8e9"
down_revision: Union[str, None] = "b8c7d6e5f4a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "demo_seed_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scenario_key", sa.String(length=64), nullable=False),
        sa.Column("entity_role", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("seed_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scenario_key", "entity_role", name="uq_demo_seed_scenario_role"
        ),
    )
    op.create_index(
        op.f("ix_demo_seed_records_scenario_key"),
        "demo_seed_records",
        ["scenario_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_demo_seed_records_scenario_key"), table_name="demo_seed_records"
    )
    op.drop_table("demo_seed_records")
