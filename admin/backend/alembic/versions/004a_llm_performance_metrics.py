"""Add LLM performance metrics table

Revision ID: 004
Revises: 93bea4659785
Create Date: 2025-11-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '004a'
down_revision = '93bea4659785'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create llm_performance_metrics table for storing LLM performance data."""
    op.create_table(
        'llm_performance_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('backend', sa.String(length=50), nullable=False),
        sa.Column('latency_seconds', sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column('tokens_generated', sa.Integer(), nullable=False),
        sa.Column('tokens_per_second', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('request_id', sa.String(length=100), nullable=True),
        sa.Column('session_id', sa.String(length=100), nullable=True),
        sa.Column('user_id', sa.String(length=100), nullable=True),
        sa.Column('zone', sa.String(length=100), nullable=True),
        sa.Column('intent', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes for efficient querying
    op.create_index('idx_llm_metrics_timestamp', 'llm_performance_metrics', ['timestamp'], unique=False)
    op.create_index('idx_llm_metrics_model', 'llm_performance_metrics', ['model'], unique=False)
    op.create_index('idx_llm_metrics_backend', 'llm_performance_metrics', ['backend'], unique=False)
    op.create_index('idx_llm_metrics_intent', 'llm_performance_metrics', ['intent'], unique=False)
    op.create_index('idx_llm_metrics_request_id', 'llm_performance_metrics', ['request_id'], unique=False)
    op.create_index('idx_llm_metrics_session_id', 'llm_performance_metrics', ['session_id'], unique=False)
    op.create_index('idx_llm_metrics_composite', 'llm_performance_metrics', ['timestamp', 'model', 'backend'], unique=False)


def downgrade() -> None:
    """Drop llm_performance_metrics table and all indexes."""
    op.drop_index('idx_llm_metrics_composite', table_name='llm_performance_metrics')
    op.drop_index('idx_llm_metrics_session_id', table_name='llm_performance_metrics')
    op.drop_index('idx_llm_metrics_request_id', table_name='llm_performance_metrics')
    op.drop_index('idx_llm_metrics_intent', table_name='llm_performance_metrics')
    op.drop_index('idx_llm_metrics_backend', table_name='llm_performance_metrics')
    op.drop_index('idx_llm_metrics_model', table_name='llm_performance_metrics')
    op.drop_index('idx_llm_metrics_timestamp', table_name='llm_performance_metrics')
    op.drop_table('llm_performance_metrics')
