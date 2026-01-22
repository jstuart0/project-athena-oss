"""Add site scraper configuration table.

Revision ID: 037_site_scraper_config
Revises: 036_pipeline_events
Create Date: 2025-12-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '037_site_scraper_config'
down_revision = '036_pipeline_events'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'site_scraper_config',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_mode_any_url', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('guest_mode_any_url', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('allowed_domains', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('blocked_domains', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('max_content_length', sa.Integer(), nullable=False, server_default='50000'),
        sa.Column('cache_ttl', sa.Integer(), nullable=False, server_default='1800'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # Insert default configuration
    op.execute("""
        INSERT INTO site_scraper_config (owner_mode_any_url, guest_mode_any_url, allowed_domains, blocked_domains)
        VALUES (true, false, '{}', '{}')
    """)


def downgrade() -> None:
    op.drop_table('site_scraper_config')
