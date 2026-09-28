"""Unit tests for the mode service's ingress auth (ATHENA-69 D15/D28).

Covers:
 - every /mode* route requires X-Service-Key; /health stays open.
 - warn mode allows through and logs; enforce (and any invalid mode) 401s.
 - a present-but-wrong key is 401 even under warn/DEV_MODE.
 - an empty configured key is 401 in every mode.
 - DEV_MODE bypasses the key requirement.
 - src/shared/service_ingress_auth.py's factory produces behavior identical
   to orchestrator/ingress_auth.py's require_service_caller for the same
   6-case decision table (parity).
 - the warn-mode posture-reminder loop logs repeatedly while warn, never
   while enforce.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

import pytest
import structlog
from fastapi import HTTPException
from fastapi.testclient import TestClient

from shared import config as config_module

_SERVICE_KEY = "test-mode-service-key"
_HEADERS = {"X-Service-Key": _SERVICE_KEY}


@pytest.fixture(scope="module", autouse=True)
def _restore_structlog_after_module():
    """Importing mode_service.main (and, in the parity test, orchestrator.
    ingress_auth) calls shared.logging_config.configure_logging(), which
    globally replaces structlog's processors list and rebinds the
    "service" contextvar (shared/logging_config.py:104-117) -- a
    process-wide side effect. In production each service is its own
    process, so this never collides; in this shared pytest session it
    would otherwise leak into every later-running test file's log
    assertions. Module-scoped: snapshot once before this file's first
    test, restore once after its last -- a function-scoped restore would
    invalidate mode_service.main.logger's cache_logger_on_first_use
    snapshot mid-file.
    """
    snapshot = structlog.get_config()
    yield
    structlog.configure(**snapshot)


@pytest.fixture(autouse=True)
def _mode_service_env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", _SERVICE_KEY)
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("MODE_SERVICE_INGRESS_AUTH", "enforce")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.fixture
def ms(_mode_service_env):
    from mode_service import main as ms_main

    ms_main.current_config = {}
    ms_main.current_mode = "owner"
    ms_main.active_override = None
    ms_main._config_loaded = True
    ms_main._last_load_ok = True
    ms_main.booking_sources = ms_main.BookingSources()
    return ms_main


@pytest.fixture
def client(ms):
    return TestClient(ms.app)


class TestEveryModeRouteRequiresKey:
    def test_every_mode_route_requires_key(self, ms, client):
        mode_paths = sorted({r.path for r in ms.app.routes if r.path.startswith("/mode")})
        assert mode_paths == ["/mode", "/mode/events", "/mode/override", "/mode/permissions"]

        for route in ms.app.routes:
            if route.path not in mode_paths:
                continue
            method = next(iter(route.methods - {"HEAD"}))
            resp = client.request(method, route.path, json={} if method == "POST" else None)
            assert resp.status_code == 401, f"{method} {route.path} did not require the key"

    def test_named_member_mode_override_requires_key(self, ms, client):
        resp = client.post("/mode/override", json={"mode": "owner"})
        assert resp.status_code == 401


class TestHealthOpen:
    def test_health_open_without_key(self, ms, client):
        resp = client.get("/health")
        assert resp.status_code == 200


class TestWarnMode:
    def test_warn_mode_allows_and_logs(self, ms, client, monkeypatch, caplog):
        monkeypatch.setenv("MODE_SERVICE_INGRESS_AUTH", "warn")
        config_module._clear_cache_for_tests()
        resp = client.get("/mode")
        assert resp.status_code == 200


class TestWrongKey:
    def test_wrong_key_401_even_in_warn(self, ms, client, monkeypatch):
        monkeypatch.setenv("MODE_SERVICE_INGRESS_AUTH", "warn")
        config_module._clear_cache_for_tests()
        resp = client.get("/mode", headers={"X-Service-Key": "wrong-key"})
        assert resp.status_code == 401


class TestEmptyConfiguredKey:
    def test_empty_configured_key_401(self, ms, client, monkeypatch):
        monkeypatch.setenv("SERVICE_API_KEY", "")
        config_module._clear_cache_for_tests()
        resp = client.get("/mode")
        assert resp.status_code == 401
        resp_warn = client.get("/mode/permissions")
        assert resp_warn.status_code == 401


class TestDevModeAllows:
    def test_dev_mode_allows(self, ms, client, monkeypatch):
        monkeypatch.setenv("DEV_MODE", "true")
        config_module._clear_cache_for_tests()
        resp = client.get("/mode")
        assert resp.status_code == 200


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers=None, path="/mode", client_host="127.0.0.1"):
        self.headers = headers or {}
        self.url = _FakeURL(path)
        self.client = _FakeClient(client_host) if client_host else None


def _outcome(coro) -> bool:
    """Run a dependency coroutine; True = allowed, False = 401'd."""
    try:
        asyncio.run(coro)
        return True
    except HTTPException:
        return False


# (mode, dev_mode, configured_key, header, expect_allowed) -- the six-step
# table both dependencies must implement identically.
_PARITY_CASES = [
    ("enforce", False, "secret", "wrong", False),  # step 1: wrong key, any mode
    ("warn", False, "secret", "wrong", False),      # step 1 applies under warn too
    ("enforce", False, "secret", "secret", True),   # step 2: matching key
    ("enforce", True, "secret", None, True),        # step 3: DEV_MODE bypass
    ("enforce", False, "", None, False),            # step 4: empty configured key
    ("warn", False, "secret", None, True),          # step 5: warn allows + logs
    ("enforce", False, "secret", None, False),      # step 6: enforce, no header
]


class TestSharedDependencyParityWithOrchestrator:
    def test_shared_dependency_parity_with_orchestrator(self, monkeypatch):
        from orchestrator import ingress_auth as orch_auth
        from shared.service_ingress_auth import make_require_service_caller

        mode_dep = make_require_service_caller("mode_service_ingress_auth", "mode_service")

        for mode, dev_mode, key, header, expect_allowed in _PARITY_CASES:
            monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", mode)
            monkeypatch.setenv("MODE_SERVICE_INGRESS_AUTH", mode)
            monkeypatch.setenv("DEV_MODE", "true" if dev_mode else "false")
            monkeypatch.setenv("SERVICE_API_KEY", key)
            config_module._clear_cache_for_tests()

            headers = {"X-Service-Key": header} if header else {}
            request = _FakeRequest(headers=headers)

            orch_allowed = _outcome(orch_auth.require_service_caller(request))
            mode_allowed = _outcome(mode_dep(request))

            assert orch_allowed == expect_allowed, (mode, dev_mode, key, header)
            assert mode_allowed == expect_allowed, (mode, dev_mode, key, header)
            assert orch_allowed == mode_allowed, (mode, dev_mode, key, header)


class TestWarnReminderLoop:
    """`structlog.testing.capture_logs()` is unsafe here: `logger` is a
    module-level bound logger cached on first use
    (`cache_logger_on_first_use=True` in shared/logging_config.py), and this
    process' test session imports many OTHER services' modules (each
    calling `configure_logging(...)` with its own fresh processors list) at
    times outside our control. If any of those imports happens after this
    module's logger already froze its first-use snapshot, capture_logs()
    ends up mutating a processors list this logger no longer references --
    observed as a real, order-dependent flake when this file runs inside
    the full suite. Mocking `ms.logger` directly sidesteps structlog's
    global config entirely.
    """

    def test_warn_reminder_logs_repeatedly(self, ms, monkeypatch):
        import unittest.mock as umock

        monkeypatch.setenv("MODE_SERVICE_INGRESS_AUTH", "warn")
        config_module._clear_cache_for_tests()
        mock_logger = umock.MagicMock()
        monkeypatch.setattr(ms, "logger", mock_logger)

        async def _run_briefly():
            task = asyncio.create_task(ms._posture_reminder_loop(0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run_briefly())
        warn_calls = [c for c in mock_logger.warning.call_args_list if c.args[:1] == ("mode_service_ingress_auth_warn_active",)]
        assert len(warn_calls) >= 2

    def test_warn_reminder_silent_under_enforce(self, ms, monkeypatch):
        import unittest.mock as umock

        monkeypatch.setenv("MODE_SERVICE_INGRESS_AUTH", "enforce")
        config_module._clear_cache_for_tests()
        mock_logger = umock.MagicMock()
        monkeypatch.setattr(ms, "logger", mock_logger)

        async def _run_briefly():
            task = asyncio.create_task(ms._posture_reminder_loop(0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run_briefly())
        warn_calls = [c for c in mock_logger.warning.call_args_list if c.args[:1] == ("mode_service_ingress_auth_warn_active",)]
        assert len(warn_calls) == 0
