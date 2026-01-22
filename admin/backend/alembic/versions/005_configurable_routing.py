"""Add configurable routing tables for intent classification and provider routing

Revision ID: 005
Revises: 004
Create Date: 2025-11-16 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create configurable routing tables."""

    # Create intent_patterns table
    op.create_table(
        'intent_patterns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('intent_category', sa.String(length=50), nullable=False),
        sa.Column('pattern_type', sa.String(length=50), nullable=False),
        sa.Column('keyword', sa.String(length=100), nullable=False),
        sa.Column('confidence_weight', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('intent_category', 'pattern_type', 'keyword', name='uq_intent_pattern_keyword')
    )

    # Create indexes for intent_patterns
    op.create_index('idx_intent_patterns_category', 'intent_patterns', ['intent_category'], unique=False)
    op.create_index('idx_intent_patterns_enabled', 'intent_patterns', ['enabled'], unique=False)
    op.create_index('idx_intent_patterns_keyword', 'intent_patterns', ['keyword'], unique=False)

    # Create intent_routing table
    op.create_table(
        'intent_routing',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('intent_category', sa.String(length=50), nullable=False),
        sa.Column('use_rag', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('rag_service_url', sa.String(length=255), nullable=True),
        sa.Column('use_web_search', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('use_llm', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('intent_category', name='uq_intent_routing_category')
    )

    # Create indexes for intent_routing
    op.create_index('idx_intent_routing_category', 'intent_routing', ['intent_category'], unique=False)
    op.create_index('idx_intent_routing_enabled', 'intent_routing', ['enabled'], unique=False)
    op.create_index('idx_intent_routing_priority', 'intent_routing', ['priority'], unique=False)

    # Create provider_routing table
    op.create_table(
        'provider_routing',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('intent_category', sa.String(length=50), nullable=False),
        sa.Column('provider_name', sa.String(length=50), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('intent_category', 'provider_name', name='uq_provider_routing_category_provider')
    )

    # Create indexes for provider_routing
    op.create_index('idx_provider_routing_category', 'provider_routing', ['intent_category'], unique=False)
    op.create_index('idx_provider_routing_provider', 'provider_routing', ['provider_name'], unique=False)
    op.create_index('idx_provider_routing_enabled', 'provider_routing', ['enabled'], unique=False)
    op.create_index('idx_provider_routing_priority', 'provider_routing', ['priority'], unique=False)


def downgrade() -> None:
    """Drop configurable routing tables and all indexes."""

    # Drop provider_routing table
    op.drop_index('idx_provider_routing_priority', table_name='provider_routing')
    op.drop_index('idx_provider_routing_enabled', table_name='provider_routing')
    op.drop_index('idx_provider_routing_provider', table_name='provider_routing')
    op.drop_index('idx_provider_routing_category', table_name='provider_routing')
    op.drop_table('provider_routing')

    # Drop intent_routing table
    op.drop_index('idx_intent_routing_priority', table_name='intent_routing')
    op.drop_index('idx_intent_routing_enabled', table_name='intent_routing')
    op.drop_index('idx_intent_routing_category', table_name='intent_routing')
    op.drop_table('intent_routing')

    # Drop intent_patterns table
    op.drop_index('idx_intent_patterns_keyword', table_name='intent_patterns')
    op.drop_index('idx_intent_patterns_enabled', table_name='intent_patterns')
    op.drop_index('idx_intent_patterns_category', table_name='intent_patterns')
    op.drop_table('intent_patterns')
