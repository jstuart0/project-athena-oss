"""Update RAG tool ports in tool_registry.

This migration updates the service URLs for RAG tools that had port conflicts:
- directions: 8022 -> 8030
- site_scraper: 8022 -> 8031
- serpapi_events: 8022 -> 8032
- price_compare: 8023 -> 8033

Also ensures all RAG tools are properly registered with correct ports.

Revision ID: 038_rag_tool_ports
Revises: 037_site_scraper_config
Create Date: 2025-12-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '038_rag_tool_ports'
down_revision = '037_site_scraper_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Update tool registry with corrected RAG service ports."""

    # Update existing tools with new ports
    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8030'
        WHERE tool_name = 'get_directions';
    """)

    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8031'
        WHERE tool_name = 'scrape_website';
    """)

    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8032'
        WHERE tool_name = 'search_events';
    """)

    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8033'
        WHERE tool_name = 'compare_prices';
    """)

    # Upsert all RAG tools to ensure they exist with correct ports
    # Using ON CONFLICT to update if exists, insert if not
    rag_tools = [
        # (tool_name, display_name, description, category, service_url, guest_mode_allowed, function_schema)
        ('get_weather', 'Get Weather', 'Get current weather and forecast for a location', 'rag',
         'http://localhost:8010', True,
         '{"type": "function", "function": {"name": "get_weather", "description": "Get current weather and forecast for a location", "parameters": {"type": "object", "properties": {"location": {"type": "string", "description": "City name or location"}, "units": {"type": "string", "enum": ["fahrenheit", "celsius"], "default": "fahrenheit"}}, "required": ["location"]}}}'),

        ('get_airport_info', 'Get Airport Info', 'Get airport information, delays, and flight status', 'rag',
         'http://localhost:8011', True,
         '{"type": "function", "function": {"name": "get_airport_info", "description": "Get airport information, delays, and flight status", "parameters": {"type": "object", "properties": {"airport": {"type": "string", "description": "Airport code (e.g., BWI, JFK) or name"}}, "required": ["airport"]}}}'),

        ('get_sports_scores', 'Get Sports Scores', 'Get sports scores, schedules, and team information', 'rag',
         'http://localhost:8017', True,
         '{"type": "function", "function": {"name": "get_sports_scores", "description": "Get sports scores, schedules, and team information", "parameters": {"type": "object", "properties": {"team": {"type": "string", "description": "Team name"}, "league": {"type": "string", "description": "League (nfl, nba, mlb, nhl, premier-league, mls)"}}, "required": ["team"]}}}'),

        ('search_web', 'Search Web', 'Search the web for information', 'rag',
         'http://localhost:8018', True,
         '{"type": "function", "function": {"name": "search_web", "description": "Search the web for information", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}}, "required": ["query"]}}}'),

        ('search_restaurants', 'Search Restaurants', 'Find restaurants by cuisine, location, or name', 'rag',
         'http://localhost:8019', True,
         '{"type": "function", "function": {"name": "search_restaurants", "description": "Find restaurants by cuisine, location, or name", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Restaurant name, cuisine type, or search query"}, "location": {"type": "string", "description": "City or area to search"}}, "required": ["query"]}}}'),

        ('search_recipes', 'Search Recipes', 'Find recipes by ingredients, dish name, or cuisine', 'rag',
         'http://localhost:8020', True,
         '{"type": "function", "function": {"name": "search_recipes", "description": "Find recipes by ingredients, dish name, or cuisine", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Recipe name, ingredient, or cuisine type"}}, "required": ["query"]}}}'),

        ('get_directions', 'Get Directions', 'Get driving/walking/transit directions between locations', 'rag',
         'http://localhost:8030', True,
         '{"type": "function", "function": {"name": "get_directions", "description": "Get driving, walking, or transit directions between locations", "parameters": {"type": "object", "properties": {"origin": {"type": "string", "description": "Starting location"}, "destination": {"type": "string", "description": "End location"}, "mode": {"type": "string", "enum": ["driving", "walking", "transit"], "default": "driving"}}, "required": ["origin", "destination"]}}}'),

        ('scrape_website', 'Scrape Website', 'Extract content from a website URL', 'rag',
         'http://localhost:8031', True,
         '{"type": "function", "function": {"name": "scrape_website", "description": "Extract content from a website URL", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Website URL to scrape"}}, "required": ["url"]}}}'),

        ('search_events', 'Search Events', 'Find local events, concerts, shows, and activities', 'rag',
         'http://localhost:8032', True,
         '{"type": "function", "function": {"name": "search_events", "description": "Find local events, concerts, shows, and activities", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Event type or search query"}, "location": {"type": "string", "description": "City or area"}}, "required": ["query"]}}}'),

        ('compare_prices', 'Compare Prices', 'Compare prices for products across retailers', 'rag',
         'http://localhost:8033', True,
         '{"type": "function", "function": {"name": "compare_prices", "description": "Compare prices for products across multiple retailers", "parameters": {"type": "object", "properties": {"product": {"type": "string", "description": "Product name or description"}}, "required": ["product"]}}}'),
    ]

    for tool in rag_tools:
        tool_name, display_name, description, category, service_url, guest_mode, schema = tool
        op.execute(f"""
            INSERT INTO tool_registry
                (tool_name, display_name, description, category, service_url, enabled, guest_mode_allowed, function_schema, source)
            VALUES
                ('{tool_name}', '{display_name}', '{description}', '{category}',
                 '{service_url}', true, {str(guest_mode).lower()}, '{schema}'::jsonb, 'static')
            ON CONFLICT (tool_name)
            DO UPDATE SET
                service_url = EXCLUDED.service_url,
                source = 'static',
                enabled = true;
        """)


def downgrade() -> None:
    """Revert tool registry ports to original values."""

    # Revert port changes
    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8022'
        WHERE tool_name = 'get_directions';
    """)

    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8022'
        WHERE tool_name = 'scrape_website';
    """)

    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8023'
        WHERE tool_name = 'search_events';
    """)

    op.execute("""
        UPDATE tool_registry
        SET service_url = 'http://localhost:8023'
        WHERE tool_name = 'compare_prices';
    """)
