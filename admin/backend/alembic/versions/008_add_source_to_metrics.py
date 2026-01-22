"""Add source column to LLM performance metrics

Revision ID: 008_add_source_to_metrics
Revises: 007_voice_test_feedback
Create Date: 2025-11-16

This migration adds a source column to track where LLM requests originated
(admin_voice_test, gateway, orchestrator, rag services, etc.)
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade():
    """Add source column to llm_performance_metrics."""

    # Add source column
    op.add_column('llm_performance_metrics',
                  sa.Column('source', sa.String(length=50), nullable=True))

    # Add index for source column
    op.create_index('idx_llm_metrics_source', 'llm_performance_metrics', ['source'])


def downgrade():
    """Remove source column from llm_performance_metrics."""

    # Drop index first
    op.drop_index('idx_llm_metrics_source', table_name='llm_performance_metrics')

    # Drop source column
    op.drop_column('llm_performance_metrics', 'source')
