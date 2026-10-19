"""add sop_ai_generation_runs table (Phase 3 batch 1)

Revision ID: f7e8d9c0b1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f7e8d9c0b1a2'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sop_ai_generation_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('request_token', sa.String(length=64), nullable=False),
        # 执行领取计数:结果提交按 attempt 条件更新,防被接管后的旧执行者覆盖
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column('input_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('model_config_version', sa.String(length=16), nullable=False),
        sa.Column('is_demo', sa.Boolean(), nullable=False),
        # status 用字符串列(常量见 app.models.SopGenStatus),不引入 PG 枚举残留
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('sop_id', sa.Integer(), nullable=True),
        sa.Column('missing_information', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('incomplete_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('field_sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('error_code', sa.String(length=32), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('call_count', sa.Integer(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['sop_id'], ['sops.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_sop_ai_generation_runs_case_id'), 'sop_ai_generation_runs', ['case_id'], unique=False)
    op.create_index(op.f('ix_sop_ai_generation_runs_input_hash'), 'sop_ai_generation_runs', ['input_hash'], unique=False)
    op.create_index(op.f('ix_sop_ai_generation_runs_request_token'), 'sop_ai_generation_runs', ['request_token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_sop_ai_generation_runs_request_token'), table_name='sop_ai_generation_runs')
    op.drop_index(op.f('ix_sop_ai_generation_runs_input_hash'), table_name='sop_ai_generation_runs')
    op.drop_index(op.f('ix_sop_ai_generation_runs_case_id'), table_name='sop_ai_generation_runs')
    op.drop_table('sop_ai_generation_runs')
