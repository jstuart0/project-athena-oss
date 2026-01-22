"""Add post-synthesis web search fallback feature flag

Revision ID: 049_add_post_synthesis_fallback
Revises: 048_add_performance_optimization_features
Create Date: 2026-01-12

Adds feature flag for post-synthesis fallback:
- post_synthesis_fallback: Retry with web search when LLM synthesis indicates it couldn't find information

When RAG services return data that doesn't answer the user's question, the system
synthesizes a "I couldn't find information" response. This feature detects those
responses and triggers a web search fallback to provide a helpful answer.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic
revision = '049_add_post_synthesis_fallback'
down_revision = '048_add_performance_optimization_features'
branch_labels = None
depends_on = None


def upgrade():
    """Add post-synthesis fallback feature flag."""

    # Insert new feature flag
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, avg_latency_ms, required, priority, config)
        VALUES
        (
            'post_synthesis_fallback',
            'Post-Synthesis Web Search Fallback',
            'When LLM synthesis indicates it could not find information (e.g., "I couldn''t find..."), automatically retry with web search. Adds latency but improves answer quality for edge cases.',
            'fallback',
            false,
            2500,
            false,
            60,
            '{
                "detection_patterns": [
                    "couldn''t find",
                    "could not find",
                    "don''t have information",
                    "no information available",
                    "unable to find",
                    "I don''t know",
                    "I''m not sure",
                    "I cannot find",
                    "no data available",
                    "not able to find"
                ],
                "excluded_intents": ["control", "automation", "scene", "timer", "reminder"],
                "max_latency_ms": 5000,
                "log_triggers": true,
                "min_response_length": 10
            }'::jsonb
        )
        ON CONFLICT (name) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            category = EXCLUDED.category,
            config = EXCLUDED.config,
            priority = EXCLUDED.priority;
    """)


def downgrade():
    """Remove post-synthesis fallback feature flag."""
    op.execute("""
        DELETE FROM features
        WHERE name = 'post_synthesis_fallback';
    """)
