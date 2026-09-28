"""Unit tests for src/mode_service/main.py permission-contract routes
(ATHENA-69 P3).

Covers:
 - GET /mode/permissions ?mode=guest forcing guest permissions regardless of
   server mode; ?mode=owner rejected (422); no param unchanged.
 - restricted_intents forwarded from admin config.
 - empty admin config still gets the shared floor + baseline (D8/D22).
 - the mode service's own guest defaults now come from shared.guest_policy,
   so an env override reaches the response the same way it does for the
   orchestrator.
 - the dead garage.*/alarm.* defaults are gone; scene is not a default
   allowed domain (D3.3/D8).

TestClient on mode_service.main.app WITHOUT the `with` context manager, so
lifespan (Redis connect, calendar/config-refresh background tasks) never
runs; module-level current_mode/current_config are set directly per test.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

import pytest
import structlog
from fastapi.testclient import TestClient

from shared import config as config_module

_SERVICE_KEY = "test-mode-service-key"
_HEADERS = {"X-Service-Key": _SERVICE_KEY}


@pytest.fixture(scope="module", autouse=True)
def _restore_structlog_after_module():
    """Importing mode_service.main calls shared.logging_config.configure_logging(),
    which globally replaces structlog's processors list and rebinds the
    "service" contextvar (shared/logging_config.py:104-117) -- a process-wide
    side effect. In production each service is its own process, so this never
    collides; in this shared pytest session it would otherwise leak
    "mode-service" into every later-running test file's log assertions.
    Module-scoped: snapshot once before this file's first test, restore
    once after its last.
    """
    snapshot = structlog.get_config()
    yield
    structlog.configure(**snapshot)


@pytest.fixture(autouse=True)
def _mode_service_env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", _SERVICE_KEY)
    monkeypatch.setenv("DEV_MODE", "true")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.fixture
def ms(_mode_service_env):
    from mode_service import main as ms_main

    ms_main.current_config = {}
    ms_main.current_events = []
    ms_main.current_mode = "owner"
    ms_main.active_override = None
    ms_main._config_loaded = True
    ms_main._last_load_ok = True
    return ms_main


@pytest.fixture
def client(ms):
    return TestClient(ms.app)


class TestModePermissionsParam:
    def test_guest_param_returns_guest_while_owner(self, ms, client):
        ms.current_mode = "owner"
        ms.current_config = {}
        resp = client.get("/mode/permissions", params={"mode": "guest"}, headers=_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["mode"] == "guest"

    def test_owner_param_rejected(self, ms, client):
        resp = client.get("/mode/permissions", params={"mode": "owner"}, headers=_HEADERS)
        assert resp.status_code == 422

    def test_no_param_unchanged_owner(self, ms, client):
        ms.current_mode = "owner"
        resp = client.get("/mode/permissions", headers=_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["mode"] == "owner"

    def test_restricted_intents_forwarded(self, ms, client):
        ms.current_config = {"guest_restricted_intents": ["tesla", "banking"]}
        resp = client.get("/mode/permissions", params={"mode": "guest"}, headers=_HEADERS)
        assert resp.json()["restricted_intents"] == ["tesla", "banking"]

    def test_empty_admin_config_still_gets_floor_and_baseline(self, ms, client):
        """Named member: ^lock\\. in restricted_entities; allowed_intents non-empty."""
        ms.current_config = {}
        resp = client.get("/mode/permissions", params={"mode": "guest"}, headers=_HEADERS)
        body = resp.json()
        assert r"^lock\." in body["restricted_entities"]
        assert body["allowed_intents"] != []

    def test_floor_env_override(self, ms, client, monkeypatch):
        monkeypatch.setenv("GUEST_BASELINE_RESTRICTED_ENTITIES", r'["^sensor\\.tesla"]')
        config_module._clear_cache_for_tests()
        ms.current_config = {}
        resp = client.get("/mode/permissions", params={"mode": "guest"}, headers=_HEADERS)
        assert resp.json()["restricted_entities"] == [r"^sensor\.tesla"]

    def test_no_dead_garage_pattern(self, ms, client):
        ms.current_config = {}
        resp = client.get("/mode/permissions", params={"mode": "guest"}, headers=_HEADERS)
        for pattern in resp.json()["restricted_entities"]:
            assert "garage" not in pattern
            assert pattern != "alarm.*"

    def test_scene_not_in_default_allowed_domains(self, ms, client):
        ms.current_config = {}
        resp = client.get("/mode/permissions", params={"mode": "guest"}, headers=_HEADERS)
        assert "scene" not in resp.json()["allowed_domains"]


class TestGetPermissionsUnknownModeFailsClosed:
    """ATHENA-69 Pass H2 (xander delta review, High): get_permissions()'s
    old tail was an unconditional else -- any effective_mode that wasn't
    literally "degraded"/"guest" fell through to the unrestricted owner
    branch. determine_mode() now only ever returns "owner"/"guest"
    (validated), so this simulates a stored/computed value bypassing that
    validation (e.g. a future caller of determine_mode that forgets the
    Literal-typed request model) to prove get_permissions() itself fails
    closed as a second, independent layer -- belt and suspenders.
    """

    def test_unknown_stored_mode_yields_degraded_not_owner(self, ms, client, monkeypatch):
        monkeypatch.setattr(ms, "determine_mode", lambda: "bogus-mode")
        ms.current_config = {}

        resp = client.get("/mode/permissions", headers=_HEADERS)

        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "degraded"
        assert body["allowed_intents"] == []
        assert body["allowed_domains"] == []
        assert r"^lock\." in body["restricted_entities"]

    def test_unknown_stored_mode_is_never_unrestricted(self, ms, client, monkeypatch):
        """The specific exploit shape: an unvalidated mode string must never
        produce owner's `restricted_entities: []` / unrestricted response."""
        monkeypatch.setattr(ms, "determine_mode", lambda: "Owner")
        ms.current_config = {}

        resp = client.get("/mode/permissions", headers=_HEADERS)

        body = resp.json()
        assert body["mode"] != "owner"
        assert body["restricted_entities"] != []


