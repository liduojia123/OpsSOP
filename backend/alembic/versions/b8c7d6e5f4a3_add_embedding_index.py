"""add embedding index tables + pgvector extension (Phase 3 batch 2)

Revision ID: b8c7d6e5f4a3
Revises: f7e8d9c0b1a2
Create Date: 2026-10-15 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from pgvector.sqlalchemy import Vector

revision: str = 'b8c7d6e5f4a3'
down_revision: Union[str, None] = 'f7e8d9c0b1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector 扩展:镜像需含扩展(如 pgvector/pgvector:pg16)且用户有 CREATE EXTENSION 权限。
    # 失败时给出明确原因;不得以普通 JSON 数组冒充 pgvector 已接入。
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        raise RuntimeError(
            "无法创建 pgvector 扩展:请确认数据库镜像包含 pgvector(如 pgvector/pgvector:pg16),"
            "且当前数据库用户有 CREATE EXTENSION 权限;本项目不以普通 JSON 数组替代向量列"
        ) from exc

    op.create_table(
        'embedding_index_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('active_generation', sa.Integer(), nullable=False),
        sa.Column('active_fingerprint', sa.String(length=16), nullable=False),
        sa.Column('active_model', sa.String(length=128), nullable=False),
        sa.Column('active_dims', sa.Integer(), nullable=False),
        sa.Column('active_text_version', sa.String(length=16), nullable=False),
        sa.Column('unit_count', sa.Integer(), nullable=False),
        sa.Column('built_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('building_generation', sa.Integer(), nullable=False),
        sa.Column('build_attempt', sa.Integer(), nullable=False),
        sa.Column('build_fingerprint', sa.String(length=16), nullable=False),
        sa.Column('error_code', sa.String(length=32), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'embedding_index_units',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(length=8), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=True),
        sa.Column('unit_key', sa.String(length=32), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('text_excerpt', sa.Text(), nullable=False),
        sa.Column('config_fingerprint', sa.String(length=16), nullable=False),
        sa.Column('embedding_dims', sa.Integer(), nullable=False),
        # pgvector 向量列(不限定维度;实际维度记录在 embedding_dims)
        sa.Column('embedding', Vector(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'generation', 'source_type', 'source_id', 'unit_key',
            name='uq_emb_unit_gen_source',
        ),
    )
    op.create_index(op.f('ix_embedding_index_units_generation'), 'embedding_index_units', ['generation'], unique=False)
    op.create_index(op.f('ix_embedding_index_units_content_hash'), 'embedding_index_units', ['content_hash'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_embedding_index_units_content_hash'), table_name='embedding_index_units')
    op.drop_index(op.f('ix_embedding_index_units_generation'), table_name='embedding_index_units')
    op.drop_table('embedding_index_units')
    op.drop_table('embedding_index_state')
    # 扩展保留:其他对象可能共享;需要时由管理员显式 DROP EXTENSION
