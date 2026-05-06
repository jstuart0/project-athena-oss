"""Clear legacy maintainer-IP defaults from gateway_config rows.

Follow-up to bob:1 / commit 4f6b159 (Campaign 1 / ATHENA-11 Phase 5).

The column-default fix (4f6b159) set GatewayConfig.orchestrator_url and
ollama_fallback_url to default='' going forward. Existing rows in deployed
DBs may still carry the maintainer's homelab IPs. This migration clears
them (exact match and trailing-slash variant) with idempotent UPDATEs.

NOTE: SQLAlchemy 2.x form — params dict is the SECOND POSITIONAL arg to
execute(), not kwargs. The 1.x form `execute(text, legacy=...)` will fail
in 2.x.

Revision ID: 053
Revises: 052
Create Date: 2026-05-06
"""

from alembic import op
import sqlalchemy as sa

revision = "053"
down_revision = "052"  # primary chain only; do NOT chain off 004a (legacy branch head)
branch_labels = None
depends_on = None

LEGACY_ORCH = "http://192.168.10.167:8001"
LEGACY_OLLAMA = "http://192.168.10.167:11434"


def upgrade() -> None:
    bind = op.get_bind()
    # orchestrator_url: clear exact match and trailing-slash variant
    bind.execute(
        sa.text(
            "UPDATE gateway_config SET orchestrator_url = '' "
            "WHERE orchestrator_url = :legacy"
        ),
        {"legacy": LEGACY_ORCH},
    )
    bind.execute(
        sa.text(
            "UPDATE gateway_config SET orchestrator_url = '' "
            "WHERE orchestrator_url = :legacy_slash"
        ),
        {"legacy_slash": LEGACY_ORCH + "/"},
    )
    # ollama_fallback_url: clear exact match and trailing-slash variant
    bind.execute(
        sa.text(
            "UPDATE gateway_config SET ollama_fallback_url = '' "
            "WHERE ollama_fallback_url = :legacy"
        ),
        {"legacy": LEGACY_OLLAMA},
    )
    bind.execute(
        sa.text(
            "UPDATE gateway_config SET ollama_fallback_url = '' "
            "WHERE ollama_fallback_url = :legacy_slash"
        ),
        {"legacy_slash": LEGACY_OLLAMA + "/"},
    )


def downgrade() -> None:
    # No-op: the maintainer-specific IP cannot be safely restored on an
    # arbitrary deployer's DB. Per Campaign 1 (ATHENA-11) Phase 5 plan.
    pass
