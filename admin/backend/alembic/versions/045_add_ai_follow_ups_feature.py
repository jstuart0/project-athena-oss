"""Add AI follow-ups feature flag.

Revision ID: 045_add_ai_follow_ups_feature
Revises: 044_add_long_query_rules
Create Date: 2026-01-12

Phase 2 Voice Assistant Improvements: AI-initiated follow-ups.
After Athena responds, if silence detected for 3 seconds, she can ask
"Is there anything else?" to continue the conversation naturally.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '045_add_ai_follow_ups_feature'
down_revision = '044_add_long_query_rules'
branch_labels = None
depends_on = None


def upgrade():
    """Add AI follow-ups feature flag to features table."""
    # Insert the feature flag
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority)
        VALUES (
            'ai_follow_ups_enabled',
            'AI-Initiated Follow-ups',
            'After responding, Athena asks "Is there anything else?" after 3 seconds of silence. Allows natural conversation continuation without wake word. Max 2 follow-ups per session.',
            'voice',
            false,
            false,
            50
        )
        ON CONFLICT (name) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            category = EXCLUDED.category
    """)


def downgrade():
    """Remove AI follow-ups feature flag."""
    op.execute("DELETE FROM features WHERE name = 'ai_follow_ups_enabled'")
