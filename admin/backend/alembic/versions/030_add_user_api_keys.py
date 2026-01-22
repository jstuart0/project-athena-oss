"""Add user API keys table for programmatic authentication.

Revision ID: 030_add_user_api_keys
Revises: 029_add_test_mode_fields
Create Date: 2025-12-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '030_add_user_api_keys'
down_revision = '029_add_test_mode_fields'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('key_prefix', sa.String(16), nullable=False),
        sa.Column('key_hash', sa.String(64), nullable=False),
        sa.Column('scopes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_reason', sa.Text(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_ip', sa.String(45), nullable=True),
        sa.Column('request_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('created_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key_prefix'),
        sa.UniqueConstraint('key_hash'),
    )

    op.create_index('idx_user_api_keys_user_id', 'user_api_keys', ['user_id'])
    op.create_index('idx_user_api_keys_key_prefix', 'user_api_keys', ['key_prefix'])
    op.create_index('idx_user_api_keys_revoked', 'user_api_keys', ['revoked'])
    op.create_index('idx_user_api_keys_expires_at', 'user_api_keys', ['expires_at'])
    op.create_index('idx_user_api_keys_last_used_at', 'user_api_keys', ['last_used_at'])


def downgrade() -> None:
    op.drop_table('user_api_keys')
