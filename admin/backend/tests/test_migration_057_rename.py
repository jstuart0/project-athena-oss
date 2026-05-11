"""ATHENA-17: round-trip migration 057 against scratch DBs.

Bypasses the shared `db` fixture. Builds the legacy 056-shape schema via
explicit SQL, stamps the revision, then exercises 057 upgrade/downgrade.

Source-of-truth for the legacy SQL fixture: `admin/backend/app/models.py`
at the commit shipping this migration, cross-referenced with athena_prod's
live `pg_indexes` and `pg_constraint` snapshot captured 2026-05-11. A
live `pg_dump --schema-only` capture was attempted but blocked by a
pg_dump version mismatch (server 16.9 vs local 14.19); the hand-shaped
fixture is the working approximation. The fixture may drift from
production over time — that's a pre-existing risk accepted for ATHENA-17.

CI status: the Postgres parametrization is LOCAL-ONLY. No current CI
workflow exports POSTGRES_TEST_URL. Operator MUST run the postgres path
locally before opening the PR (`POSTGRES_TEST_URL=postgresql://... pytest`).

To run only the Postgres path:
    POSTGRES_TEST_URL=postgresql://postgres:postgres@localhost:5432/postgres \\
        pytest admin/backend/tests/test_migration_057_rename.py -xvs -m postgres
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

# Repo root: this file lives at admin/backend/tests/, so go up three levels.
REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "admin" / "backend" / "alembic.ini"

# Legacy schema as it exists on a 056-recorded DB. Mirrors what athena_prod
# looks like today: rag_services + rag_connectors + 2 explicit idx_* indexes
# (idx_rag_services_name, idx_rag_services_enabled) + constraint-backed
# rag_services_name_key (UNIQUE) + rag_services_pkey (PK) +
# FK rag_connectors_service_id_fkey.
#
# NOTE: idx_rag_services_service_type and ix_rag_services_name are declared
# in models.py but are NOT present on athena_prod (never migrated). The
# migration uses `ALTER INDEX IF EXISTS` so their absence is a no-op.
# This fixture intentionally omits them to test that absent-index path.
_LEGACY_SQL_POSTGRES = """\
CREATE TABLE rag_services (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    description TEXT,
    service_type VARCHAR(50),
    host VARCHAR(255),
    port INTEGER,
    protocol VARCHAR(8) DEFAULT 'http',
    health_endpoint VARCHAR(256) DEFAULT '/health',
    endpoint_url TEXT,
    control_method VARCHAR(50) DEFAULT 'none',
    container_name VARCHAR(255),
    headers JSONB,
    query_template TEXT,
    response_parser TEXT,
    cache_ttl INTEGER DEFAULT 300,
    timeout INTEGER DEFAULT 5000,
    rate_limit INTEGER DEFAULT 100,
    api_key_encrypted TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    auto_start BOOLEAN DEFAULT TRUE,
    health_status VARCHAR(20),
    is_running BOOLEAN DEFAULT FALSE,
    last_health_check TIMESTAMPTZ,
    last_response_time_ms INTEGER,
    last_error TEXT,
    health_message VARCHAR(500),
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
)"""

# Index set mirrors athena_prod's actual pg_indexes snapshot as of 2026-05-11.
# Excludes idx_rag_services_service_type (declared in models.py but absent on
# prod). No ix_rag_services_name either — the unique=True column suppresses it.
_LEGACY_INDEXES_POSTGRES = """\
CREATE INDEX idx_rag_services_name ON rag_services (name)"""

_LEGACY_INDEXES_POSTGRES_2 = """\
CREATE INDEX idx_rag_services_enabled ON rag_services (enabled)"""

# rag_services_name_key and rag_services_pkey are emitted automatically by
# UNIQUE NOT NULL and PRIMARY KEY above (no explicit CREATE INDEX needed).

_LEGACY_CONNECTORS_POSTGRES = """\
CREATE TABLE rag_connectors (
    id SERIAL PRIMARY KEY,
    service_id INTEGER REFERENCES rag_services(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
)"""

# SQLite version is simpler — just create the tables; constraint name
# guarantees don't apply to SQLite. The three idx_* indexes are present here
# (including service_type) to verify the migration silently skips them.
_LEGACY_SQL_SQLITE = """\
CREATE TABLE rag_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(64) UNIQUE NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    enabled INTEGER DEFAULT 1,
    service_type VARCHAR(50)
);
CREATE INDEX idx_rag_services_name ON rag_services (name);
CREATE INDEX idx_rag_services_enabled ON rag_services (enabled);
CREATE INDEX idx_rag_services_service_type ON rag_services (service_type);
CREATE TABLE rag_connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER REFERENCES rag_services(id)
)"""


def _scratch_alembic_config(db_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    # script_location in alembic.ini is relative to the ini file; force absolute.
    cfg.set_main_option(
        "script_location",
        str((ALEMBIC_INI.parent / "alembic").resolve()),
    )
    return cfg


def _run_alembic(monkeypatch, cfg: Config, op_name: str, target: str) -> None:
    """Run alembic command with DATABASE_URL temporarily removed so alembic's
    env.py honors cfg.sqlalchemy.url rather than the global :memory: URL set
    by conftest.py:27. Pattern lifted from test_security_hardening.py:1992-2005.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        if op_name == "stamp":
            command.stamp(cfg, target)
        elif op_name == "upgrade":
            command.upgrade(cfg, target)
        elif op_name == "downgrade":
            command.downgrade(cfg, target)
        else:
            raise ValueError(f"unsupported alembic op: {op_name}")
    finally:
        monkeypatch.undo()   # restore DATABASE_URL for the rest of the test


def _apply_legacy_schema_postgres(engine) -> None:
    """Apply the hand-shaped 056-state schema to a Postgres scratch DB."""
    stmts = [
        _LEGACY_SQL_POSTGRES,
        _LEGACY_INDEXES_POSTGRES,
        _LEGACY_INDEXES_POSTGRES_2,
        _LEGACY_CONNECTORS_POSTGRES,
    ]
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _apply_legacy_schema_sqlite(engine) -> None:
    """Apply the hand-shaped 056-state schema to a SQLite scratch DB."""
    with engine.begin() as conn:
        for stmt in [s.strip() for s in _LEGACY_SQL_SQLITE.split(";") if s.strip()]:
            conn.execute(text(stmt))


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def scratch_engine(request, tmp_path, monkeypatch):
    """Yields (engine, cfg, dialect_name, alembic_fn) for each DB variant.

    The `alembic_fn` callable takes (op_name, target) and runs the alembic
    command with DATABASE_URL hidden so env.py uses cfg's URL. Tests MUST
    go through `alembic_fn` rather than calling command.* directly.
    """
    if request.param == "sqlite":
        db_url = f"sqlite:///{tmp_path / 'm057.db'}"
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
        _apply_legacy_schema_sqlite(engine)
    else:
        db_url = os.getenv("POSTGRES_TEST_URL")
        if not db_url:
            pytest.skip("POSTGRES_TEST_URL not set; skipping Postgres run")
        engine = create_engine(db_url)
        _apply_legacy_schema_postgres(engine)

    cfg = _scratch_alembic_config(db_url)

    # Stamp 056 so upgrade sees the DB as already at that revision.
    _run_alembic(monkeypatch, cfg, "stamp", "056")

    def alembic_fn(op_name: str, target: str) -> None:
        _run_alembic(monkeypatch, cfg, op_name, target)

    yield engine, cfg, request.param, alembic_fn

    # Teardown: drop everything so the Postgres scratch DB is clean for the
    # next parametrization (SQLite file is discarded by tmp_path).
    if request.param == "postgres":
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS rag_connectors CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS rag_services CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS athena_service_registry CASCADE"))
            conn.execute(text(
                "DELETE FROM alembic_version WHERE version_num IN ('056', '057')"
            ))
        engine.dispose()


