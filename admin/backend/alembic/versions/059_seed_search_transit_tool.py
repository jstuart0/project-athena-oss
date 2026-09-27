"""Seed the search_transit tool_registry row (ATHENA-90, D9/M5).

search_transit (src/orchestrator/rag_tools.py) has been reachable to the
LLM only through the registry's legacy fallback (legacy_tools_fallback,
default True) -- it has no tool_registry row, so it can't be toggled on
the Admin UI tools page, contrary to CLAUDE.md's Admin-UI-first rule.

This is a data-only seed, following the 038 pattern: ON CONFLICT DO
NOTHING, so an operator's own row (enabled or disabled) is never
overwritten. SEARCH_TRANSIT_SCHEMA below must stay equal to
rag_tools.TOOL_DEFINITIONS["search_transit"]["function_schema"] --
enforced by tests/unit/test_transit_tool_wiring.py::T7. If a future edit
changes the tool's params without updating this literal, T7 fails loud
rather than silently shipping a stale Admin-UI schema.

Revision ID: 059
Revises: 058
Create Date: 2026-09-27

ATHENA-90 (Campaign: 2026-09-27-deliver-athena-transit-and-base-knowledge)
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None

SEARCH_TRANSIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_transit",
        "description": (
            "Search public transit in the configured region: stops, routes, "
            "schedules, departures, and free options. Give stop_id for next "
            "departures, query for a stop or route name (add lat/lon to sort "
            "by distance), or lat/lon alone for nearby stops."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {
                    "type": "number",
                    "description": "Latitude of the user's location",
                },
                "lon": {
                    "type": "number",
                    "description": "Longitude of the user's location",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for stops or routes (e.g., 'Penn Station', 'Route 11')",
                },
                "stop_id": {
                    "type": "string",
                    "description": "Specific stop ID to get departures",
                },
                "transit_type": {
                    "type": "string",
                    "description": "Filter by transit type",
                    "enum": ["bus", "metro", "light_rail", "rail", "ferry", "commuter_bus"],
                },
                "free_only": {
                    "type": "boolean",
                    "description": "Only show free transit options",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}


def upgrade() -> None:
    bind = op.get_bind()

    # Defensive, same philosophy as 058's ENCRYPTION_KEY pre-check: this
    # migration's only job is to seed a row into a table that should
    # already exist in any real deployment (tool_registry predates this
    # migration by dozens of revisions). A schema-fixture test that stamps
    # straight to a mid-chain revision without the full table set (see
    # test_service_registry_endpoint_post_rename.py) shouldn't crash the
    # whole upgrade chain over a table this migration doesn't own creating.
    if not sa.inspect(bind).has_table("tool_registry"):
        print("WARNING: tool_registry table not found — skipping search_transit seed (migration 059).")
        return

    is_postgres = bind.dialect.name == "postgresql"
    schema_json = json.dumps(SEARCH_TRANSIT_SCHEMA)

    insert_sql = """
        INSERT INTO tool_registry
            (tool_name, display_name, description, category, service_url,
             enabled, guest_mode_allowed, timeout_seconds, source, function_schema)
        VALUES
            (:tool_name, :display_name, :description, :category, :service_url,
             true, :guest_mode, :timeout_seconds, 'static', {schema_expr})
        ON CONFLICT (tool_name) DO NOTHING;
    """.format(schema_expr="CAST(:schema AS jsonb)" if is_postgres else ":schema")

    bind.execute(
        sa.text(insert_sql),
        {
            "tool_name": "search_transit",
            "display_name": "Transit & Transportation",
            "description": "Find nearby transit stops, routes, and schedules for buses, trains, ferries, and light rail",
            "category": "rag",
            "service_url": "http://athena-rag-transportation:8025",
            "guest_mode": True,
            "timeout_seconds": 20,
            "schema": schema_json,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tool_registry"):
        return
    op.execute(
        "DELETE FROM tool_registry WHERE tool_name = 'search_transit' AND source = 'static';"
    )
