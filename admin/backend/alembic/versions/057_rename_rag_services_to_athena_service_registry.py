"""Rename rag_services to athena_service_registry.

Revision ID: 057
Revises: 056
Create Date: 2026-05-11

ATHENA-17: Post-consolidation, the rag_services table holds non-RAG services
(Redis, gateway, Ollama) in addition to RAG connectors. Rename to
athena_service_registry to reflect schema reality.

Operations (atomic, single transaction):
  1. Rename table:   rag_services → athena_service_registry
     (PostgreSQL + SQLite — both support ALTER TABLE RENAME TO since SQLite 3.25.)
  2. Defensively rename indexes via `ALTER INDEX IF EXISTS` (PostgreSQL ONLY).
     Live athena_prod has only `idx_rag_services_{name,enabled}` from the
     four `idx_*`/`ix_*` indexes declared in models.py. Both
     `idx_rag_services_service_type` and `ix_rag_services_name` are
     declared in models.py but never made it to prod — they were added
     to the model after the table was already in production and no
     CREATE INDEX migration was authored. (Empirically corrected: when a
     column declares both `unique=True` AND `index=True` — as
     RagService.name does at models.py:2287 — SQLAlchemy emits BOTH
     `*_name_key` AND `ix_*_name`; `unique=True` does NOT suppress the
     implicit `ix_*` index.) The `IF EXISTS` clause makes every absent
     index a silent no-op. THIS MIGRATION DOES NOT CREATE THE MISSING
     INDEXES — its scope is rename, not convergence. Bringing prod into
     model alignment is a separate, future migration (out of scope).
  3. Rename constraint-backed indexes via `ALTER TABLE ... RENAME CONSTRAINT`
     wrapped in `DO $$ ... $$` existence checks against `pg_constraint`:
       - `rag_services_pkey`     → `athena_service_registry_pkey`
       - `rag_services_name_key` → `athena_service_registry_name_key`
     These are real constraints (not just indexes); using `ALTER INDEX` on
     them is semantically wrong even though it appears to work in pg_indexes.
     PostgreSQL has no native `RENAME CONSTRAINT IF EXISTS`, hence the DO block.
  4. FK constraint on rag_connectors.service_id is NOT renamed (see plan D2).
     PostgreSQL auto-updates the FK's referred_table on rename via OID; the
     constraint name follows the child-table convention and remains correct.
     SQLite enforces FKs by recompiling triggers on table rename; behavior is
     equivalent.

Pre-existing schema state on athena_prod (verified 2026-05-11):
    SELECT indexname FROM pg_indexes WHERE tablename='rag_services';
    → idx_rag_services_enabled
    → idx_rag_services_name
    → rag_services_name_key   (constraint-backed)
    → rag_services_pkey       (constraint-backed)

Decoupled from migration 056 (deprecated-table hard drop, date-gated body).
On athena_prod, 056 is already recorded as applied (with a no-op body
because the date gate was false at the time of application). Alembic does
NOT re-run a revision's upgrade body once recorded, so 056's body will not
fire later regardless of the current date. The _deprecated_* tables on
athena_prod will remain in the schema indefinitely; that is orthogonal to
ATHENA-17. Running `alembic upgrade head` for this deploy executes ONLY 057.

Downgrade: reverses table rename, index renames, and constraint renames.
The migration is purely metadata-renames; no data is moved or transformed.
"""
from alembic import op
from sqlalchemy import text  # noqa: F401 — imported for op.execute() callers

# revision identifiers, used by Alembic.
revision = '057'
down_revision = '056'
branch_labels = None
depends_on = None

# Every entry is wrapped with `IF EXISTS` in the SQL — entries that aren't
# present on a given DB become silent no-ops. Listed in declaration order;
# whether each actually fires depends on the DB's pre-existing schema.
_INDEX_RENAMES = [
    # Three indexes declared in RagService.__table_args__ (models.py:2332-2334).
    # athena_prod has only the first two; service_type was declared after the
    # table was already in prod and never migrated.
    ('idx_rag_services_name',         'idx_athena_service_registry_name'),
    ('idx_rag_services_enabled',      'idx_athena_service_registry_enabled'),
    ('idx_rag_services_service_type', 'idx_athena_service_registry_service_type'),
    # Implicit `ix_*` index that SQLAlchemy emits because RagService.name
    # has `index=True` (in addition to `unique=True`). When both flags are
    # set, SA emits BOTH the unique constraint AND the implicit index —
    # `unique=True` does NOT suppress `index=True`. On a fresh install from
    # current models.py this index exists and renames to its new form.
    # On athena_prod it does NOT exist (never migrated post-model edit) and
    # the IF EXISTS makes the rename a silent no-op. This migration does
    # NOT create the missing index on prod — that is out of scope.
    ('ix_rag_services_name',          'ix_athena_service_registry_name'),
]

# Constraint-backed renames. Wrapped in DO $$ blocks because PostgreSQL has
# no `RENAME CONSTRAINT IF EXISTS`. Applied AFTER `op.rename_table` so the
# table reference inside the DO block uses the new name.
_CONSTRAINT_RENAMES = [
    ('rag_services_pkey',     'athena_service_registry_pkey'),
    ('rag_services_name_key', 'athena_service_registry_name_key'),
]


def _rename_constraint_if_exists(table: str, old: str, new: str) -> None:
    """Emit `ALTER TABLE ... RENAME CONSTRAINT` wrapped in a pg_constraint
    existence check, since PostgreSQL has no IF EXISTS form for this op."""
    op.execute(
        text(
            f"""
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = '{old}'
              AND conrelid = '{table}'::regclass
          ) THEN
            ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};
          END IF;
        END
        $$;
        """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == 'postgresql'

    op.rename_table('rag_services', 'athena_service_registry')

    # SQLite has no ALTER INDEX RENAME TO statement; skip everything below.
    if not is_postgres:
        return

    for old, new in _INDEX_RENAMES:
        op.execute(text(f'ALTER INDEX IF EXISTS {old} RENAME TO {new}'))

    for old, new in _CONSTRAINT_RENAMES:
        _rename_constraint_if_exists('athena_service_registry', old, new)


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == 'postgresql'

    if is_postgres:
        # Undo constraint renames first (table is still named
        # `athena_service_registry` at this point).
        for old, new in _CONSTRAINT_RENAMES:
            _rename_constraint_if_exists('athena_service_registry', new, old)

        for old, new in _INDEX_RENAMES:
            op.execute(text(f'ALTER INDEX IF EXISTS {new} RENAME TO {old}'))

    op.rename_table('athena_service_registry', 'rag_services')
