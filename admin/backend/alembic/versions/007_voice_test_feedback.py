"""Add voice test feedback table

Revision ID: 007_voice_test_feedback
Revises: 006_end_to_end_latency_tracking
Create Date: 2025-11-16

This migration adds a feedback table for voice test results to support
active learning and response quality tracking.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade():
    """Add voice_test_feedback table."""

    op.create_table(
        'voice_test_feedback',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('test_id', sa.Integer(), sa.ForeignKey('voice_tests.id', ondelete='CASCADE'), nullable=False),
        sa.Column('feedback_type', sa.String(length=20), nullable=False),  # 'correct' or 'incorrect'
        sa.Column('query', sa.Text(), nullable=False),  # Original query for reference
        sa.Column('response', sa.Text(), nullable=True),  # LLM response that was marked
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),  # Optional user notes
        sa.PrimaryKeyConstraint('id'),
        sa.Index('idx_feedback_test_id', 'test_id'),
        sa.Index('idx_feedback_type', 'feedback_type'),
        sa.Index('idx_feedback_created_at', 'created_at'),
    )


def downgrade():
    """Remove voice_test_feedback table."""
    op.drop_table('voice_test_feedback')
