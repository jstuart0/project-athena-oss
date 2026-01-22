"""Add tool_proposals table for self-building tools.

This migration adds the tool_proposals table for the self-building tools feature,
which allows the LLM to propose new n8n workflows that require owner approval.

Revision ID: 034_add_tool_proposals
Revises: 033_hybrid_tool_registry_phase1
Create Date: 2025-12-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '034_add_tool_proposals'
down_revision = '033_hybrid_tool_registry_phase1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # Tool Proposals Table
    # Stores LLM-proposed tool definitions awaiting approval
    # =========================================================================
    op.create_table(
        'tool_proposals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('proposal_id', sa.String(50), nullable=False),  # Short unique ID like 'abc12345'
        sa.Column('name', sa.String(100), nullable=False),  # Tool name (snake_case)
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('trigger_phrases', postgresql.JSONB(), nullable=False),  # List of phrases
        sa.Column('workflow_definition', postgresql.JSONB(), nullable=False),  # Full n8n workflow JSON

        # Status tracking
        sa.Column('status', sa.String(20), server_default='pending', nullable=False),
        # 'pending', 'approved', 'rejected', 'deployed', 'failed'

        # Creation info
        sa.Column('created_by', sa.String(100), server_default='llm', nullable=False),  # 'llm', username
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),

        # Approval info
        sa.Column('approved_by_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('rejection_reason', sa.Text()),

        # Deployment info
        sa.Column('n8n_workflow_id', sa.String(100)),  # ID from n8n after deployment
        sa.Column('deployed_at', sa.DateTime(timezone=True)),
        sa.Column('error_message', sa.Text()),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('proposal_id'),
    )

    # Indexes for common queries
    op.create_index('idx_tool_proposals_status', 'tool_proposals', ['status'])
    op.create_index('idx_tool_proposals_created_at', 'tool_proposals', ['created_at'])
    op.create_index('idx_tool_proposals_name', 'tool_proposals', ['name'])


def downgrade() -> None:
    op.drop_index('idx_tool_proposals_name')
    op.drop_index('idx_tool_proposals_created_at')
    op.drop_index('idx_tool_proposals_status')
    op.drop_table('tool_proposals')
