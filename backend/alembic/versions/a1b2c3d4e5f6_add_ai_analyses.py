"""add ai_analyses table (Phase 2)

Revision ID: a1b2c3d4e5f6
Revises: c53e6658b32f
Create Date: 2026-10-14 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c53e6658b32f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_analyses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('fault_id', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        # trigger_type 而非 trigger:避免与 SQL 关键字冲突
        sa.Column('trigger_type', sa.String(length=16), nullable=False),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column('input_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('model_config_version', sa.String(length=16), nullable=False),
        sa.Column('knowledge_version', sa.String(length=16), nullable=False),
        sa.Column('is_demo', sa.Boolean(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('success', 'incomplete', 'failed', name='ai_analysis_status'),
            nullable=False,
        ),
        sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('incomplete_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('error_code', sa.String(length=32), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('call_count', sa.Integer(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['fault_id'], ['faults.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ai_analyses_fault_id'), 'ai_analyses', ['fault_id'], unique=False)
    op.create_index(op.f('ix_ai_analyses_input_hash'), 'ai_analyses', ['input_hash'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ai_analyses_input_hash'), table_name='ai_analyses')
    op.drop_index(op.f('ix_ai_analyses_fault_id'), table_name='ai_analyses')
    op.drop_table('ai_analyses')
    # 删除依赖表后清理本迁移创建的枚举,避免再次 upgrade 时类型残留
    bind = op.get_bind()
    sa.Enum(name='ai_analysis_status').drop(bind=bind, checkfirst=True)
