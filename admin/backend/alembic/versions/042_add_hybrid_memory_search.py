"""Add hybrid memory search feature flag

Revision ID: 042_hybrid_memory
Revises: 041_dynamic_automation_system
Create Date: 2026-01-04

Adds feature flag to enable hybrid (keyword + semantic) memory search.
When enabled, memory retrieval combines BM25 keyword matching with
vector similarity for better recall on keyword-heavy queries.
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime


# revision identifiers
revision = '042_hybrid_memory'
down_revision = '041_dynamic_automation_system'
branch_labels = None
depends_on = None


def upgrade():
    """Add hybrid_memory_search feature flag."""
    conn = op.get_bind()

    # Check if feature already exists
    result = conn.execute(
        sa.text("SELECT id FROM features WHERE name = 'hybrid_memory_search'")
    ).fetchone()

    if not result:
        conn.execute(sa.text("""
            INSERT INTO features (
                name, display_name, description, category, enabled,
                avg_latency_ms, required, priority, config, requires_restart,
                created_at, updated_at
            ) VALUES (
                'hybrid_memory_search',
                'Hybrid Memory Search',
                'Combines keyword matching with semantic vector search for memory retrieval. Improves recall for queries with specific keywords (e.g., "miles driven") that may not match semantically. Adds minimal latency (~5-10ms) via parallel execution. May not be needed if your LLM model is smart enough to infer context without explicit memory hints.',
                'optimization',
                false,
                5.0,
                false,
                50,
                '{"keyword_weight": 0.3, "semantic_weight": 0.7, "min_keyword_score": 0.5}'::jsonb,
                false,
                NOW(),
                NOW()
            )
        """))
        print("Added hybrid_memory_search feature flag")
    else:
        print("hybrid_memory_search feature flag already exists")


def downgrade():
    """Remove hybrid_memory_search feature flag."""
    conn = op.get_bind()
    conn.execute(sa.text("""
        DELETE FROM features WHERE name = 'hybrid_memory_search'
    """))
    print("Removed hybrid_memory_search feature flag")
