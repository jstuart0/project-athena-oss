"""Add get_sports_standings tool to tool registry

Revision ID: 020_add_sports_standings_tool
Revises: 019_add_sms_enhanced_features
Create Date: 2025-12-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
import json

# revision identifiers, used by Alembic
revision = '020_add_sports_standings_tool'
down_revision = '019_add_sms_enhanced_features'
branch_labels = None
depends_on = None


def upgrade():
    """Add get_sports_standings tool to tool_registry."""

    # Get connection for executing raw SQL
    conn = op.get_bind()

    # Define the new tool
    tool_data = {
        "tool_name": "get_sports_standings",
        "display_name": "Sports Standings & Rankings",
        "description": "Get league standings, rankings, and best teams",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_sports_standings",
                "description": "Get current standings and rankings for a league. Use this for questions about 'best team', 'top teams', 'rankings', 'standings', 'who is leading', or 'who has the best record'. Returns teams sorted by wins/points.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "league": {
                            "type": "string",
                            "description": "League to get standings for",
                            "enum": ["nfl", "nba", "mlb", "nhl", "premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1", "mls", "ncaaf", "ncaab"]
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of top teams to return (default 10)",
                            "default": 10
                        }
                    },
                    "required": ["league"]
                }
            }
        },
        "enabled": True,
        "guest_mode_allowed": True,
        "timeout_seconds": 15,
        "priority": 100,
        "service_url": "http://localhost:8017",
        "web_search_fallback_enabled": True
    }

    # Insert the new tool
    conn.execute(
        sa.text("""
            INSERT INTO tool_registry (
                tool_name, display_name, description, category,
                function_schema, enabled, guest_mode_allowed,
                timeout_seconds, priority, service_url, web_search_fallback_enabled,
                created_at, updated_at
            ) VALUES (
                :tool_name, :display_name, :description, :category,
                CAST(:function_schema AS jsonb), :enabled, :guest_mode_allowed,
                :timeout_seconds, :priority, :service_url, :web_search_fallback_enabled,
                NOW(), NOW()
            )
            ON CONFLICT (tool_name) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                description = EXCLUDED.description,
                function_schema = EXCLUDED.function_schema,
                enabled = EXCLUDED.enabled,
                guest_mode_allowed = EXCLUDED.guest_mode_allowed,
                timeout_seconds = EXCLUDED.timeout_seconds,
                service_url = EXCLUDED.service_url,
                web_search_fallback_enabled = EXCLUDED.web_search_fallback_enabled,
                updated_at = NOW()
        """),
        {
            "tool_name": tool_data["tool_name"],
            "display_name": tool_data["display_name"],
            "description": tool_data["description"],
            "category": tool_data["category"],
            "function_schema": json.dumps(tool_data["function_schema"]),
            "enabled": tool_data["enabled"],
            "guest_mode_allowed": tool_data["guest_mode_allowed"],
            "timeout_seconds": tool_data["timeout_seconds"],
            "priority": tool_data["priority"],
            "service_url": tool_data["service_url"],
            "web_search_fallback_enabled": tool_data["web_search_fallback_enabled"]
        }
    )

    print("✓ Added get_sports_standings tool to tool_registry")


def downgrade():
    """Remove get_sports_standings tool from tool_registry."""

    conn = op.get_bind()

    conn.execute(
        sa.text("DELETE FROM tool_registry WHERE tool_name = 'get_sports_standings'")
    )

    print("✓ Removed get_sports_standings tool from tool_registry")
