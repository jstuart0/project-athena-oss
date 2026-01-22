"""Add Apple TV configuration tables.

Room TV Config: Maps rooms to Apple TV entities
TV App Config: Per-app settings (profile screens, delays, guest access)
TV Feature Flags: Multi-TV commands, auto profile select

Revision ID: 031_add_apple_tv_config
Revises: 030_add_user_api_keys
Create Date: 2025-12-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '031_add_apple_tv_config'
down_revision = '030_add_user_api_keys'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Room TV Config - maps rooms to Apple TV entities
    op.create_table(
        'room_tv_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('room_name', sa.String(100), nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('media_player_entity_id', sa.String(255), nullable=False),
        sa.Column('remote_entity_id', sa.String(255), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('room_name'),
    )
    op.create_index('idx_room_tv_configs_room_name', 'room_tv_configs', ['room_name'])
    op.create_index('idx_room_tv_configs_enabled', 'room_tv_configs', ['enabled'])

    # TV App Config - per-app settings
    op.create_table(
        'tv_app_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('app_name', sa.String(100), nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('icon_url', sa.String(500)),
        sa.Column('has_profile_screen', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('profile_select_delay_ms', sa.Integer(), server_default='1500', nullable=False),
        sa.Column('guest_allowed', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('deep_link_scheme', sa.String(100)),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('app_name'),
    )
    op.create_index('idx_tv_app_configs_enabled', 'tv_app_configs', ['enabled'])
    op.create_index('idx_tv_app_configs_guest', 'tv_app_configs', ['guest_allowed'])

    # TV Feature Flags
    op.create_table(
        'tv_feature_flags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('feature_name', sa.String(100), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('feature_name'),
    )

    # Seed TV feature flags
    op.execute("""
        INSERT INTO tv_feature_flags (feature_name, enabled, description) VALUES
            ('multi_tv_commands', false, 'Allow commands like "open Netflix everywhere"'),
            ('auto_profile_select', true, 'Automatically send select command for apps with profile screens'),
            ('guest_mode_filtering', true, 'Filter apps based on owner/guest mode')
    """)

    # Seed default app configs based on testing (31 apps that work)
    op.execute("""
        INSERT INTO tv_app_configs (app_name, display_name, has_profile_screen, guest_allowed, deep_link_scheme, sort_order, icon_url) VALUES
            ('Netflix', 'Netflix', true, true, NULL, 1, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/netflix_logo_icon_170919.png'),
            ('YouTube', 'YouTube', true, true, 'youtube', 2, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/youtube_logo_icon_168737.png'),
            ('Disney+', 'Disney+', true, true, 'disneyplus', 3, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/disneyplus_logo_icon_168587.png'),
            ('Prime Video', 'Prime Video', true, true, NULL, 4, 'https://upload.wikimedia.org/wikipedia/commons/1/11/Amazon_Prime_Video_logo.svg'),
            ('Hulu', 'Hulu', false, true, 'hulu', 5, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/hulu_logo_icon_168908.png'),
            ('HBO Max', 'Max', false, true, NULL, 6, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/hbo_logo_icon_168869.png'),
            ('Paramount+', 'Paramount+', false, true, NULL, 7, NULL),
            ('Peacock', 'Peacock', false, true, NULL, 8, NULL),
            ('YouTube TV', 'YouTube TV', false, true, NULL, 9, NULL),
            ('DIRECTV', 'DIRECTV', true, true, NULL, 10, NULL),
            ('ESPN', 'ESPN', false, true, NULL, 11, NULL),
            ('NFL', 'NFL', false, true, NULL, 12, NULL),
            ('Spotify', 'Spotify', false, true, 'spotify', 13, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/spotify_logo_icon_168894.png'),
            ('TV', 'Apple TV+', false, true, NULL, 14, 'https://cdn.icon-icons.com/icons2/2699/PNG/512/apple_tv_logo_icon_170917.png'),
            ('Music', 'Apple Music', false, true, NULL, 15, NULL),
            ('Arcade', 'Apple Arcade', false, true, NULL, 16, NULL),
            ('Fitness', 'Apple Fitness+', false, true, NULL, 17, NULL),
            ('Tubi', 'Tubi', false, true, NULL, 18, NULL),
            ('The CW', 'The CW', false, true, NULL, 19, NULL),
            ('FXNOW', 'FXNOW', false, true, NULL, 20, NULL),
            ('NEC On The Run', 'NEC On The Run', false, true, NULL, 21, NULL),
            ('Podcasts', 'Podcasts', false, true, NULL, 22, NULL),
            ('Movies', 'Movies', false, true, NULL, 23, NULL),
            ('Search', 'Search', false, true, NULL, 24, NULL),
            ('Computers', 'AirPlay', false, true, NULL, 25, NULL),
            ('BODi', 'BODi', false, true, NULL, 26, NULL),
            ('Photos', 'Photos', false, false, NULL, 50, NULL),
            ('Settings', 'Settings', false, false, NULL, 51, NULL),
            ('TV Shows', 'TV Shows', false, false, NULL, 52, NULL),
            ('VLC', 'VLC', false, false, NULL, 53, NULL),
            ('IPVanish', 'IPVanish', false, false, NULL, 54, NULL)
    """)


def downgrade() -> None:
    op.drop_table('tv_feature_flags')
    op.drop_table('tv_app_configs')
    op.drop_table('room_tv_configs')
