"""Add pipeline_events table for Admin Jarvis real-time monitoring.

Revision ID: 036_pipeline_events
Revises: 035_add_livekit_config
Create Date: 2024-12-28

Stores pipeline events emitted by the orchestrator for:
- Real-time WebSocket streaming to Admin Jarvis UI
- Historical event querying and analytics
- Debugging and troubleshooting
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '036_pipeline_events'
down_revision = '035_add_livekit_config'
branch_labels = None
depends_on = None


def upgrade():
    # Create pipeline_events table
    op.create_table(
        'pipeline_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_type', sa.String(50), nullable=False, index=True),
        sa.Column('session_id', sa.String(100), nullable=False, index=True),
        sa.Column('interface', sa.String(50), nullable=True, index=True),
        sa.Column('data', JSONB, nullable=True),
        sa.Column('timestamp', sa.Float(), nullable=False, index=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    # Create composite index for common query patterns
    op.create_index(
        'ix_pipeline_events_session_timestamp',
        'pipeline_events',
        ['session_id', 'timestamp']
    )

    # Create index for time-based queries
    op.create_index(
        'ix_pipeline_events_type_timestamp',
        'pipeline_events',
        ['event_type', 'timestamp']
    )

    # Add retention policy comment (for future reference)
    # Consider adding a cron job to delete events older than 7 days


def downgrade():
    op.drop_index('ix_pipeline_events_type_timestamp', table_name='pipeline_events')
    op.drop_index('ix_pipeline_events_session_timestamp', table_name='pipeline_events')
    op.drop_table('pipeline_events')
