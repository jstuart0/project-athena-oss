"""Conversation capture: route, channel, audience, outcome, withheld flag, debug detail.

Every addition is nullable or defaulted, so an orchestrator that still writes
the old column list keeps working during a rolling update.

- conversations.session_class: the caller class of the session id (owner,
  guest, public, other). The writer only appends to a conversation of the same
  class. There is no backfill: a row with a NULL class (everything that exists
  before this revision, and anything an older orchestrator writes during a
  rolling update) is adopted and stamped by the next turn written to it, so a
  stable id (SMS, device) isn't dropped by the partition guard.
- conversation_turns: route, channel, audience_class, outcome, text_withheld,
  debug_detail (JSONB).
- Two indexes for the Conversations page (audience filter, keyset paging),
  built CONCURRENTLY outside the DDL transaction so readers and the writer
  aren't blocked while they build. The ALTERs run under lock_timeout.
- Seeds conversation_debug_mode_enabled='false' and
  conversation_retention_days='30'. A value an operator already set is never
  overwritten, and the downgrade removes a seeded key only if it still holds
  the seeded value.
- Existing rows are not rewritten: redaction applies from this revision on.

Every statement is idempotent (IF [NOT] EXISTS), so up, down, up is safe.
A failed CREATE INDEX CONCURRENTLY leaves an invalid index of that name; drop
it and re-run (IF NOT EXISTS would otherwise skip it).

Revision ID: 064
Revises: 063
Create Date: 2026-10-10
"""
from alembic import op
from sqlalchemy import text

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None

DEBUG_KEY = "conversation_debug_mode_enabled"
RETENTION_KEY = "conversation_retention_days"
CLASS_INDEX = "idx_conversations_class_started_m"
KEYSET_INDEX = "idx_conversations_started_id_m"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))

    conn.execute(text("ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS session_class VARCHAR(16)"))
    for ddl in (
        "ADD COLUMN IF NOT EXISTS route VARCHAR(32)",
        "ADD COLUMN IF NOT EXISTS channel VARCHAR(8)",
        "ADD COLUMN IF NOT EXISTS audience_class VARCHAR(16)",
        "ADD COLUMN IF NOT EXISTS outcome VARCHAR(16)",
        "ADD COLUMN IF NOT EXISTS text_withheld BOOLEAN NOT NULL DEFAULT false",
        "ADD COLUMN IF NOT EXISTS debug_detail JSONB",
    ):
        conn.execute(text(f"ALTER TABLE IF EXISTS conversation_turns {ddl}"))

    conn.execute(
        text("""
            INSERT INTO system_settings (key, value, description, category)
            VALUES (:key, 'false',
                    'When enabled together with Analytics Mode, captured conversations also store tuning detail (model, tools, timings, validation).',
                    'privacy')
            ON CONFLICT (key) DO NOTHING
        """),
        {"key": DEBUG_KEY},
    )
    conn.execute(
        text("""
            INSERT INTO system_settings (key, value, description, category)
            VALUES (:key, '30',
                    'Days captured conversations are kept before they are deleted (1-3650).',
                    'privacy')
            ON CONFLICT (key) DO NOTHING
        """),
        {"key": RETENTION_KEY},
    )

    if _has_table(conn, "conversations"):
        with op.get_context().autocommit_block():
            conn = op.get_bind()
            # The transaction-local SET LOCAL above ended with the commit that
            # starts this block, so the wait for older transactions is bounded
            # here, per session, and reset afterwards.
            conn.execute(text("SET lock_timeout = '5s'"))
            try:
                _drop_invalid_index(conn, CLASS_INDEX)
                _drop_invalid_index(conn, KEYSET_INDEX)
                conn.execute(text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {CLASS_INDEX} ON conversations (session_class, started_at)"))
                conn.execute(text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {KEYSET_INDEX} ON conversations (started_at DESC, id DESC)"))
            finally:
                conn.execute(text("RESET lock_timeout"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text(f"DROP INDEX IF EXISTS {KEYSET_INDEX}"))
    conn.execute(text(f"DROP INDEX IF EXISTS {CLASS_INDEX}"))
    for column in ("debug_detail", "text_withheld", "outcome", "audience_class", "channel", "route"):
        conn.execute(text(f"ALTER TABLE IF EXISTS conversation_turns DROP COLUMN IF EXISTS {column}"))
    conn.execute(text("ALTER TABLE IF EXISTS conversations DROP COLUMN IF EXISTS session_class"))
    conn.execute(
        text("DELETE FROM system_settings WHERE key = :key AND value = 'false'"),
        {"key": DEBUG_KEY},
    )
    conn.execute(
        text("DELETE FROM system_settings WHERE key = :key AND value = '30'"),
        {"key": RETENTION_KEY},
    )


def _drop_invalid_index(conn, name: str) -> None:
    """A CONCURRENTLY build that failed leaves an invalid index; clear it so the retry rebuilds."""
    invalid = conn.execute(
        text("SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
             "WHERE c.relname = :n AND c.relnamespace = current_schema()::regnamespace AND NOT i.indisvalid"),
        {"n": name},
    ).scalar()
    if invalid:
        conn.execute(text(f"DROP INDEX IF EXISTS {name}"))


def _has_table(conn, table: str) -> bool:
    return bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}).scalar())
