"""Add Orchestrator LLM intent classification feature flag

Revision ID: 011
Revises: 010
Create Date: 2025-11-17

This migration adds a feature flag to control whether the Orchestrator uses
LLM-based intent classification (phi3:mini) or traditional pattern matching
for categorizing queries into WEATHER, SPORTS, AIRPORTS, CONTROL, etc.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade():
    """Add enable_llm_intent_classification feature flag."""

    # Insert new feature flag
    op.execute("""
        INSERT INTO features (name, display_name, description, enabled, category, created_at, updated_at)
        VALUES (
            'enable_llm_intent_classification',
            'LLM Intent Classification',
            'Use LLM (phi3:mini) for intent classification in Orchestrator instead of pattern matching. Adds 50-200ms latency but improves accuracy for ambiguous queries.',
            true,
            'llm',
            NOW(),
            NOW()
        )
    """)

    print("✓ Created feature flag: enable_llm_intent_classification")


def downgrade():
    """Remove enable_llm_intent_classification feature flag."""

    op.execute("DELETE FROM features WHERE name = 'enable_llm_intent_classification'")

    print("✓ Removed feature flag: enable_llm_intent_classification")
