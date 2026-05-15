"""ATHENA-11 C1: regression tests for migration 058 (clear legacy OIDC secrets).

Tests run against SQLite in-memory fixtures. No external service required.

All tests synthesize the `secrets` table directly via SQLAlchemy and call
the migration's upgrade() function in-process using the same scratch-engine
pattern established in test_migration_057_rename.py.

ENCRYPTION_KEY isolation:
  Each test that exercises decrypt logic sets ENCRYPTION_KEY in the
  subprocess/monkeypatch scope and constructs the encrypted fixture using the
  same key. Tests that verify the "unset" path explicitly remove ENCRYPTION_KEY
  from the environment so the pre-check fires.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

# ── path bootstrap ──────────────────────────────────────────────────────────
# Ensure admin/backend is on sys.path so app.* imports work during test runs
# invoked from the repo root (e.g., `pytest admin/backend/tests/`).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADMIN_BACKEND = _REPO_ROOT / "admin" / "backend"
if str(_ADMIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(_ADMIN_BACKEND))

ALEMBIC_INI = _ADMIN_BACKEND / "alembic.ini"

# ── constants ────────────────────────────────────────────────────────────────
LEGACY_REDIRECT = "https://athena-admin.xmojo.net/api/auth/callback"
LEGACY_PROVIDER = "https://auth.xmojo.net/application/o/athena-admin/"
CUSTOM_VALUE = "https://auth.example.com/application/o/my-app/"

# ── fixtures / helpers ───────────────────────────────────────────────────────

_SECRETS_DDL = """\
CREATE TABLE secrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name VARCHAR(100) NOT NULL,
    encrypted_value TEXT NOT NULL
)"""

_ALEMBIC_VERSION_DDL = """\
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL
)"""


def _make_engine(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'm058.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(_SECRETS_DDL))
        conn.execute(text(_ALEMBIC_VERSION_DDL))
        conn.execute(text("INSERT INTO alembic_version VALUES ('057')"))
    return engine, db_url


def _scratch_alembic_config(db_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location",
        str((ALEMBIC_INI.parent / "alembic").resolve()),
    )
    return cfg


def _run_upgrade(monkeypatch, db_url: str, env_overrides: dict | None = None) -> Config:
    """Run alembic upgrade 058 with DATABASE_URL hidden so env.py uses cfg's URL."""
    cfg = _scratch_alembic_config(db_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
    try:
        command.upgrade(cfg, "058")
    finally:
        monkeypatch.undo()
    return cfg


def _encrypt_with_key(plaintext: str, enc_key: str) -> str:
    """Encrypt plaintext using the given ENCRYPTION_KEY, returning the
    base64-encoded ciphertext as stored in the secrets table."""
    import base64
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt_b64 = base64.urlsafe_b64encode(b"athena-admin-default-salt-change-me").decode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=base64.urlsafe_b64decode(salt_b64.encode()),
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(enc_key.encode()))
    f = Fernet(key)
    encrypted_bytes = f.encrypt(plaintext.encode())
    return base64.urlsafe_b64encode(encrypted_bytes).decode()


def _row_count(engine, service_name: str) -> int:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM secrets WHERE service_name = :sn"),
            {"sn": service_name},
        )
        return result.scalar()


def _reload_encryption_module():
    """Force-reload encryption.py so module-level ENCRYPTION_KEY capture
    picks up the current environment value in tests that mutate os.environ."""
    for k in ["app.utils.encryption", "app.utils", "app"]:
        sys.modules.pop(k, None)


# ── tests ────────────────────────────────────────────────────────────────────

