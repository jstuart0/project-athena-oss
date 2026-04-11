"""Add conversation analytics tables and analytics_mode_enabled setting.

Creates the conversations, conversation_turns, and turn_evaluations tables
used by the orchestrator (write path) and admin Conversations page (read path).
Also adds the analytics_mode_enabled system setting, which is the admin UI
toggle that gates whether the orchestrator persists conversation data.

When analytics_mode_enabled is false (the default), the orchestrator skips
all writes to these tables. Users enable it via Admin UI > Privacy settings.

Revision ID: 052
Revises: 051
Create Date: 2026-04-10
"""

from alembic import op
from sqlalchemy import text

revision = '052'
down_revision = '051'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # --- Analytics tables (orchestrator writes, admin backend reads) ---

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS conversations (
            id              VARCHAR(36)     NOT NULL PRIMARY KEY,
            session_id      VARCHAR(100)    NOT NULL,
            room            VARCHAR(100),
            user_mode       VARCHAR(20),
            interface_type  VARCHAR(20),
            started_at      TIMESTAMP       NOT NULL DEFAULT NOW(),
            ended_at        TIMESTAMP,
            turn_count      INTEGER         NOT NULL,
            total_tokens    INTEGER,
            created_at      TIMESTAMP       NOT NULL DEFAULT NOW(),
            CONSTRAINT ix_conversations_session_id UNIQUE (session_id)
        )
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_conversations_started_m
            ON conversations (started_at)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_conversations_room_m
            ON conversations (room)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_conversations_session_m
            ON conversations (session_id)
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id                        VARCHAR(36)  NOT NULL PRIMARY KEY,
            conversation_id           VARCHAR(36)  NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            turn_number               INTEGER      NOT NULL,
            request_id                VARCHAR(100),
            query_text                TEXT         NOT NULL,
            response_text             TEXT,
            intent                    VARCHAR(100),
            confidence                NUMERIC,
            model_used                VARCHAR(100),
            rag_tools_used            JSONB,
            response_time_ms          INTEGER,
            tokens_generated          INTEGER,
            tokens_per_second         NUMERIC,
            validation_passed         BOOLEAN,
            error_message             TEXT,
            source                    VARCHAR(50)  NOT NULL,
            complexity                VARCHAR(50),
            is_fallback               BOOLEAN,
            base_knowledge_populated  BOOLEAN,
            node_timings_ms           JSONB,
            created_at                TIMESTAMP    NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_turns_conversation_m
            ON conversation_turns (conversation_id)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_turns_created_m
            ON conversation_turns (created_at)
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS turn_evaluations (
            id                    VARCHAR(36)  NOT NULL PRIMARY KEY,
            turn_id               VARCHAR(36)  NOT NULL
                REFERENCES conversation_turns(id) ON DELETE CASCADE,
            rating                INTEGER      NOT NULL,
            notes                 TEXT,
            evaluated_by_user_id  INTEGER,
            evaluated_by_username VARCHAR(100),
            created_at            TIMESTAMP    NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_evaluations_turn_m
            ON turn_evaluations (turn_id)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_evaluations_rating_m
            ON turn_evaluations (rating)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_evaluations_created_m
            ON turn_evaluations (created_at)
    """))

    # --- Admin UI toggle ---

    conn.execute(text("""
        INSERT INTO system_settings (key, value, description, category)
        VALUES (
            'analytics_mode_enabled',
            'false',
            'When enabled, all conversations are persisted to the database for review and quality evaluation via the Conversations page. Best-effort capture: turns in-flight at service restart may not be recorded.',
            'privacy'
        )
        ON CONFLICT (key) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS turn_evaluations CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS conversation_turns CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS conversations CASCADE"))
    conn.execute(text(
        "DELETE FROM system_settings WHERE key = 'analytics_mode_enabled'"
    ))
