"""Add performance optimization feature flags

Revision ID: 048_add_performance_optimization_features
Revises: 047_add_intent_routing_config
Create Date: 2026-01-12

Adds feature flags for:
- search_pre_classification: Embedding-based search pre-classification to skip LLM inference
- status_skip_synthesis: Skip LLM synthesis for simple status queries
- status_bulk_query: Use bulk HA state queries for status requests
- airport_code_lookup: Resolve city names to airport codes for flights
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic
revision = '048_add_performance_optimization_features'
down_revision = '047_add_intent_routing_config'
branch_labels = None
depends_on = None


def upgrade():
    """Add performance optimization feature flags."""

    # Insert new feature flags
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, avg_latency_ms, required, priority, config)
        VALUES
        (
            'search_pre_classification',
            'Search Pre-Classification',
            'Use embedding similarity to pre-classify obvious search queries without LLM inference. Saves ~1.3s on high-confidence matches. Disable for A/B testing.',
            'optimization',
            true,
            0,
            false,
            50,
            '{"confidence_threshold": 0.85, "fallback_to_llm": true}'::jsonb
        ),
        (
            'status_skip_synthesis',
            'Skip Status Synthesis',
            'Skip LLM synthesis for simple status queries (e.g., "what lights are on"). Returns templated response directly. Saves ~1.5s per status query.',
            'optimization',
            true,
            0,
            false,
            51,
            '{"enabled_patterns": ["lights.*on", "what.*status", "is.*locked"], "max_entities": 20}'::jsonb
        ),
        (
            'status_bulk_query',
            'Bulk HA State Query',
            'Use bulk Home Assistant state query instead of per-entity queries for status requests. Reduces HA API calls and saves 1-2s on multi-entity queries.',
            'optimization',
            true,
            0,
            false,
            52,
            '{"batch_size": 50, "timeout_ms": 5000}'::jsonb
        ),
        (
            'airport_code_lookup',
            'Airport Code Lookup',
            'Automatically resolve city names to airport codes before calling flights API. Fixes FlightAware 400 errors from natural language destinations.',
            'integration',
            true,
            0,
            false,
            53,
            '{"cache_ttl_seconds": 86400, "fallback_airports": {"new york": "JFK", "los angeles": "LAX", "chicago": "ORD", "miami": "MIA", "san francisco": "SFO", "boston": "BOS", "seattle": "SEA", "denver": "DEN", "atlanta": "ATL", "dallas": "DFW", "washington": "DCA", "baltimore": "BWI", "philadelphia": "PHL"}}'::jsonb
        ),
        (
            'entity_type_filtering',
            'Entity Type Filtering',
            'Filter HA entities by type before status queries. Only query relevant entity domains (light.*, lock.*, etc.) instead of all entities.',
            'optimization',
            true,
            0,
            false,
            54,
            '{"entity_type_map": {"lights": ["light"], "locks": ["lock"], "doors": ["lock", "binary_sensor.door", "cover.garage"], "fans": ["fan"], "climate": ["climate"], "sensors": ["sensor"]}}'::jsonb
        )
        ON CONFLICT (name) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            category = EXCLUDED.category,
            config = EXCLUDED.config,
            priority = EXCLUDED.priority;
    """)


def downgrade():
    """Remove performance optimization feature flags."""
    op.execute("""
        DELETE FROM features
        WHERE name IN (
            'search_pre_classification',
            'status_skip_synthesis',
            'status_bulk_query',
            'airport_code_lookup',
            'entity_type_filtering'
        );
    """)
