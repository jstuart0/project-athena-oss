"""Phase 5 removal guards — ATHENA-1 Campaign 4.

Asserts that the deprecated server/service registry artifacts are gone:
  - app.routes.servers module deleted (ImportError on import)
  - ServerConfig, ServiceRegistry, AthenaService absent from models.__all__
  - Alembic migration 056 exists, has correct chain, and raises
    NotImplementedError on downgrade (forward-only migration)
"""
import importlib
import pytest


# ---------------------------------------------------------------------------
# A. Route module deleted
# ---------------------------------------------------------------------------

def test_servers_route_module_deleted():
    """app.routes.servers must raise ImportError — the module was deleted in Phase 5."""
    with pytest.raises(ImportError):
        importlib.import_module("app.routes.servers")


# ---------------------------------------------------------------------------
# B. Deprecated model classes absent from __all__
# ---------------------------------------------------------------------------

def test_server_config_not_in_models_all():
    """ServerConfig must not be in models.__all__ after Phase 5."""
    import app.models as models
    assert 'ServerConfig' not in models.__all__, (
        "ServerConfig is still listed in models.__all__ — "
        "Phase 5 removal was incomplete"
    )


def test_service_registry_not_in_models_all():
    """ServiceRegistry must not be in models.__all__ after Phase 5."""
    import app.models as models
    assert 'ServiceRegistry' not in models.__all__, (
        "ServiceRegistry is still listed in models.__all__ — "
        "Phase 5 removal was incomplete"
    )


def test_athena_service_not_in_models_all():
    """AthenaService must not be in models.__all__ after Phase 5."""
    import app.models as models
    assert 'AthenaService' not in models.__all__, (
        "AthenaService is still listed in models.__all__ — "
        "Phase 5 removal was incomplete"
    )


# ---------------------------------------------------------------------------
# C. Alembic migration 056 — chain + forward-only downgrade
# ---------------------------------------------------------------------------

def test_migration_056_revision_chain():
    """Migration 056 must chain off 055 (single canonical head)."""
    import importlib.util, pathlib

    versions_dir = pathlib.Path(__file__).parent.parent / "alembic" / "versions"
    migration_file = versions_dir / "056_drop_deprecated_service_tables.py"

    assert migration_file.exists(), (
        f"Migration file 056_drop_deprecated_service_tables.py not found at {migration_file}"
    )

    spec = importlib.util.spec_from_file_location("migration_056", migration_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert getattr(mod, "revision", None) == "056", (
        "migration 056: revision identifier must be '056'"
    )
    assert getattr(mod, "down_revision", None) == "055", (
        "migration 056: down_revision must be '055' to chain off Phase 2 consolidation"
    )


def test_migration_056_downgrade_raises():
    """Migration 056 downgrade() must raise NotImplementedError (forward-only)."""
    import importlib.util, pathlib

    versions_dir = pathlib.Path(__file__).parent.parent / "alembic" / "versions"
    migration_file = versions_dir / "056_drop_deprecated_service_tables.py"

    spec = importlib.util.spec_from_file_location("migration_056", migration_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with pytest.raises(NotImplementedError):
        mod.downgrade()
