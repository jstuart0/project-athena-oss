"""Add tool_api_key_requirements junction table and required_api_keys cache.

Revision ID: 017_add_tool_api_key_requirements
Revises: 016_add_tool_web_search_fallback
Create Date: 2025-12-02

Links tools to their required API keys for:
- Validation before tool execution
- Automatic key injection into tool calls
- Clear visibility in Admin UI of dependencies
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '017_add_tool_api_key_requirements'
down_revision = '016_add_tool_web_search_fallback'
branch_labels = None
depends_on = None


def upgrade():
    """Create tool_api_key_requirements table and add cache field to tool_registry."""

    # Add required_api_keys cache field to tool_registry
    op.add_column(
        'tool_registry',
        sa.Column('required_api_keys', JSONB, server_default='[]', nullable=True)
    )

    # Create junction table for tool-API key relationships
    op.create_table(
        'tool_api_key_requirements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tool_id', sa.Integer(), sa.ForeignKey('tool_registry.id', ondelete='CASCADE'), nullable=False),
        sa.Column('api_key_service', sa.String(255), nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('inject_as', sa.String(100), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Create indexes
    op.create_index('idx_tool_api_key_tool_id', 'tool_api_key_requirements', ['tool_id'])
    op.create_index('idx_tool_api_key_service', 'tool_api_key_requirements', ['api_key_service'])

    # Create unique constraint
    op.create_unique_constraint(
        'uq_tool_api_key_requirement',
        'tool_api_key_requirements',
        ['tool_id', 'api_key_service']
    )

    # Seed initial tool-API key relationships based on known requirements
    # This maps tools to the API keys they need
    op.execute("""
        -- search_restaurants needs google-places and/or foursquare
        INSERT INTO tool_api_key_requirements (tool_id, api_key_service, is_required, inject_as, description)
        SELECT id, 'google-places', true, 'google_api_key', 'Google Places API for restaurant search'
        FROM tool_registry WHERE tool_name = 'search_restaurants'
        ON CONFLICT DO NOTHING;

        INSERT INTO tool_api_key_requirements (tool_id, api_key_service, is_required, inject_as, description)
        SELECT id, 'foursquare', false, 'foursquare_api_key', 'Foursquare API for additional venue data'
        FROM tool_registry WHERE tool_name = 'search_restaurants'
        ON CONFLICT DO NOTHING;

        -- search_events needs ticketmaster
        INSERT INTO tool_api_key_requirements (tool_id, api_key_service, is_required, inject_as, description)
        SELECT id, 'ticketmaster', true, 'api_key', 'Ticketmaster API for event search'
        FROM tool_registry WHERE tool_name = 'search_events'
        ON CONFLICT DO NOTHING;

        -- search_flights needs flightaware
        INSERT INTO tool_api_key_requirements (tool_id, api_key_service, is_required, inject_as, description)
        SELECT id, 'flightaware', true, 'api_key', 'FlightAware API for flight data'
        FROM tool_registry WHERE tool_name = 'search_flights'
        ON CONFLICT DO NOTHING;

        -- get_news needs newsapi or serpapi
        INSERT INTO tool_api_key_requirements (tool_id, api_key_service, is_required, inject_as, description)
        SELECT id, 'serpapi', false, 'api_key', 'SerpAPI for news search (fallback)'
        FROM tool_registry WHERE tool_name = 'get_news'
        ON CONFLICT DO NOTHING;

        -- search_web needs searxng (self-hosted, no key needed) or serpapi
        INSERT INTO tool_api_key_requirements (tool_id, api_key_service, is_required, inject_as, description)
        SELECT id, 'serpapi', false, 'api_key', 'SerpAPI for web search (fallback if SearXNG unavailable)'
        FROM tool_registry WHERE tool_name = 'search_web'
        ON CONFLICT DO NOTHING;
    """)

    # Update the cache field for tools that have requirements
    op.execute("""
        UPDATE tool_registry t
        SET required_api_keys = (
            SELECT COALESCE(json_agg(r.api_key_service), '[]'::json)
            FROM tool_api_key_requirements r
            WHERE r.tool_id = t.id
        )
        WHERE EXISTS (
            SELECT 1 FROM tool_api_key_requirements r WHERE r.tool_id = t.id
        );
    """)


def downgrade():
    """Remove tool_api_key_requirements table and cache field."""
    op.drop_table('tool_api_key_requirements')
    op.drop_column('tool_registry', 'required_api_keys')
