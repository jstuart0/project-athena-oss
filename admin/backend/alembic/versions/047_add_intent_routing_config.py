"""Add intent routing configuration table.

Revision ID: 047_add_intent_routing_config
Revises: 046_add_weather_provider_feature
Create Date: 2026-01-12

Enables per-intent routing strategy configuration:
- cascading: Direct RAG first, fallback to tool calling on failure (default)
- always_tool_calling: Skip direct RAG, always use LLM tool selection
- direct_only: Never fall back to tool calling
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '047_add_intent_routing_config'
down_revision = '046_add_weather_provider_feature'
branch_labels = None
depends_on = None


def upgrade():
    """Create intent_routing_config table and seed with defaults."""
    op.create_table(
        'intent_routing_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('intent_name', sa.String(50), unique=True, nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('routing_strategy', sa.String(20), nullable=False, server_default='cascading'),
        sa.Column('enabled', sa.Boolean(), server_default='true'),
        sa.Column('priority', sa.Integer(), server_default='10'),
        sa.Column('config', JSONB, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Create index on intent_name for fast lookups
    op.create_index('ix_intent_routing_config_intent_name', 'intent_routing_config', ['intent_name'])

    # Seed with all known intents, defaulting to cascading
    intents = [
        ('weather', 'Weather', 10),
        ('dining', 'Dining & Restaurants', 10),
        ('sports', 'Sports Scores', 10),
        ('stocks', 'Stock Prices', 10),
        ('news', 'News', 10),
        ('events', 'Events', 10),
        ('flights', 'Flight Status', 10),
        ('airports', 'Airport Info', 10),
        ('recipes', 'Recipes', 10),
        ('streaming', 'Streaming Services', 10),
        ('websearch', 'Web Search', 10),
        ('directions', 'Directions', 10),
    ]

    for intent_name, display_name, priority in intents:
        op.execute(f"""
            INSERT INTO intent_routing_config (intent_name, display_name, routing_strategy, enabled, priority)
            VALUES ('{intent_name}', '{display_name}', 'cascading', true, {priority})
            ON CONFLICT (intent_name) DO NOTHING
        """)


def downgrade():
    """Remove intent_routing_config table."""
    op.drop_index('ix_intent_routing_config_intent_name', table_name='intent_routing_config')
    op.drop_table('intent_routing_config')