class TestMigration058:

    def test_migration_058_skips_when_encryption_key_unset(
        self, tmp_path, monkeypatch, capsys
    ):
        """When ENCRYPTION_KEY is not set, upgrade prints a WARNING and returns
        without deleting any rows (pre-check must fire before any decrypt attempt).
        """
        enc_key = "test-key-for-migration-058"
        engine, db_url = _make_engine(tmp_path)

        # Insert a row that would be deleted if the key were set.
        encrypted = _encrypt_with_key(LEGACY_REDIRECT, enc_key)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_redirect_uri', :v)"),
                {"v": encrypted},
            )

        # Remove ENCRYPTION_KEY and reload the encryption module so the
        # module-level capture sees the unset state.
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(monkeypatch, db_url, env_overrides={"ENCRYPTION_KEY": None})

        captured = capsys.readouterr()
        assert "WARNING: ENCRYPTION_KEY not set" in captured.out
        # Row must still be present — migration did NOT delete it.
        assert _row_count(engine, "oidc_redirect_uri") == 1

    def test_migration_058_clears_legacy_redirect_uri(
        self, tmp_path, monkeypatch, capsys
    ):
        """A row with the legacy redirect URI is deleted when ENCRYPTION_KEY is set."""
        enc_key = "test-key-for-migration-058"
        engine, db_url = _make_engine(tmp_path)

        encrypted = _encrypt_with_key(LEGACY_REDIRECT, enc_key)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_redirect_uri', :v)"),
                {"v": encrypted},
            )

        assert _row_count(engine, "oidc_redirect_uri") == 1

        monkeypatch.setenv("ENCRYPTION_KEY", enc_key)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(monkeypatch, db_url, env_overrides={"ENCRYPTION_KEY": enc_key})

        captured = capsys.readouterr()
        assert "✓ Cleared 1 legacy OIDC secret rows" in captured.out
        assert _row_count(engine, "oidc_redirect_uri") == 0

    def test_migration_058_clears_legacy_provider_url(
        self, tmp_path, monkeypatch, capsys
    ):
        """A row with the legacy provider URL is deleted when ENCRYPTION_KEY is set."""
        enc_key = "test-key-for-migration-058"
        engine, db_url = _make_engine(tmp_path)

        encrypted = _encrypt_with_key(LEGACY_PROVIDER, enc_key)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_provider_url', :v)"),
                {"v": encrypted},
            )

        assert _row_count(engine, "oidc_provider_url") == 1

        monkeypatch.setenv("ENCRYPTION_KEY", enc_key)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(monkeypatch, db_url, env_overrides={"ENCRYPTION_KEY": enc_key})

        captured = capsys.readouterr()
        assert "✓ Cleared 1 legacy OIDC secret rows" in captured.out
        assert _row_count(engine, "oidc_provider_url") == 0

    def test_migration_058_dry_run_does_not_delete(
        self, tmp_path, monkeypatch, capsys
    ):
        """DRY_RUN_058=true prints [DRY-RUN] output but deletes nothing."""
        enc_key = "test-key-for-migration-058"
        engine, db_url = _make_engine(tmp_path)

        encrypted = _encrypt_with_key(LEGACY_REDIRECT, enc_key)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_redirect_uri', :v)"),
                {"v": encrypted},
            )

        monkeypatch.setenv("ENCRYPTION_KEY", enc_key)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(
            monkeypatch,
            db_url,
            env_overrides={"ENCRYPTION_KEY": enc_key, "DRY_RUN_058": "true"},
        )

        captured = capsys.readouterr()
        assert "[DRY-RUN]" in captured.out
        assert "Would delete row" in captured.out
        # Row must still be present.
        assert _row_count(engine, "oidc_redirect_uri") == 1

    def test_migration_058_preserves_non_matching(
        self, tmp_path, monkeypatch, capsys
    ):
        """Rows with custom (deployer-entered) values are NOT deleted."""
        enc_key = "test-key-for-migration-058"
        engine, db_url = _make_engine(tmp_path)

        encrypted = _encrypt_with_key(CUSTOM_VALUE, enc_key)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_provider_url', :v)"),
                {"v": encrypted},
            )

        monkeypatch.setenv("ENCRYPTION_KEY", enc_key)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(monkeypatch, db_url, env_overrides={"ENCRYPTION_KEY": enc_key})

        captured = capsys.readouterr()
        assert "✓ Cleared 0 legacy OIDC secret rows" in captured.out
        assert _row_count(engine, "oidc_provider_url") == 1

    def test_migration_058_per_row_decryption_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        """A row encrypted with a DIFFERENT key is skipped (DEBUG log); a
        row encrypted with the CURRENT key is still processed normally."""
        current_key = "current-encryption-key"
        other_key = "a-different-old-key-value"
        engine, db_url = _make_engine(tmp_path)

        # Row 1: encrypted with a different key (unreadable) — should be skipped.
        bad_encrypted = _encrypt_with_key(LEGACY_REDIRECT, other_key)
        # Row 2: encrypted with the current key and matches legacy — should be deleted.
        good_encrypted = _encrypt_with_key(LEGACY_PROVIDER, current_key)

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_redirect_uri', :v)"),
                {"v": bad_encrypted},
            )
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_provider_url', :v)"),
                {"v": good_encrypted},
            )

        monkeypatch.setenv("ENCRYPTION_KEY", current_key)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(monkeypatch, db_url, env_overrides={"ENCRYPTION_KEY": current_key})

        captured = capsys.readouterr()
        # The unreadable row generates a DEBUG skip message.
        assert "DEBUG: migration 058" in captured.out
        assert "skipping" in captured.out
        # Exactly 1 row cleared (the good one).
        assert "✓ Cleared 1 legacy OIDC secret rows" in captured.out
        # Bad row still present; good row gone.
        assert _row_count(engine, "oidc_redirect_uri") == 1
        assert _row_count(engine, "oidc_provider_url") == 0

    def test_migration_058_clears_legacy_provider_url_without_trailing_slash(
        self, tmp_path, monkeypatch, capsys
    ):
        """A row with oidc_provider_url stored WITHOUT a trailing slash is deleted.

        _matches() covers three forms: exact, stripped, and stripped+slash.
        LEGACY_PROVIDER ends with '/', so branch 2 (decrypted == legacy.rstrip("/"))
        handles the no-trailing-slash variant.
        """
        enc_key = "test-key-for-migration-058"
        engine, db_url = _make_engine(tmp_path)

        # Store the value without the trailing slash.
        no_slash_value = LEGACY_PROVIDER.rstrip("/")
        encrypted = _encrypt_with_key(no_slash_value, enc_key)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO secrets (service_name, encrypted_value) VALUES ('oidc_provider_url', :v)"),
                {"v": encrypted},
            )

        assert _row_count(engine, "oidc_provider_url") == 1

        monkeypatch.setenv("ENCRYPTION_KEY", enc_key)
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        _run_upgrade(monkeypatch, db_url, env_overrides={"ENCRYPTION_KEY": enc_key})

        captured = capsys.readouterr()
        assert "✓ Cleared 1 legacy OIDC secret rows" in captured.out
        assert _row_count(engine, "oidc_provider_url") == 0

    def test_migration_058_imports_from_app(self, monkeypatch):
        """The sys.path bootstrap in 058 allows `from app.utils.encryption import
        decrypt_value` to succeed when the migration file is imported directly."""
        # Ensure ENCRYPTION_KEY is set so the module-level key derivation
        # uses a real value and doesn't fall back to a throwaway key.
        monkeypatch.setenv("ENCRYPTION_KEY", "test-import-key")
        monkeypatch.delenv("ENCRYPTION_SALT", raising=False)
        _reload_encryption_module()

        migration_path = (
            _ADMIN_BACKEND
            / "alembic"
            / "versions"
            / "058_clear_legacy_oidc_redirect_uri.py"
        )
        spec = importlib.util.spec_from_file_location("migration_058", migration_path)
        module = importlib.util.module_from_spec(spec)
        # Loading the module executes the sys.path bootstrap at module top-level.
        spec.loader.exec_module(module)

        # After loading, app.utils.encryption must be importable.
        from app.utils.encryption import decrypt_value  # noqa: F401 (import test)

        assert callable(decrypt_value)
