"""Add memories.vector_status (pending until the vector store confirms).

Postgres is the source of truth for memories; a row's vector is only
served once app.services.memory_vectors.store_vector has written it. Rows
that predate the column start 'pending' and are embedded by the vector
store's automatic pending pass.

The boot-time compat ALTER in app.database adds the same column, so this
migration skips when the column is present. The memories table itself is
created by create_all, so it also skips when the table is absent. A NULL
vector_id (possible only on hand-built tables) gets a fresh UUID: the
column is the point id.

Revision ID: 062
Revises: 061
Create Date: 2026-09-29
"""
import uuid

import sqlalchemy as sa
from alembic import op

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def _columns(bind):
    return {c["name"] for c in sa.inspect(bind).get_columns("memories")}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("memories"):
        print("WARNING: memories table not found — skipping vector_status (migration 062).")
        return

    if bind.dialect.name == "postgresql":
        # Don't queue behind open transactions on memories (and make every
        # later memory query queue behind this one): give up after 5 s. Set
        # before the column reflection below, which waits on the same lock.
        op.execute("SET LOCAL lock_timeout = '5s'")

    if "vector_status" not in _columns(bind):
        op.add_column(
            "memories",
            sa.Column("vector_status", sa.String(16), nullable=False, server_default="pending"),
        )

    missing = bind.execute(sa.text("SELECT id FROM memories WHERE vector_id IS NULL")).fetchall()
    for (memory_id,) in missing:
        bind.execute(
            sa.text("UPDATE memories SET vector_id = :vid WHERE id = :id"),
            {"vid": str(uuid.uuid4()), "id": memory_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("memories") and "vector_status" in _columns(bind):
        op.drop_column("memories", "vector_status")