class TestMigration057RoundTrip:

    def test_upgrade_renames_table(self, scratch_engine):
        """After upgrade, athena_service_registry exists and rag_services does not."""
        engine, cfg, _dialect, alembic = scratch_engine

        alembic("upgrade", "057")

        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert "athena_service_registry" in tables
        assert "rag_services" not in tables

    def test_upgrade_renames_indexes_on_postgres(self, scratch_engine):
        """Assert that whatever indexes existed BEFORE upgrade now exist under
        their new names, and no legacy `*rag_services*` names remain on the
        renamed table. Does NOT assert a hardcoded full set — the migration uses
        `ALTER INDEX IF EXISTS` so any index absent from the fixture is a silent
        no-op (matching athena_prod's pre-existing index subset).
        """
        engine, cfg, dialect, alembic = scratch_engine
        if dialect != "postgres":
            pytest.skip("SQLite has no ALTER INDEX; indexes keep legacy names")

        # Capture pre-upgrade index names on `rag_services`.
        with engine.connect() as conn:
            pre_rows = conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'rag_services'"
            )).fetchall()
        pre_names = {r[0] for r in pre_rows}

        alembic("upgrade", "057")

        with engine.connect() as conn:
            post_rows = conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'athena_service_registry' ORDER BY indexname"
            )).fetchall()
        post_names = {r[0] for r in post_rows}

        # Every pre-upgrade `rag_services_*` / `idx_rag_services_*` /
        # `ix_rag_services_*` index should appear under its renamed form.
        expected_renamed = {
            old.replace("rag_services", "athena_service_registry")
            for old in pre_names
        }
        assert expected_renamed.issubset(post_names), (
            f"Missing renamed indexes; "
            f"pre={pre_names}, post={post_names}, "
            f"expected_subset={expected_renamed}"
        )

        # No legacy names should survive on the renamed table.
        legacy_survivors = {n for n in post_names if "rag_services" in n}
        assert not legacy_survivors, (
            f"Legacy index names still attached: {legacy_survivors}"
        )

    def test_constraint_renames_on_postgres(self, scratch_engine):
        """The DO-block constraint renames target the actual prod state:
        rag_services_pkey + rag_services_name_key (both constraint-backed
        indexes) become athena_service_registry_pkey +
        athena_service_registry_name_key.
        """
        engine, cfg, dialect, alembic = scratch_engine
        if dialect != "postgres":
            pytest.skip("constraint metadata is Postgres-only")

        alembic("upgrade", "057")

        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'athena_service_registry'::regclass "
                "ORDER BY conname"
            )).fetchall()
        names = {r[0] for r in rows}

        assert "athena_service_registry_pkey" in names
        assert "athena_service_registry_name_key" in names
        # Legacy constraint names must be gone.
        assert "rag_services_pkey" not in names
        assert "rag_services_name_key" not in names

    def test_fk_follows_rename_on_postgres(self, scratch_engine):
        """rag_connectors.service_id FK's confrelid resolves to
        athena_service_registry after upgrade (FK OID tracks the rename
        automatically; name stays as rag_connectors_service_id_fkey per plan D2).
        """
        engine, cfg, dialect, alembic = scratch_engine
        if dialect != "postgres":
            pytest.skip("pg_constraint is Postgres-only")

        alembic("upgrade", "057")

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT confrelid::regclass::text FROM pg_constraint "
                "WHERE conname = 'rag_connectors_service_id_fkey'"
            )).fetchone()
        assert row is not None, "FK constraint rag_connectors_service_id_fkey not found"
        assert row[0] == "athena_service_registry"

    def test_downgrade_reverses(self, scratch_engine):
        """Round-trip should restore the exact pre-upgrade index set on
        `rag_services` (not a hardcoded full set — see
        test_upgrade_renames_indexes_on_postgres rationale).
        """
        engine, cfg, dialect, alembic = scratch_engine

        pre_names: set = set()
        if dialect == "postgres":
            with engine.connect() as conn:
                pre_rows = conn.execute(text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'rag_services'"
                )).fetchall()
            pre_names = {r[0] for r in pre_rows}

        alembic("upgrade", "057")
        alembic("downgrade", "056")

        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert "rag_services" in tables
        assert "athena_service_registry" not in tables

        if dialect == "postgres":
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'rag_services' ORDER BY indexname"
                )).fetchall()
            post_names = {r[0] for r in rows}
            assert post_names == pre_names, (
                f"Downgrade did not restore pre-upgrade index set; "
                f"pre={pre_names}, post-downgrade={post_names}"
            )

            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT confrelid::regclass::text FROM pg_constraint "
                    "WHERE conname = 'rag_connectors_service_id_fkey'"
                )).fetchone()
            assert row is not None
            assert row[0] == "rag_services"

    def test_round_trip(self, scratch_engine):
        """Second forward pass after a full downgrade must also succeed."""
        _engine, cfg, _dialect, alembic = scratch_engine
        alembic("upgrade", "057")
        alembic("downgrade", "056")
        alembic("upgrade", "057")  # second forward pass must also succeed