class TestDegradedPermissionsParity:
    """ATHENA-69 Pass H2 (xander delta review, Info->fix): the mode
    service's cold-start/fallback degraded response must match the
    orchestrator's D4 outage fallback (orchestrator.mode_permission.
    degraded_permissions()) exactly -- floor the seven physical-security
    domains, no intent/domain narrowing beyond that. Before this fix the
    mode service reused the narrower guest baseline, so the same failure
    class (system can't determine real permissions) produced two different
    security postures depending on which layer detected it.

    Deliberately does NOT import `orchestrator.mode_permission` here:
    importing the orchestrator package in-process has previously leaked
    structlog global config into this file's sibling test module (the
    `_restore_structlog_after_module` fixture above documents exactly this
    class of process-wide side effect). Instead this asserts the mode
    service's degraded shape against the same source-of-truth constant
    (`GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT`) and the literal shape
    `orchestrator.mode_permission.degraded_permissions()` returns (verified
    by reading both implementations side by side; see PR/commit notes).
    """

    def test_mode_service_degraded_matches_orchestrator_shape(self, ms, client, monkeypatch):
        from shared.guest_policy import GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT

        monkeypatch.setattr(ms, "determine_mode", lambda: "bogus-mode")
        ms.current_config = {}
        resp = client.get("/mode/permissions", headers=_HEADERS)
        ms_degraded = resp.json()

        assert ms_degraded["mode"] == "degraded"
        assert ms_degraded["allowed_intents"] == []
        assert ms_degraded["allowed_domains"] == []
        assert ms_degraded["restricted_intents"] == []
        assert ms_degraded["restricted_entities"] == GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT
