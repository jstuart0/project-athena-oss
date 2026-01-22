"""Add multi-guest support with device identification.

Revision ID: 023_add_multi_guest_support
Revises: 022_add_room_groups
Create Date: 2025-12-12

Adds:
- guests: Multiple guests per calendar event (reservation)
- user_sessions: Device fingerprint to guest mapping

Supports future voice fingerprinting via voice_profile_id field.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '023_add_multi_guest_support'
down_revision = '022_add_room_groups'
branch_labels = None
depends_on = None


def upgrade():
    """Create guests and user_sessions tables, migrate existing data."""

    # Create guests table
    op.create_table(
        'guests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('calendar_event_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=100), nullable=True),
        sa.Column('phone', sa.String(length=20), nullable=True),
        sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('voice_profile_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['calendar_event_id'], ['calendar_events.id'], ondelete='CASCADE')
    )
    op.create_index('idx_guests_calendar_event', 'guests', ['calendar_event_id'])
    op.create_index('idx_guests_voice_profile', 'guests', ['voice_profile_id'])
    op.create_index('idx_guests_is_primary', 'guests', ['is_primary'])

    # Create user_sessions table
    op.create_table(
        'user_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=255), nullable=False),
        sa.Column('guest_id', sa.Integer(), nullable=True),
        sa.Column('device_id', sa.String(length=255), nullable=False),
        sa.Column('device_type', sa.String(length=50), server_default='web', nullable=False),
        sa.Column('room', sa.String(length=50), nullable=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('preferences', postgresql.JSONB(), server_default='{}', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', name='uq_user_session_id'),
        sa.ForeignKeyConstraint(['guest_id'], ['guests.id'], ondelete='CASCADE')
    )
    op.create_index('idx_user_sessions_device', 'user_sessions', ['device_id'])
    op.create_index('idx_user_sessions_guest', 'user_sessions', ['guest_id'])
    op.create_index('idx_user_sessions_last_seen', 'user_sessions', ['last_seen'])

    # Migrate existing calendar_events.guest_name to guests table
    # This creates a Guest record for each existing reservation with guest info
    op.execute("""
        INSERT INTO guests (calendar_event_id, name, email, phone, is_primary, created_at)
        SELECT id, guest_name, guest_email, guest_phone, true, created_at
        FROM calendar_events
        WHERE guest_name IS NOT NULL
          AND guest_name != ''
          AND deleted_at IS NULL
    """)


def downgrade():
    """Remove guests and user_sessions tables."""
    op.drop_table('user_sessions')
    op.drop_table('guests')
