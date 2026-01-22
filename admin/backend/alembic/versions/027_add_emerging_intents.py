"""Add emerging intents and intent metrics tables.

Creates tables for intent discovery system:
- emerging_intents: Tracks novel/unknown intents discovered during classification
- intent_metrics: Records all intent classifications for analytics

Revision ID: 027
Revises: 026
Create Date: 2024-12-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = '027_add_emerging_intents'
down_revision = '026_add_directions_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create emerging_intents and intent_metrics tables."""

    # Create emerging_intents table for intent discovery
    op.create_table(
        'emerging_intents',
        sa.Column('id', sa.Integer(), primary_key=True),

        # Intent identification
        sa.Column('canonical_name', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(200)),
        sa.Column('description', sa.Text()),

        # Semantic clustering - store embedding as JSONB array
        # Using JSONB instead of pgvector for portability
        # Format: [0.123, 0.456, ...] - 384 dimensions for all-MiniLM-L6-v2
        sa.Column('embedding', JSONB),

        # Metrics
        sa.Column('occurrence_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.func.now()),

        # Sample data for analysis - stores up to 10 example queries
        sa.Column('sample_queries', JSONB, server_default='[]'),

        # LLM suggestions
        sa.Column('suggested_category', sa.String(50)),  # utility, commerce, health, etc.
        sa.Column('suggested_api_sources', JSONB),  # Potential APIs to power this

        # Admin workflow
        sa.Column('status', sa.String(20), nullable=False, server_default='discovered'),
        sa.Column('reviewed_at', sa.DateTime(timezone=True)),
        sa.Column('reviewed_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('promoted_to_intent', sa.String(50)),  # If promoted, which IntentCategory
        sa.Column('rejection_reason', sa.Text()),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Create indexes for emerging_intents
    op.create_index('idx_emerging_intents_canonical_name', 'emerging_intents', ['canonical_name'])
    op.create_index('idx_emerging_intents_status', 'emerging_intents', ['status'])
    op.create_index('idx_emerging_intents_count', 'emerging_intents', ['occurrence_count'])
    op.create_index('idx_emerging_intents_category', 'emerging_intents', ['suggested_category'])

    # Create intent_metrics table for analytics
    op.create_table(
        'intent_metrics',
        sa.Column('id', sa.Integer(), primary_key=True),

        # Classification result
        sa.Column('intent', sa.String(50), nullable=False),  # The classified intent
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('complexity', sa.String(20)),  # simple, complex, super_complex

        # Novel intent tracking
        sa.Column('is_novel', sa.Boolean(), server_default='false'),
        sa.Column('emerging_intent_id', sa.Integer(), sa.ForeignKey('emerging_intents.id')),

        # Query info
        sa.Column('raw_query', sa.Text()),  # The actual query text
        sa.Column('query_hash', sa.String(64)),  # MD5 hash for deduplication analysis

        # Context
        sa.Column('session_id', sa.String(100)),
        sa.Column('mode', sa.String(20)),  # owner, guest
        sa.Column('room', sa.String(50)),

        # Request metadata
        sa.Column('request_id', sa.String(50)),
        sa.Column('processing_time_ms', sa.Integer()),

        # Timestamp
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Create indexes for intent_metrics
    op.create_index('idx_intent_metrics_intent', 'intent_metrics', ['intent'])
    op.create_index('idx_intent_metrics_created', 'intent_metrics', ['created_at'])
    op.create_index('idx_intent_metrics_is_novel', 'intent_metrics', ['is_novel'])
    op.create_index('idx_intent_metrics_emerging_id', 'intent_metrics', ['emerging_intent_id'])
    op.create_index('idx_intent_metrics_session', 'intent_metrics', ['session_id'])

    # Create intent discovery settings
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority) VALUES
        ('intent_discovery', 'Intent Discovery', 'Automatically discover and track novel user intents for service development insights', 'analytics', true, false, 200)
        ON CONFLICT (name) DO NOTHING
    """)


def downgrade() -> None:
    """Remove emerging_intents and intent_metrics tables."""
    op.execute("DELETE FROM features WHERE name = 'intent_discovery'")

    # Drop intent_metrics indexes and table
    op.drop_index('idx_intent_metrics_session', table_name='intent_metrics')
    op.drop_index('idx_intent_metrics_emerging_id', table_name='intent_metrics')
    op.drop_index('idx_intent_metrics_is_novel', table_name='intent_metrics')
    op.drop_index('idx_intent_metrics_created', table_name='intent_metrics')
    op.drop_index('idx_intent_metrics_intent', table_name='intent_metrics')
    op.drop_table('intent_metrics')

    # Drop emerging_intents indexes and table
    op.drop_index('idx_emerging_intents_category', table_name='emerging_intents')
    op.drop_index('idx_emerging_intents_count', table_name='emerging_intents')
    op.drop_index('idx_emerging_intents_status', table_name='emerging_intents')
    op.drop_index('idx_emerging_intents_canonical_name', table_name='emerging_intents')
    op.drop_table('emerging_intents')
