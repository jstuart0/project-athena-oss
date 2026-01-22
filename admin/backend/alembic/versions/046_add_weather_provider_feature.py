"""Add weather provider feature flag.

Revision ID: 046_add_weather_provider_feature
Revises: 045_add_ai_follow_ups_feature
Create Date: 2026-01-12

Allows toggling between standard OpenWeatherMap (free tier, 5-day forecast)
and OneCall API 3.0 (8-day forecast with alerts, hourly data).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '046_add_weather_provider_feature'
down_revision = '045_add_ai_follow_ups_feature'
branch_labels = None
depends_on = None


def upgrade():
    """Add weather_provider feature flag to features table."""
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority, config)
        VALUES (
            'weather_provider',
            'Weather Provider',
            'Select weather data provider: standard (free tier, 5-day forecast) or onecall (OneCall 3.0, 8-day forecast with alerts)',
            'rag',
            true,
            false,
            10,
            '{"mode": "standard", "available_modes": ["standard", "onecall"]}'
        )
        ON CONFLICT (name) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            config = EXCLUDED.config,
            category = EXCLUDED.category
    """)


def downgrade():
    """Remove weather_provider feature flag."""
    op.execute("DELETE FROM features WHERE name = 'weather_provider'")
