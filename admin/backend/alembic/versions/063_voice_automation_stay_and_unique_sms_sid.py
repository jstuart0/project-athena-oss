"""Scope guest voice automations by stay; one sms_incoming row per Twilio SID.

- voice_automations.calendar_event_id: the stay a guest automation belongs
  to (nullable, indexed, cleared when the event is deleted). Guest names
  aren't unique (every Airbnb booking is "Airbnb Guest"), so a guest caller
  is scoped to its stay. Existing rows keep NULL, which no guest caller ever
  matches: they stay visible to the owner only.
- sms_incoming.twilio_sid gets a unique index. Duplicates that predate it
  (a Twilio retry answered twice) keep the SID on their earliest row; the
  later rows keep everything else and lose only the SID. That can't be
  undone by the downgrade, which only drops the index.

Both steps skip a table that doesn't exist and an index/column that already
does, so the migration is safe to re-run.

Revision ID: 063
Revises: 062
Create Date: 2026-09-30
"""
import sqlalchemy as sa
from alembic import op

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None

STAY_INDEX = "idx_voice_automations_stay"
SID_INDEX = "uq_sms_incoming_twilio_sid"
STAY_FK = "fk_voice_automations_calendar_event"


def _has_table(bind, table):
    return sa.inspect(bind).has_table(table)


def _columns(bind, table):
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table):
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    if postgres:
        # Don't queue behind open transactions on these tables.
        op.execute("SET LOCAL lock_timeout = '5s'")

    if _has_table(bind, "voice_automations"):
        if "calendar_event_id" not in _columns(bind, "voice_automations"):
            if postgres:
                op.add_column("voice_automations", sa.Column("calendar_event_id", sa.Integer(), nullable=True))
                if _has_table(bind, "calendar_events"):
                    op.create_foreign_key(STAY_FK, "voice_automations", "calendar_events",
                                          ["calendar_event_id"], ["id"], ondelete="SET NULL")
            else:
                with op.batch_alter_table("voice_automations") as batch:
                    batch.add_column(sa.Column("calendar_event_id", sa.Integer(), nullable=True))
                    batch.create_foreign_key(STAY_FK, "calendar_events", ["calendar_event_id"], ["id"],
                                             ondelete="SET NULL")
        if STAY_INDEX not in _indexes(bind, "voice_automations"):
            op.create_index(STAY_INDEX, "voice_automations", ["calendar_event_id"])
    else:
        print("WARNING: voice_automations table not found — skipping calendar_event_id (migration 063).")

    if _has_table(bind, "sms_incoming"):
        if SID_INDEX not in _indexes(bind, "sms_incoming"):
            bind.execute(sa.text(
                "UPDATE sms_incoming SET twilio_sid = NULL "
                "WHERE twilio_sid IS NOT NULL AND id NOT IN ("
                "  SELECT MIN(id) FROM sms_incoming WHERE twilio_sid IS NOT NULL GROUP BY twilio_sid)"
            ))
            op.create_index(SID_INDEX, "sms_incoming", ["twilio_sid"], unique=True)
    else:
        print("WARNING: sms_incoming table not found — skipping the unique twilio_sid (migration 063).")


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "sms_incoming") and SID_INDEX in _indexes(bind, "sms_incoming"):
        op.drop_index(SID_INDEX, table_name="sms_incoming")
    if _has_table(bind, "voice_automations"):
        if STAY_INDEX in _indexes(bind, "voice_automations"):
            op.drop_index(STAY_INDEX, table_name="voice_automations")
        if "calendar_event_id" in _columns(bind, "voice_automations"):
            if bind.dialect.name == "postgresql":
                fks = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys("voice_automations")}
                if STAY_FK in fks:
                    op.drop_constraint(STAY_FK, "voice_automations", type_="foreignkey")
                op.drop_column("voice_automations", "calendar_event_id")
            else:
                with op.batch_alter_table("voice_automations") as batch:
                    batch.drop_column("calendar_event_id")
