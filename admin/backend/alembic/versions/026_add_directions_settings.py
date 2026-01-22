"""Add directions settings table and seed data.

Creates the directions_settings table for configurable Directions RAG service
settings, seeds default values, adds feature flag and tool registry entry.

Revision ID: 026
Revises: 025
Create Date: 2024-12-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = '026_add_directions_settings'
down_revision = '025_add_calendar_source_times'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create directions_settings table and seed data."""
    # Create directions_settings table
    op.create_table(
        'directions_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('setting_key', sa.String(100), nullable=False, unique=True, index=True),
        sa.Column('setting_value', sa.String(500), nullable=False),
        sa.Column('setting_type', sa.String(50), nullable=False),  # string, integer, boolean, json
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('category', sa.String(50), nullable=False, server_default='general'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # Create indexes
    op.create_index('idx_directions_settings_key', 'directions_settings', ['setting_key'])
    op.create_index('idx_directions_settings_category', 'directions_settings', ['category'])

    # Seed default settings
    op.execute("""
        INSERT INTO directions_settings (setting_key, setting_value, setting_type, display_name, description, category) VALUES
        ('default_travel_mode', 'driving', 'string', 'Default Travel Mode', 'Default mode when not specified (driving, walking, bicycling, transit)', 'defaults'),
        ('default_transit_mode', 'train', 'string', 'Default Transit Mode', 'Default transit type when using public transport (bus, train, subway)', 'defaults'),
        ('include_traffic', 'false', 'boolean', 'Include Traffic Data', 'Request real-time traffic data (reduces cacheability)', 'api'),
        ('cache_ttl_seconds', '300', 'integer', 'Cache TTL (seconds)', 'How long to cache direction results', 'performance'),
        ('offer_sms', 'true', 'boolean', 'Offer SMS', 'Automatically offer to text directions when SMS is enabled', 'sms'),
        ('include_step_details', 'false', 'boolean', 'Include Step Details', 'Include turn-by-turn details in SMS (longer message)', 'sms'),
        ('google_maps_link', 'true', 'boolean', 'Include Google Maps Link', 'Include Google Maps URL in responses', 'response'),
        ('max_alternatives', '1', 'integer', 'Max Route Alternatives', 'Number of alternative routes to consider (1 = fastest only)', 'api'),
        ('waypoints_enabled', 'true', 'boolean', 'Enable Waypoints/Stops', 'Allow adding stops along the route', 'waypoints'),
        ('max_waypoints', '3', 'integer', 'Max Waypoints', 'Maximum number of stops allowed per route', 'waypoints'),
        ('default_stop_position', 'halfway', 'string', 'Default Stop Position', 'Where to place stops if not specified (beginning, halfway, end)', 'waypoints'),
        ('places_search_radius_meters', '5000', 'integer', 'Places Search Radius (m)', 'Search radius for finding places along route', 'waypoints'),
        ('prefer_chain_restaurants', 'false', 'boolean', 'Prefer Chain Restaurants', 'Prefer well-known chains when searching for food stops', 'waypoints'),
        ('min_rating_for_stops', '4.0', 'string', 'Min Rating for Stops', 'Minimum Google rating (1-5) when suggesting places', 'waypoints')
    """)

    # Seed feature flag
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority) VALUES
        ('rag_directions', 'Directions RAG Service', 'Route planning and navigation via Google Directions API', 'rag', true, false, 120)
        ON CONFLICT (name) DO NOTHING
    """)

    # Seed tool registry entry
    op.execute("""
        INSERT INTO tool_registry (
            tool_name, display_name, description, category, function_schema,
            enabled, guest_mode_allowed, requires_auth, timeout_seconds,
            priority, service_url, web_search_fallback_enabled
        ) VALUES (
            'get_directions',
            'Get Directions',
            'Get driving, walking, biking, or transit directions between two locations with optional stops',
            'rag',
            '{
                "name": "get_directions",
                "description": "Get directions between two locations with optional stops along the way. Supports category-based stops (food, gas, coffee) or specific place stops.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": "The destination address or place name"
                        },
                        "origin": {
                            "type": "string",
                            "description": "Starting location (optional, defaults to home address)"
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["driving", "walking", "bicycling", "transit"],
                            "description": "Travel mode (optional, defaults to driving)"
                        },
                        "transit_mode": {
                            "type": "string",
                            "enum": ["bus", "train", "subway", "tram"],
                            "description": "Transit mode when using public transport"
                        },
                        "stops": {
                            "type": "array",
                            "description": "List of stops to add along the route",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["category", "place"],
                                        "description": "Whether this is a category search or specific place"
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "Category name (food, gas, coffee, restaurant, etc.) or specific place/address"
                                    },
                                    "position": {
                                        "type": "string",
                                        "enum": ["beginning", "quarter", "halfway", "three_quarters", "end"],
                                        "description": "Where along the route to place this stop (defaults to halfway)"
                                    },
                                    "brand": {
                                        "type": "string",
                                        "description": "Optional brand preference (e.g., Starbucks, McDonalds)"
                                    }
                                },
                                "required": ["type", "value"]
                            }
                        }
                    },
                    "required": ["destination"]
                }
            }'::jsonb,
            true,
            true,
            false,
            45,
            110,
            'http://localhost:8022',
            true
        ) ON CONFLICT (tool_name) DO NOTHING
    """)


def downgrade() -> None:
    """Remove directions settings table and seed data."""
    op.execute("DELETE FROM tool_registry WHERE tool_name = 'get_directions'")
    op.execute("DELETE FROM features WHERE name = 'rag_directions'")
    op.drop_index('idx_directions_settings_category', table_name='directions_settings')
    op.drop_index('idx_directions_settings_key', table_name='directions_settings')
    op.drop_table('directions_settings')
