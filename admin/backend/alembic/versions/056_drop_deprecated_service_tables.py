"""Drop deprecated service tables.

Revision ID: 056
Revises: 055
Create Date: 2026-05-08

Per Q-NEW3 from plan r1 codex review, this migration runs >=7 days after Phase 4
ships (Phase 4 at commit 52f0940 / reconcile 9aeaf49).

Ship date for Phase 5: 2026-05-15 = Phase 4 ship + 7 days.

Operator runbook:
  1. During a maintenance window, run against admin-DB (athena_admin):
       alembic upgrade head
  2. The three deprecated tables will be permanently dropped:
       - service_registry_deprecated
       - athena_services_deprecated
       - server_configs_deprecated
  3. Downgrade is intentionally not supported — the pre-campaign tag
     pre-consolidate-service-registry-2026-05-07 is the only rollback path.

ATHENA-1 Campaign 4 Phase 5 — final drops.
"""

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = '056'
down_revision = '055'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == 'sqlite':
        # SQLite supports DROP TABLE IF EXISTS natively.
        conn.execute(text("DROP TABLE IF EXISTS service_registry_deprecated"))
        conn.execute(text("DROP TABLE IF EXISTS athena_services_deprecated"))
        conn.execute(text("DROP TABLE IF EXISTS server_configs_deprecated"))
    else:
        # PostgreSQL: drop with CASCADE to handle any residual FK references.
        # Order: child table first (service_registry_deprecated has FK to server_configs_deprecated).
        conn.execute(text("DROP TABLE IF EXISTS service_registry_deprecated CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS athena_services_deprecated CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS server_configs_deprecated CASCADE"))


def downgrade() -> None:
    raise NotImplementedError(
        "Forward-only — Campaign 4 / ATHENA-1 final drops; "
        "pre-campaign tag pre-consolidate-service-registry-2026-05-07 is the only rollback"
    )
