"""Add centralized ollama_url system setting

Revision ID: 051
Revises: 050
Create Date: 2025-01-24

This migration adds a centralized ollama_url system setting that:
- Is the single source of truth for the Ollama API URL
- Can be configured via Admin UI
- Is seeded from OLLAMA_URL environment variable on first run
- Replaces per-model endpoint_url in llm_backends (which becomes optional override)
"""

import os
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '051'
down_revision = '050'
branch_labels = None
depends_on = None

# Get Ollama URL from environment for initial seeding
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')


def upgrade():
    """Add ollama_url system setting."""

    # Insert ollama_url into system_settings
    # This is the single source of truth for all services
    op.execute(f"""
        INSERT INTO system_settings (key, value, description, category)
        VALUES (
            'ollama_url',
            '{OLLAMA_URL}',
            'Primary Ollama API URL. All LLM requests use this endpoint unless overridden per-model.',
            'llm'
        )
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            description = EXCLUDED.description,
            category = EXCLUDED.category
    """)

    print(f"✓ Added ollama_url system setting: {OLLAMA_URL}")


def downgrade():
    """Remove ollama_url system setting."""
    op.execute("DELETE FROM system_settings WHERE key = 'ollama_url'")
    print("✓ Removed ollama_url system setting")
