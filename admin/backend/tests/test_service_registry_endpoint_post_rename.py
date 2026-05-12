"""ATHENA-17 Phase 2: post-rename endpoint smoke test.

Boots the FastAPI app via TestClient against a scratch SQLite DB that has been
upgraded through migration 057 (rag_services → athena_service_registry) and
asserts GET /api/service-registry/services returns 200 with the exact expected
response envelope.

This test catches any surviving raw-SQL references along the request path that
a grep would miss (ORM session caching, f-strings, etc.).

Fixture caveats (plan r3 M2):
1. Config("alembic.ini") is relative — must use the absolute ALEMBIC_INI path.
2. `command.upgrade("head")` on a fresh DB does NOT create the legacy
   rag_services table (migration 055 only adds columns to an existing table;
   057 renames it). The fixture must apply the legacy SQL + stamp("056") +
   upgrade("head") — identical to test_migration_057_rename.py.
3. conftest.py imports main.app at module-load time; the app's engine is
   already bound to the conftest `:memory:` URL. We override the get_db
   dependency (FastAPI's supported pattern) to redirect the already-imported
   app to the scratch engine.

Requirements:
- DEV_MODE=true (set by conftest.py — get_current_user returns a default admin
  without requiring a token).
- No POSTGRES_TEST_URL needed; this test uses SQLite only.

CI note: no POSTGRES_TEST_URL export required for this test to pass in CI.
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

# Repo root: this file lives at admin/backend/tests/, three levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "admin" / "backend" / "alembic.ini"

# Full-schema legacy fixture: mirrors the complete rag_services column set as
# the ORM declares in models.py (all columns that migration 055 added). Using
# the full set here (rather than the minimal 5-column variant from
# test_migration_057_rename.py) so that after migration 057 renames the table,
# the ORM can SELECT all its mapped columns from athena_service_registry without
# hitting "no such column" errors.
_LEGACY_SQL_SQLITE = """\
CREATE TABLE rag_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    headers TEXT,
    query_template TEXT,
    response_parser TEXT,
    cache_ttl INTEGER DEFAULT 300,
    timeout INTEGER DEFAULT 5000,
    rate_limit INTEGER DEFAULT 100,
    api_key_encrypted TEXT,
    enabled INTEGER DEFAULT 1,
    auto_start INTEGER DEFAULT 1,
    health_status VARCHAR(20),
    is_running INTEGER DEFAULT 0,
    last_health_check DATETIME,
    last_response_time_ms INTEGER,
    last_error TEXT,
    health_message VARCHAR(500),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_rag_services_name ON rag_services (name);
CREATE INDEX idx_rag_services_enabled ON rag_services (enabled);
CREATE INDEX idx_rag_services_service_type ON rag_services (service_type);
CREATE TABLE rag_connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER REFERENCES rag_services(id)
)"""


def _run_alembic(monkeypatch, cfg: Config, op_name: str, target: str) -> None:
    """Run an alembic command with DATABASE_URL unset so alembic's env.py uses
    cfg.sqlalchemy.url rather than the global :memory: URL from conftest.py.

    Pattern: test_migration_057_rename.py:127-143.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        if op_name == "stamp":
            command.stamp(cfg, target)
        elif op_name == "upgrade":
            command.upgrade(cfg, target)
        else:
            raise ValueError(f"unsupported alembic op: {op_name}")
    finally:
        monkeypatch.undo()  # restore DATABASE_URL for the rest of the test


