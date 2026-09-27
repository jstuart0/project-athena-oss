"""ATHENA-89 Phase 7 (DC14 item 1) -- apps/jarvis-web/backend/main.py's
kitchen-appliance entity IDs (a specific GE appliance model's oven/fridge/
freezer HA entities, plus stove sensors) are now configured via
OVEN_ENTITY_ID / FRIDGE_ENTITY_ID / FREEZER_ENTITY_ID /
STOVE_COOK_MODE_SENSOR_ID / STOVE_DISPLAY_TEMP_SENSOR_ID /
STOVE_TIMER_SENSOR_ID / FRIDGE_DOOR_SENSOR_ID instead of hardcoded. Covers
the "not configured" 503 each endpoint now returns when its entity ID(s)
are unset, before any Home Assistant call is attempted.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# apps/jarvis-web/backend/main.py, gateway/main.py, orchestrator/main.py,
# admin/backend/main.py all share the bare name "main.py" -- a plain
# `import main` would collide with whichever one got cached into
# sys.modules["main"] first in this pytest process (test_rag_region_config.py
# already documents this same hazard for the RAG services' main.py files).
# Load under a private synthetic name instead.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JARVIS_BACKEND = _REPO_ROOT / "apps" / "jarvis-web" / "backend"
sys.path.insert(0, str(_JARVIS_BACKEND))

os.environ.setdefault("SERVICE_API_KEY", "test-key-jarvis-appliances")

_spec = importlib.util.spec_from_file_location("_test_jarvis_web_backend_main", _JARVIS_BACKEND / "main.py")
jarvis_main = importlib.util.module_from_spec(_spec)
sys.modules["_test_jarvis_web_backend_main"] = jarvis_main
_spec.loader.exec_module(jarvis_main)

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    # HA_TOKEN must be truthy so each endpoint's existing "Home Assistant
    # not configured" guard doesn't fire before the NEW entity-specific
    # guard this phase adds.
    monkeypatch.setattr(jarvis_main, "HA_TOKEN", "fake-ha-token")
    monkeypatch.setattr(jarvis_main, "OVEN_ENTITY", "")
    monkeypatch.setattr(jarvis_main, "FRIDGE_ENTITY", "")
    monkeypatch.setattr(jarvis_main, "FREEZER_ENTITY", "")
    return TestClient(jarvis_main.app)


def test_get_oven_state_503_when_unconfigured(client):
    resp = client.get("/api/appliances/oven")
    assert resp.status_code == 503
    assert "Oven not configured" in resp.json()["detail"]


def test_set_oven_temperature_503_when_unconfigured(client):
    resp = client.post("/api/appliances/oven/temperature", json={"temperature": 350})
    assert resp.status_code == 503
    assert "Oven not configured" in resp.json()["detail"]


def test_set_oven_mode_503_when_unconfigured(client):
    resp = client.post("/api/appliances/oven/mode", json={"mode": "Bake"})
    assert resp.status_code == 503
    assert "Oven not configured" in resp.json()["detail"]


def test_turn_oven_off_503_when_unconfigured(client):
    resp = client.post("/api/appliances/oven/off")
    assert resp.status_code == 503
    assert "Oven not configured" in resp.json()["detail"]


def test_get_fridge_state_503_when_both_unconfigured(client):
    resp = client.get("/api/appliances/fridge")
    assert resp.status_code == 503
    assert "Fridge/freezer not configured" in resp.json()["detail"]


def test_set_fridge_temperature_503_when_unconfigured(client):
    resp = client.post("/api/appliances/fridge/temperature", json={"temperature": 37})
    assert resp.status_code == 503
    assert "Fridge not configured" in resp.json()["detail"]


def test_set_freezer_temperature_503_when_unconfigured(client):
    resp = client.post("/api/appliances/freezer/temperature", json={"temperature": 0})
    assert resp.status_code == 503
    assert "Freezer not configured" in resp.json()["detail"]


def test_get_fridge_state_not_blocked_when_only_fridge_configured(client, monkeypatch):
    """Sibling check: configuring just one of fridge/freezer must not hit
    the "both unconfigured" 503 -- proves the guard is an OR, not an AND,
    on the two independently-configurable entities."""
    monkeypatch.setattr(jarvis_main, "FRIDGE_ENTITY", "water_heater.example_fridge")
    with mock.patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = mock.AsyncMock()
        mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mock.AsyncMock(return_value=False)
        resp_obj = mock.MagicMock(status_code=200)
        resp_obj.json.return_value = {"state": "off", "attributes": {}}
        mock_client.get = mock.AsyncMock(return_value=resp_obj)
        mock_client_cls.return_value = mock_client

        resp = client.get("/api/appliances/fridge")

    assert resp.status_code != 503
