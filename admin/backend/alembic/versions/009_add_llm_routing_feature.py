"""Add LLM-based routing feature flag

Revision ID: 009_add_llm_routing_feature
Revises: 008_add_source_to_metrics
Create Date: 2025-11-16

This migration adds a feature flag to control whether the Gateway uses
LLM-based intent classification or traditional keyword matching for routing
queries between the orchestrator and Ollama.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade():
    """Add llm_based_routing feature flag."""

    # Insert new feature flag
    op.execute("""
        INSERT INTO features (name, display_name, description, enabled, category, created_at, updated_at)
        VALUES (
            'llm_based_routing',
            'Use LLM for Intent Classification',
            'Use AI to intelligently classify query intent instead of keyword matching. More accurate but adds 50-200ms latency.',
            true,
            'routing',
            NOW(),
            NOW()
        )
    """)


def downgrade():
    """Remove llm_based_routing feature flag."""

    op.execute("DELETE FROM features WHERE name = 'llm_based_routing'")
