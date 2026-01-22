"""Add end-to-end latency tracking with feature flags

Revision ID: 006_end_to_end_latency_tracking
Revises: 005_configurable_routing
Create Date: 2025-11-16

This migration adds:
1. Features table for feature flag management
2. Component latency tracking to llm_performance_metrics
3. Feature snapshot capability via JSONB
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    """Add features table and extend llm_performance_metrics."""

    # Create features table
    op.create_table(
        'features',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('avg_latency_ms', sa.Float(), nullable=True),
        sa.Column('hit_rate', sa.Float(), nullable=True),
        sa.Column('required', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )

    # Create indexes for features table
    op.create_index('idx_features_enabled', 'features', ['enabled'])
    op.create_index('idx_features_category', 'features', ['category'])
    op.create_index(op.f('ix_features_name'), 'features', ['name'], unique=True)

    # Extend llm_performance_metrics table with component latencies
    op.add_column('llm_performance_metrics',
                  sa.Column('gateway_latency_ms', sa.Float(), nullable=True))
    op.add_column('llm_performance_metrics',
                  sa.Column('intent_classification_latency_ms', sa.Float(), nullable=True))
    op.add_column('llm_performance_metrics',
                  sa.Column('rag_lookup_latency_ms', sa.Float(), nullable=True))
    op.add_column('llm_performance_metrics',
                  sa.Column('llm_inference_latency_ms', sa.Float(), nullable=True))
    op.add_column('llm_performance_metrics',
                  sa.Column('response_assembly_latency_ms', sa.Float(), nullable=True))
    op.add_column('llm_performance_metrics',
                  sa.Column('cache_lookup_latency_ms', sa.Float(), nullable=True))
    op.add_column('llm_performance_metrics',
                  sa.Column('features_enabled', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    # Seed initial feature data
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority) VALUES
        ('intent_classification', 'Intent Classification', 'Classify user query intent', 'processing', true, true, 1),
        ('multi_intent_detection', 'Multi-Intent Detection', 'Detect and parse multiple intents in a single query', 'processing', true, false, 2),
        ('conversation_context', 'Conversation Context', 'Preserve context between queries in a conversation', 'processing', true, false, 3),
        ('rag_weather', 'Weather RAG', 'Retrieve live weather data from National Weather Service', 'rag', true, false, 10),
        ('rag_sports', 'Sports RAG', 'Retrieve sports scores and schedules from ESPN', 'rag', true, false, 11),
        ('rag_airports', 'Airports RAG', 'Retrieve airport and flight information', 'rag', true, false, 12),
        ('redis_caching', 'Redis Caching', 'Cache responses in Redis for faster retrieval', 'optimization', true, false, 20),
        ('mlx_backend', 'MLX Backend', 'Use MLX-optimized backend for LLM inference', 'optimization', true, false, 21),
        ('response_streaming', 'Response Streaming', 'Stream LLM responses in real-time', 'optimization', true, false, 22),
        ('home_assistant', 'Home Assistant', 'Integrate with Home Assistant for device control', 'integration', true, false, 30),
        ('clarification_questions', 'Clarification Questions', 'Ask clarifying questions for ambiguous queries', 'integration', true, false, 31)
    """)


def downgrade():
    """Revert changes."""

    # Remove columns from llm_performance_metrics
    op.drop_column('llm_performance_metrics', 'features_enabled')
    op.drop_column('llm_performance_metrics', 'cache_lookup_latency_ms')
    op.drop_column('llm_performance_metrics', 'response_assembly_latency_ms')
    op.drop_column('llm_performance_metrics', 'llm_inference_latency_ms')
    op.drop_column('llm_performance_metrics', 'rag_lookup_latency_ms')
    op.drop_column('llm_performance_metrics', 'intent_classification_latency_ms')
    op.drop_column('llm_performance_metrics', 'gateway_latency_ms')

    # Drop indexes
    op.drop_index(op.f('ix_features_name'), table_name='features')
    op.drop_index('idx_features_category', table_name='features')
    op.drop_index('idx_features_enabled', table_name='features')

    # Drop features table
    op.drop_table('features')
