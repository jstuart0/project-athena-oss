"""Add external API keys table for sports providers

Revision ID: 012
Revises: 011
Create Date: 2025-11-18
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '012'
down_revision = '011'
branch_labels = None
depends_on = None


def upgrade():
    """Create external_api_keys table and indexes."""
    op.create_table(
        'external_api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('service_name', sa.String(length=255), nullable=False),
        sa.Column('api_name', sa.String(length=255), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column('endpoint_url', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('last_used', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('service_name', name='uq_external_api_keys_service_name')
    )

    op.create_index('idx_external_api_keys_service_name', 'external_api_keys', ['service_name'], unique=False)
    op.create_index('idx_external_api_keys_enabled', 'external_api_keys', ['enabled'], unique=False)
    op.create_index('idx_external_api_keys_last_used', 'external_api_keys', ['last_used'], unique=False)

    print("✓ Created external_api_keys table")


def downgrade():
    """Drop external_api_keys table and indexes."""
    op.drop_index('idx_external_api_keys_last_used', table_name='external_api_keys')
    op.drop_index('idx_external_api_keys_enabled', table_name='external_api_keys')
    op.drop_index('idx_external_api_keys_service_name', table_name='external_api_keys')
    op.drop_table('external_api_keys')

    print("✓ Dropped external_api_keys table")

