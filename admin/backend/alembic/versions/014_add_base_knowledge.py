"""Add base knowledge system

Revision ID: 014
Revises: 013
Create Date: 2025-11-24 02:35:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = '014'
down_revision = '013'
branch_labels = None
depends_on = None


def upgrade():
    """Add base_knowledge table for context-aware knowledge management."""

    op.create_table(
        'base_knowledge',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(50), nullable=False, index=True),
        sa.Column('key', sa.String(100), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('applies_to', sa.String(20), nullable=False, server_default='both'),  # 'guest', 'owner', 'both'
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('extra_metadata', JSONB, nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), onupdate=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category', 'key', 'applies_to', name='uix_category_key_applies')
    )

    # Create index for quick lookups
    op.create_index('ix_base_knowledge_applies_to', 'base_knowledge', ['applies_to'])
    op.create_index('ix_base_knowledge_enabled', 'base_knowledge', ['enabled'])


def downgrade():
    """Remove base_knowledge table."""
    op.drop_index('ix_base_knowledge_enabled')
    op.drop_index('ix_base_knowledge_applies_to')
    op.drop_table('base_knowledge')
