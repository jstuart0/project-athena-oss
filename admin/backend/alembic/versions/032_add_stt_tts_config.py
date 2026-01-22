"""Add STT and TTS configuration tables.

STT Models: Whisper model options (tiny, base, small)
TTS Voices: Piper voice options (lessac, amy, ryan, etc.)
Voice Service Config: Host/port settings for services

Revision ID: 032_add_stt_tts_config
Revises: 031_add_apple_tv_config
Create Date: 2025-12-18
"""
from alembic import op
import sqlalchemy as sa

revision = '032_add_stt_tts_config'
down_revision = '031_add_apple_tv_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # STT Models table
    op.create_table(
        'stt_models',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('engine', sa.String(50), nullable=False, server_default='faster-whisper'),
        sa.Column('model_name', sa.String(100), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('size_mb', sa.Integer()),
        sa.Column('is_active', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_index('idx_stt_models_is_active', 'stt_models', ['is_active'])

    # TTS Voices table
    op.create_table(
        'tts_voices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('engine', sa.String(50), nullable=False, server_default='piper'),
        sa.Column('voice_id', sa.String(100), nullable=False),
        sa.Column('language', sa.String(10), server_default='en'),
        sa.Column('quality', sa.String(20), server_default='medium'),
        sa.Column('description', sa.Text()),
        sa.Column('sample_url', sa.String(500)),
        sa.Column('is_active', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_index('idx_tts_voices_is_active', 'tts_voices', ['is_active'])

    # Voice service configuration
    op.create_table(
        'voice_service_config',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('service_type', sa.String(10), nullable=False),  # 'stt' or 'tts'
        sa.Column('host', sa.String(100), nullable=False),
        sa.Column('wyoming_port', sa.Integer(), nullable=False),
        sa.Column('rest_port', sa.Integer()),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('service_type'),
    )

    # Seed default STT models
    op.execute("""
        INSERT INTO stt_models (name, display_name, engine, model_name, description, size_mb, is_active)
        VALUES
        ('tiny-int8', 'Whisper Tiny (Fast)', 'faster-whisper', 'tiny-int8', 'Fastest model, good for simple commands. ~2-3x realtime.', 73, true),
        ('tiny.en', 'Whisper Tiny English', 'faster-whisper', 'tiny.en', 'English-only tiny model, slightly better accuracy.', 73, false),
        ('base-int8', 'Whisper Base (Balanced)', 'faster-whisper', 'base-int8', 'Better accuracy, slightly slower. ~1.5-2x realtime.', 290, false),
        ('base.en', 'Whisper Base English', 'faster-whisper', 'base.en', 'English-only base model, best accuracy for English.', 290, false),
        ('small-int8', 'Whisper Small (Accurate)', 'faster-whisper', 'small-int8', 'High accuracy, slower. Best for complex speech.', 967, false)
    """)

    # Seed default TTS voices
    op.execute("""
        INSERT INTO tts_voices (name, display_name, engine, voice_id, language, quality, description, is_active)
        VALUES
        ('lessac-medium', 'Lessac (Medium)', 'piper', 'en_US-lessac-medium', 'en', 'medium', 'Clear American English voice. Good default choice.', true),
        ('amy-medium', 'Amy (Medium)', 'piper', 'en_US-amy-medium', 'en', 'medium', 'Natural female American voice.', false),
        ('amy-low', 'Amy (Low)', 'piper', 'en_US-amy-low', 'en', 'low', 'Faster, lower quality female voice.', false),
        ('ryan-medium', 'Ryan (Medium)', 'piper', 'en_US-ryan-medium', 'en', 'medium', 'Natural male American voice.', false),
        ('ryan-high', 'Ryan (High Quality)', 'piper', 'en_US-ryan-high', 'en', 'high', 'High quality male voice. Slower synthesis.', false),
        ('ryan-low', 'Ryan (Low)', 'piper', 'en_US-ryan-low', 'en', 'low', 'Faster, lower quality male voice.', false),
        ('joe-medium', 'Joe (Medium)', 'piper', 'en_US-joe-medium', 'en', 'medium', 'Another male American voice option.', false),
        ('kusal-medium', 'Kusal (Medium)', 'piper', 'en_US-kusal-medium', 'en', 'medium', 'Male American voice with clear diction.', false),
        ('libritts-high', 'LibriTTS (High Quality)', 'piper', 'en_US-libritts-high', 'en', 'high', 'High quality multi-speaker model.', false),
        ('ljspeech-high', 'LJSpeech (High Quality)', 'piper', 'en_US-ljspeech-high', 'en', 'high', 'Classic high-quality female voice.', false)
    """)

    # Seed default service config (Mac mini)
    op.execute("""
        INSERT INTO voice_service_config (service_type, host, wyoming_port, rest_port, enabled)
        VALUES
        ('stt', 'localhost', 10300, 10301, true),
        ('tts', 'localhost', 10200, 10201, true)
    """)


def downgrade() -> None:
    op.drop_table('voice_service_config')
    op.drop_table('tts_voices')
    op.drop_table('stt_models')
