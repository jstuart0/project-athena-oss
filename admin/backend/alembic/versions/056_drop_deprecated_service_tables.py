"""Drop deprecated service tables.

Revision ID: 056
Revises: 055
Create Date: 2026-05-08

Per Q-NEW3 from plan r1 codex review, this migration runs >=7 days after Phase 4
ships (Phase 4 at commit 52f0940 / reconcile 9aeaf49).

Ship date for Phase 5: 2026-05-15 = Phase 4 ship + 7 days.

DATE GUARD (codex r2 H-1): On PostgreSQL, upgrade() is a no-op until
2026-05-15 to preserve the 7-day rollback window promised in the plan.
SQLite test databases always run unconditionally (no production state at risk).
Override for staging/testing: set FORCE_PHASE_5_DROP=1 in the environment.

Operator runbook:
  1. On or after 2026-05-15, during a maintenance window, run against
     admin-DB (athena_admin):
       alembic upgrade head
     OR, for staging/CI before that date:
       FORCE_PHASE_5_DROP=1 alembic upgrade head
  2. The three deprecated tables will be permanently dropped:
       - service_registry_deprecated
       - athena_services_deprecated
       - server_configs_deprecated
  3. Downgrade is intentionally not supported — the pre-campaign tag
     pre-consolidate-service-registry-2026-05-07 is the only rollback path.

ATHENA-1 Campaign 4 Phase 5 — final drops.
"""

import os
import sys
from datetime import datetime, timezone

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = '056'
down_revision = '055'
branch_labels = None
depends_on = None

# Earliest date at which the drop is permitted on production PostgreSQL.
# = Phase 4 ship date (2026-05-08) + 7 days.  (codex r2 H-1 / Q-NEW3)
_EARLIEST_DROP_DATE = datetime(2026, 5, 15, tzinfo=timezone.utc)


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect != 'sqlite':
        # Production (PostgreSQL): enforce the 7-day window unless the operator
        # has explicitly opted out with FORCE_PHASE_5_DROP=1 (e.g. staging, CI).
        now = datetime.now(timezone.utc)
        if now < _EARLIEST_DROP_DATE and os.getenv("FORCE_PHASE_5_DROP") != "1":
            print(
                f"Migration 056: skipping drop — current date {now.date()} is before "
                f"{_EARLIEST_DROP_DATE.date()} (7-day rollback window).",
                file=sys.stderr,
            )
            print(
                "Set FORCE_PHASE_5_DROP=1 to override (e.g. for staging/testing).",
                file=sys.stderr,
            )
            return  # no-op; alembic still records the revision as applied

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