@pytest.fixture
def client_against_renamed_schema(tmp_path, monkeypatch):
    """Yield a TestClient whose get_db dependency resolves to a scratch SQLite
    DB that has been run through migration 057 (rag_services renamed to
    athena_service_registry).

    Steps:
    1. Create a scratch SQLite file at tmp_path.
    2. Apply the legacy 056-state schema via explicit SQL.
    3. Stamp the DB at revision 056, then upgrade to head (runs 057).
    4. Override app's get_db dependency to serve sessions from the scratch DB.
    5. Yield TestClient(app).
    6. Restore the dependency override on teardown.
    """
    db_path = tmp_path / "m057-endpoint.db"
    db_url = f"sqlite:///{db_path}"

    # Step 1 + 2: create scratch engine, apply legacy schema.
    scratch_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    with scratch_engine.begin() as conn:
        for stmt in [s.strip() for s in _LEGACY_SQL_SQLITE.split(";") if s.strip()]:
            conn.execute(text(stmt))

    # Confirm legacy table present before migration.
    insp = inspect(scratch_engine)
    assert "rag_services" in insp.get_table_names(), (
        "Legacy fixture did not create rag_services — fixture bug"
    )

    # Step 3: stamp 056, then upgrade to head (runs 057).
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location",
        str((ALEMBIC_INI.parent / "alembic").resolve()),
    )
    _run_alembic(monkeypatch, cfg, "stamp", "056")
    _run_alembic(monkeypatch, cfg, "upgrade", "head")

    # Confirm rename happened before we hand the DB to the app.
    insp = inspect(scratch_engine)
    tables = set(insp.get_table_names())
    assert "athena_service_registry" in tables, (
        "Migration 057 did not create athena_service_registry"
    )
    assert "rag_services" not in tables, (
        "Migration 057 did not remove rag_services"
    )

    # Create remaining ORM tables (users, api_keys, etc.) that DEV_MODE auth
    # needs but that alembic doesn't know about in this scratch DB. Using
    # checkfirst=True (CREATE TABLE IF NOT EXISTS semantics) so that
    # athena_service_registry — already created by the migration — is left
    # intact. The legacy fixture provided only rag_services + rag_connectors;
    # the full ORM schema has many more tables that the app touches during auth.
    from app.database import Base as _Base
    _Base.metadata.create_all(scratch_engine, checkfirst=True)

    # Step 4: override get_db on the already-imported app.
    # conftest.py imports main.app at module-load time (line 32); we cannot
    # redirect it via DATABASE_URL at this point. FastAPI dependency overrides
    # are the correct mechanism here.
    from main import app
    from app.database import get_db

    ScratchSession = sessionmaker(
        bind=scratch_engine, autocommit=False, autoflush=False
    )

    def _override_get_db():
        session = ScratchSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db

    # Step 5: yield the client.
    try:
        yield TestClient(app)
    finally:
        # Step 6: restore override so other tests are unaffected.
        app.dependency_overrides.pop(get_db, None)
        scratch_engine.dispose()


class TestServiceRegistryEndpointPostRename:
    """Smoke-test GET /api/service-registry/services against a DB that has been
    run through migration 057.

    Asserts the exact response envelope that the admin UI depends on
    (admin/frontend/service-control.js:101-109). Any surviving rag_services
    reference in the request path would raise an OperationalError here.
    """

    def test_get_services_returns_200(self, client_against_renamed_schema):
        resp = client_against_renamed_schema.get("/api/service-registry/services")
        assert resp.status_code == 200, (
            f"Expected 200 but got {resp.status_code}: {resp.text[:500]}"
        )

    def test_get_services_exact_envelope(self, client_against_renamed_schema):
        """Response must contain exactly the five keys the admin UI reads.

        Exact envelope: {services, total_services, healthy_services,
        overall_health, control_agent_enabled}. No extra keys; no missing keys.
        (codex r1 L-1, r3 M2, admin/frontend/service-control.js:101-109)
        """
        resp = client_against_renamed_schema.get("/api/service-registry/services")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        assert set(body.keys()) == {
            "services",
            "total_services",
            "healthy_services",
            "overall_health",
            "control_agent_enabled",
        }, f"Unexpected envelope keys: {set(body.keys())}"

    def test_get_services_field_types(self, client_against_renamed_schema):
        """Each field in the envelope has the correct type."""
        resp = client_against_renamed_schema.get("/api/service-registry/services")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["services"], list)
        assert isinstance(body["total_services"], int)
        assert isinstance(body["healthy_services"], int)
        assert body["overall_health"] in {"healthy", "degraded", "unhealthy", "unknown"}
        assert isinstance(body["control_agent_enabled"], bool)

    def test_get_services_counts_are_consistent(self, client_against_renamed_schema):
        """total_services == len(services); healthy_services <= total_services."""
        resp = client_against_renamed_schema.get("/api/service-registry/services")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_services"] == len(body["services"])
        assert body["healthy_services"] <= body["total_services"]
