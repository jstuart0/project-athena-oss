"""The real seam: the orchestrator reads jarvis-web's household routes
(V4.6, D12). smart_home_controller._jarvis_get drives jarvis-web's actual
app over httpx.ASGITransport with one shared SERVICE_API_KEY.
"""
from __future__ import annotations

import ast
import asyncio

import httpx
import httpx._client
import pytest

from . import _jarvis_web_harness as jh
from . import _public_audience_harness as oh
import orchestrator.smart_home_controller as shc

JARVIS_PATHS = ["/api/appliances/oven", "/api/appliances/fridge", "/api/sensors/motion",
                "/api/sensors/illuminance", "/api/sensors/summary", "/api/media"]
INTERNAL_URL = "http://athena-jarvis-web.athena-prod.svc:3001"


@pytest.fixture
def jarvis(monkeypatch):
    key = oh.shared_config.get_config().service_api_key
    jh.configure({**jh.HOME_ENV, "SERVICE_API_KEY": key})
    jh.install_outbound(monkeypatch)
    monkeypatch.setattr(shc, "_jarvis_key_warned", False)
    yield
    jh.configure()


def _get(url, path):
    async def run():
        transport = httpx.ASGITransport(app=jh.main.app)
        # the real client: the jarvis harness fakes httpx.AsyncClient (jarvis-web's
        # own outbound Home Assistant calls), not the class itself
        async with httpx._client.AsyncClient(transport=transport) as client:
            return await shc._jarvis_get(client, url, path)
    return asyncio.run(run())


@pytest.mark.parametrize("path", JARVIS_PATHS)
def test_orchestrator_reads_household_routes(jarvis, path):
    """Floor 6; named member /api/media."""
    resp = _get(INTERNAL_URL, path)
    assert resp.status_code == 200, path


def test_all_six_sites_use_the_helper():
    source = (oh.ORCH_DIR / "smart_home_controller.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    paths = [
        ast.literal_eval(n.args[2]) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_jarvis_get"
    ]
    assert sorted(paths) == sorted(JARVIS_PATHS)
    assert "/api/media" in paths
    assert source.count('client.get(f"{jarvis_url}') == 1  # the helper itself


def test_without_key_is_401(jarvis, monkeypatch):
    monkeypatch.setattr(shc, "get_config", lambda: type("C", (), {"service_api_key": ""})())
    assert _get(INTERNAL_URL, "/api/media").status_code == 401


def test_public_jarvis_url_gets_no_key(jarvis, monkeypatch, caplog):
    headers = shc._jarvis_web_headers("https://jarvis.example.com")
    assert headers == {}
    assert "jarvis_web_url_not_internal" in caplog.text


@pytest.mark.parametrize("url, internal", [
    ("http://jarvis-web:3001", True),
    ("http://athena-jarvis-web.athena-prod.svc.cluster.local", True),
    ("http://10.0.0.5:3001", True),
    ("http://127.0.0.1:3001", True),
    ("https://jarvis.example.com", False),
    ("http://203.0.113.9", False),
])
def test_internal_host_rule(url, internal):
    assert shc._is_internal_host(url) is internal
