"""Add music configuration and room audio tables.

Creates tables for music playback configuration:
- music_config: Global music playback settings (Music Assistant, Spotify accounts, genres)
- room_audio_configs: Per-room audio output configuration (stereo pairs, groups)
- Adds music_playback feature flag

Revision ID: 028
Revises: 027
Create Date: 2024-12-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = '028_add_music_config'
down_revision = '027_add_emerging_intents'
branch_labels = None
depends_on = None

# Default genre to artists mapping (20 genres x 10 artists each)
DEFAULT_GENRE_TO_ARTISTS = {
    "jazz": ["Miles Davis", "John Coltrane", "Thelonious Monk", "Charles Mingus", "Dave Brubeck", "Bill Evans", "Herbie Hancock", "Wayne Shorter", "Art Blakey", "Chet Baker"],
    "rock": ["Led Zeppelin", "Pink Floyd", "The Rolling Stones", "Queen", "AC/DC", "Guns N' Roses", "The Who", "Deep Purple", "Aerosmith", "Van Halen"],
    "classical": ["Ludwig van Beethoven", "Johann Sebastian Bach", "Wolfgang Amadeus Mozart", "Frederic Chopin", "Claude Debussy", "Pyotr Ilyich Tchaikovsky", "Johannes Brahms", "Antonio Vivaldi", "Franz Schubert", "Igor Stravinsky"],
    "electronic": ["Daft Punk", "Aphex Twin", "Boards of Canada", "Kraftwerk", "The Chemical Brothers", "Deadmau5", "Burial", "Four Tet", "Jon Hopkins", "Tycho"],
    "hip hop": ["Kendrick Lamar", "J. Cole", "Jay-Z", "Nas", "Kanye West", "Drake", "Tyler the Creator", "A Tribe Called Quest", "OutKast", "Wu-Tang Clan"],
    "country": ["Johnny Cash", "Willie Nelson", "Dolly Parton", "Merle Haggard", "George Strait", "Garth Brooks", "Hank Williams", "Patsy Cline", "Waylon Jennings", "Loretta Lynn"],
    "r&b": ["Marvin Gaye", "Stevie Wonder", "Whitney Houston", "Luther Vandross", "Aaliyah", "Usher", "D'Angelo", "Erykah Badu", "Frank Ocean", "The Weeknd"],
    "pop": ["Michael Jackson", "Prince", "Madonna", "Whitney Houston", "Beyonce", "Taylor Swift", "Bruno Mars", "Lady Gaga", "Justin Timberlake", "Rihanna"],
    "indie": ["Arcade Fire", "Bon Iver", "Vampire Weekend", "The National", "Fleet Foxes", "Tame Impala", "Mac DeMarco", "Phoebe Bridgers", "Sufjan Stevens", "Beach House"],
    "metal": ["Metallica", "Black Sabbath", "Iron Maiden", "Slayer", "Megadeth", "Pantera", "Judas Priest", "Motorhead", "Tool", "System of a Down"],
    "blues": ["B.B. King", "Muddy Waters", "Robert Johnson", "Howlin' Wolf", "John Lee Hooker", "Stevie Ray Vaughan", "Buddy Guy", "Eric Clapton", "Albert King", "Etta James"],
    "reggae": ["Bob Marley", "Peter Tosh", "Jimmy Cliff", "Toots and the Maytals", "Lee Scratch Perry", "Burning Spear", "Steel Pulse", "Black Uhuru", "Dennis Brown", "Gregory Isaacs"],
    "folk": ["Bob Dylan", "Joni Mitchell", "Simon & Garfunkel", "Neil Young", "James Taylor", "Cat Stevens", "Nick Drake", "Leonard Cohen", "Crosby Stills Nash & Young", "Joan Baez"],
    "soul": ["Aretha Franklin", "Otis Redding", "Sam Cooke", "Al Green", "Marvin Gaye", "Curtis Mayfield", "Donny Hathaway", "Bill Withers", "Gladys Knight", "The Temptations"],
    "funk": ["James Brown", "Parliament", "Funkadelic", "Sly and the Family Stone", "Earth Wind & Fire", "Bootsy Collins", "The Meters", "Rick James", "Ohio Players", "Kool and the Gang"],
    "alternative": ["Radiohead", "The Smiths", "R.E.M.", "Nirvana", "Pixies", "The Cure", "Depeche Mode", "New Order", "Sonic Youth", "Joy Division"],
    "punk": ["The Ramones", "The Clash", "Sex Pistols", "Bad Religion", "Dead Kennedys", "Black Flag", "Misfits", "Social Distortion", "Descendents", "Minor Threat"],
    "disco": ["Bee Gees", "Donna Summer", "Gloria Gaynor", "Chic", "KC and the Sunshine Band", "Village People", "Earth Wind & Fire", "Diana Ross", "ABBA", "Sister Sledge"],
    "techno": ["Carl Cox", "Richie Hawtin", "Jeff Mills", "Derrick May", "Juan Atkins", "Kevin Saunderson", "Adam Beyer", "Charlotte de Witte", "Amelie Lens", "Nina Kraviz"],
    "house": ["Frankie Knuckles", "Larry Heard", "Marshall Jefferson", "Kerri Chandler", "Masters at Work", "Disclosure", "Duke Dumont", "Fisher", "Chris Lake", "Green Velvet"]
}


def upgrade() -> None:
    """Create music_config and room_audio_configs tables."""

    # Create music_config table for global music settings
    op.create_table(
        'music_config',
        sa.Column('id', sa.Integer(), primary_key=True),

        # Music Assistant Connection
        sa.Column('music_assistant_url', sa.String(255)),
        sa.Column('music_assistant_enabled', sa.Boolean(), server_default='false'),

        # Spotify Account Pool (for independent multi-room playback)
        sa.Column('spotify_accounts', JSONB, server_default='[]'),

        # Default Playback Settings
        sa.Column('default_volume', sa.Float(), server_default='0.5'),
        sa.Column('default_radio_mode', sa.Boolean(), server_default='true'),
        sa.Column('default_provider', sa.String(50), server_default="'music_assistant'"),

        # Genre Mappings (multiple seed artists per genre)
        sa.Column('genre_to_artists', JSONB, server_default='{}'),

        # Seed Artist Selection Mode: random, first, rotate
        sa.Column('genre_seed_selection_mode', sa.String(20), server_default="'random'"),

        # Health Monitoring
        sa.Column('stream_health_check_enabled', sa.Boolean(), server_default='true'),
        sa.Column('stream_frozen_timeout_seconds', sa.Integer(), server_default='30'),
        sa.Column('auto_restart_frozen_streams', sa.Boolean(), server_default='true'),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Create room_audio_configs table for per-room speaker configuration
    op.create_table(
        'room_audio_configs',
        sa.Column('id', sa.Integer(), primary_key=True),

        # Room identification
        sa.Column('room_name', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(100)),

        # Speaker configuration
        sa.Column('speaker_type', sa.String(20), server_default="'single'"),  # single, stereo_pair, group
        sa.Column('primary_entity_id', sa.String(255), nullable=False),
        sa.Column('secondary_entity_id', sa.String(255)),  # For stereo pairs
        sa.Column('group_entity_ids', JSONB, server_default='[]'),  # For groups

        # Preferences
        sa.Column('default_volume', sa.Float(), server_default='0.5'),
        sa.Column('preferred_provider', sa.String(50), server_default="'music_assistant'"),
        sa.Column('use_radio_mode', sa.Boolean(), server_default='true'),

        # Status
        sa.Column('enabled', sa.Boolean(), server_default='true'),
        sa.Column('last_tested_at', sa.DateTime(timezone=True)),
        sa.Column('last_test_result', sa.String(50)),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Create indexes
    op.create_index('idx_room_audio_configs_room_name', 'room_audio_configs', ['room_name'])
    op.create_index('idx_room_audio_configs_enabled', 'room_audio_configs', ['enabled'])

    # Add music_playback feature flag (disabled by default until configured)
    op.execute("""
        INSERT INTO features (name, display_name, description, category, enabled, required, priority) VALUES
        ('music_playback', 'Music Playback', 'Voice-controlled music via Music Assistant. Requires Music Assistant addon in Home Assistant.', 'integration', false, false, 50)
        ON CONFLICT (name) DO NOTHING
    """)

    # Seed default music config with 20 genres x 10 artists
    import json
    genre_json = json.dumps(DEFAULT_GENRE_TO_ARTISTS).replace("'", "''")
    op.execute(f"""
        INSERT INTO music_config (genre_to_artists, genre_seed_selection_mode, default_volume)
        VALUES ('{genre_json}'::jsonb, 'random', 0.5)
    """)


def downgrade() -> None:
    """Remove music_config and room_audio_configs tables."""
    # Remove feature flag
    op.execute("DELETE FROM features WHERE name = 'music_playback'")

    # Drop room_audio_configs indexes and table
    op.drop_index('idx_room_audio_configs_enabled', table_name='room_audio_configs')
    op.drop_index('idx_room_audio_configs_room_name', table_name='room_audio_configs')
    op.drop_table('room_audio_configs')

    # Drop music_config table
    op.drop_table('music_config')
