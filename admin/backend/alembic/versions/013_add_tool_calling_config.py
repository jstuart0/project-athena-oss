"""Add tool calling configuration tables

Revision ID: 013
Revises: 012
Create Date: 2025-11-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic
revision = '013'
down_revision = '012'
branch_labels = None
depends_on = None


def upgrade():
    """Create tool calling configuration tables."""

    # 1. tool_registry table
    op.create_table(
        'tool_registry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tool_name', sa.String(length=100), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),  # 'rag', 'control', 'info'
        sa.Column('function_schema', JSONB, nullable=False),  # OpenAI function calling schema
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('guest_mode_allowed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('requires_auth', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=True),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('service_url', sa.String(length=500), nullable=True),  # RAG service endpoint
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tool_name', name='uq_tool_registry_name')
    )

    op.create_index('idx_tool_registry_enabled', 'tool_registry', ['enabled'], unique=False)
    op.create_index('idx_tool_registry_category', 'tool_registry', ['category'], unique=False)
    op.create_index('idx_tool_registry_guest_mode', 'tool_registry', ['guest_mode_allowed'], unique=False)

    # 2. tool_calling_settings table (singleton table - only 1 row)
    op.create_table(
        'tool_calling_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('llm_model', sa.String(length=100), nullable=False, server_default='gpt-4o-mini'),
        sa.Column('llm_backend', sa.String(length=50), nullable=False, server_default='openai'),  # 'openai' or 'ollama'
        sa.Column('max_parallel_tools', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('tool_call_timeout_seconds', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('temperature', sa.Float(), nullable=False, server_default='0.1'),
        sa.Column('max_tokens', sa.Integer(), nullable=False, server_default='500'),
        sa.Column('fallback_to_direct_llm', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('cache_results', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('cache_ttl_seconds', sa.Integer(), nullable=False, server_default='300'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 3. tool_calling_triggers table
    op.create_table(
        'tool_calling_triggers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('trigger_name', sa.String(length=100), nullable=False),
        sa.Column('trigger_type', sa.String(length=50), nullable=False),  # 'confidence', 'intent', 'keywords', 'validation', 'empty_rag'
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('config', JSONB, nullable=False),  # Trigger-specific configuration
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('trigger_name', name='uq_trigger_name')
    )

    op.create_index('idx_tool_calling_triggers_enabled', 'tool_calling_triggers', ['enabled'], unique=False)
    op.create_index('idx_tool_calling_triggers_type', 'tool_calling_triggers', ['trigger_type'], unique=False)

    # 4. tool_usage_metrics table
    op.create_table(
        'tool_usage_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('tool_name', sa.String(length=100), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('trigger_reason', sa.String(length=100), nullable=True),  # Which trigger fired
        sa.Column('intent', sa.String(length=100), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('guest_mode', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('request_id', sa.String(length=100), nullable=True),
        sa.Column('session_id', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_index('idx_tool_usage_timestamp', 'tool_usage_metrics', ['timestamp'], unique=False)
    op.create_index('idx_tool_usage_tool_name', 'tool_usage_metrics', ['tool_name'], unique=False)
    op.create_index('idx_tool_usage_success', 'tool_usage_metrics', ['success'], unique=False)
    op.create_index('idx_tool_usage_composite', 'tool_usage_metrics', ['timestamp', 'tool_name', 'success'], unique=False)

    print("✓ Created tool_registry table")
    print("✓ Created tool_calling_settings table")
    print("✓ Created tool_calling_triggers table")
    print("✓ Created tool_usage_metrics table")


def downgrade():
    """Drop tool calling configuration tables."""

    # Drop indexes first
    op.drop_index('idx_tool_usage_composite', table_name='tool_usage_metrics')
    op.drop_index('idx_tool_usage_success', table_name='tool_usage_metrics')
    op.drop_index('idx_tool_usage_tool_name', table_name='tool_usage_metrics')
    op.drop_index('idx_tool_usage_timestamp', table_name='tool_usage_metrics')

    op.drop_index('idx_tool_calling_triggers_type', table_name='tool_calling_triggers')
    op.drop_index('idx_tool_calling_triggers_enabled', table_name='tool_calling_triggers')

    op.drop_index('idx_tool_registry_guest_mode', table_name='tool_registry')
    op.drop_index('idx_tool_registry_category', table_name='tool_registry')
    op.drop_index('idx_tool_registry_enabled', table_name='tool_registry')

    # Drop tables
    op.drop_table('tool_usage_metrics')
    op.drop_table('tool_calling_triggers')
    op.drop_table('tool_calling_settings')
    op.drop_table('tool_registry')

    print("✓ Dropped tool calling tables")
