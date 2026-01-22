"""Add web_search_fallback_enabled column to tool_registry.

Revision ID: 016_add_tool_web_search_fallback
Revises: 015_enhance_external_api_keys
Create Date: 2025-12-02

Adds per-tool configuration for web search fallback when tool execution fails.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '016_add_tool_web_search_fallback'
down_revision = '015'
branch_labels = None
depends_on = None


def upgrade():
    """Add web_search_fallback_enabled column to tool_registry."""
    # Add column with default True (fallback enabled by default)
    op.add_column(
        'tool_registry',
        sa.Column('web_search_fallback_enabled', sa.Boolean(), nullable=False, server_default='true')
    )

    # Remove the server default after populating (optional, keeps column clean)
    # op.alter_column('tool_registry', 'web_search_fallback_enabled', server_default=None)


def downgrade():
    """Remove web_search_fallback_enabled column."""
    op.drop_column('tool_registry', 'web_search_fallback_enabled')
