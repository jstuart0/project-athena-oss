"""Clear legacy maintainer OIDC secrets from the secrets table.

Follow-up to bob:1 / commit 4f6b159 + migration 053 (Campaign 1 / ATHENA-11).

The column-default fix (5403a8a) removed the maintainer's OIDC redirect URI
from the admin/frontend/index.html form value. However, any deployment that
saved OIDC settings before that commit may still have the maintainer's values
stored as encrypted rows in the `secrets` table.

This migration decrypts each `oidc_redirect_uri` and `oidc_provider_url`
secrets row and deletes any row whose plaintext matches the known legacy
maintainer literals:
  - https://athena-admin.xmojo.net/api/auth/callback  (oidc_redirect_uri)
  - https://auth.xmojo.net/application/o/athena-admin/ (oidc_provider_url)

ENCRYPTION_KEY pre-check (D2 / bob C1 + librarian + xander H1):
  admin/backend/app/utils/encryption.py:21-24 silently generates a throwaway
  Fernet key when ENCRYPTION_KEY is unset. Without the explicit check below,
  decrypt_value would raise InvalidToken on every row; the loop would silently
  skip all rows; and the migration would print "✓ Cleared 0 rows" with a clean
  exit code — leaving stale rows in place with no signal to the deployer.
  The guard is an explicit os.getenv check, NOT a try/except ImportError
  (the module always imports successfully).

DRY_RUN_058 opt-in:
  Set DRY_RUN_058=true to preview which rows would be deleted without applying
  any changes. Safe to run against production data before the destructive run.

sys.path note:
  Must be run with admin/backend on sys.path (default when invoked via
  `cd admin/backend && alembic upgrade head`).

NOTE: SQLAlchemy 2.x form — params dict is the SECOND POSITIONAL arg to
execute(), not kwargs. The 1.x form `execute(text, legacy=...)` will fail
in 2.x.

Revision ID: 058
Revises: 057
Create Date: 2026-05-15

ATHENA-11 (C1 campaign — bob:1 follow-up data migration)
"""

import os
import sys
from pathlib import Path

from cryptography.fernet import InvalidToken

# sys.path bootstrap (bob H2):
# Migration 058 is the FIRST migration to import from app.*. Alembic runs the
# migration file directly; `app.*` is only resolvable if admin/backend is on
# sys.path. When invoked as `cd admin/backend && alembic upgrade head` this is
# automatic via the CWD. The insert below is a belt-and-suspenders guard for
# non-standard invocation paths (e.g., `alembic -c /abs/path/alembic.ini`).
_admin_backend = Path(__file__).resolve().parents[2]  # alembic/versions/058… → admin/backend
if str(_admin_backend) not in sys.path:
    sys.path.insert(0, str(_admin_backend))

from alembic import op
import sqlalchemy as sa

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None

LEGACY_REDIRECT = "https://athena-admin.xmojo.net/api/auth/callback"
LEGACY_PROVIDER = "https://auth.xmojo.net/application/o/athena-admin/"


def _matches(decrypted: str, legacy: str) -> bool:
    """True iff decrypted equals legacy, legacy without trailing slash, or legacy with trailing slash."""
    return (
        decrypted == legacy
        or decrypted == legacy.rstrip("/")
        or decrypted == legacy.rstrip("/") + "/"
    )


def upgrade() -> None:
    # ENCRYPTION_KEY pre-check (D2 / bob C1 + librarian + xander H1):
    # Do NOT use try/except ImportError — encryption.py always imports
    # successfully. The danger is a throwaway key silently failing every
    # decrypt, producing "✓ Cleared 0 rows" while stale rows persist.
    if not os.getenv("ENCRYPTION_KEY"):
        print(
            "WARNING: ENCRYPTION_KEY not set — skipping legacy OIDC cleanup (migration 058). "
            "Re-run `alembic upgrade head` after setting ENCRYPTION_KEY."
        )
        return

    from app.utils.encryption import decrypt_value

    dry_run = os.getenv("DRY_RUN_058", "").lower() in ("true", "1", "yes")

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, service_name, encrypted_value FROM secrets "
            "WHERE service_name IN ('oidc_redirect_uri', 'oidc_provider_url')"
        )
    ).fetchall()

    cleared = 0
    would_delete = 0

    for row in rows:
        try:
            plaintext = decrypt_value(row.encrypted_value)
        except (InvalidToken, ValueError):
            # Decryption failure: key rotated or value corrupted — leave alone.
            # Log at DEBUG level so operators can identify skipped rows if needed.
            print(
                f"DEBUG: migration 058 — could not decrypt row id={row.id} "
                f"(service_name={row.service_name}); skipping"
            )
            continue

        legacy = LEGACY_REDIRECT if row.service_name == "oidc_redirect_uri" else LEGACY_PROVIDER
        if _matches(plaintext, legacy):
            if dry_run:
                print(
                    f"[DRY-RUN] Would delete row id={row.id} "
                    f"(service_name={row.service_name}, decrypted_value={plaintext})"
                )
                would_delete += 1
            else:
                bind.execute(
                    sa.text("DELETE FROM secrets WHERE id = :id"),
                    {"id": row.id},
                )
                cleared += 1

    if dry_run:
        print(
            f"[DRY-RUN] Would clear {would_delete} legacy OIDC secret rows. "
            "Re-run without DRY_RUN_058 to apply."
        )
    else:
        print(f"✓ Cleared {cleared} legacy OIDC secret rows")
        print(
            "NOTE: If your deployment legitimately used xmojo.net values, "
            "re-save OIDC settings via the admin UI before restarting admin-backend."
        )


def downgrade() -> None:
    # No-op: the maintainer-specific values cannot be safely restored on an
    # arbitrary deployer's DB. Per Campaign 1 (ATHENA-11) Phase 5 plan.
    pass
