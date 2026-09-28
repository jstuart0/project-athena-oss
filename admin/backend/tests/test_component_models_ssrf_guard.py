"""ATHENA-122 -- component_models.py's two operator-editable Ollama URL
probes (GET /api/component-models/available-models model discovery, and
validate_model_exists used by PUT /{component_name}) were live-probed
without app.utils.rag_urls.check_ssrf_safe. The stored ollama_url
(SystemSetting row / OLLAMA_URL env fallback) is operator data, but that's a
write-time trust decision only -- DNS can change afterward -- so every
live probe against it must still pass the health poller's SSRF/runtime-DNS
allowlist.

Mocking strategy matches test_settings_ollama_url_ssrf.py: check_ssrf_safe is
NOT mocked for the blocked-host assertions -- it is the exact function under
test, using a real link-local IMDS-class literal (always blocked, no env
setup needed). httpx.AsyncClient is globally monkeypatched to a
MockTransport so "blocked" is provably zero requests, not merely an
assertion that happened to pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from app.models import SystemSetting
from app.routes import component_models as component_models_module

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_BLOCKED_HOST_URL = "http://169.254.169.254:80"  # link-local IMDS, always blocked


class _RecordingTransport:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"models": [{"name": "phi3:mini", "size": 1}]})


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _seed_ollama_url(db, url: str) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    if row:
        row.value = url
    else:
        db.add(SystemSetting(key="ollama_url", value=url, category="llm"))
    db.commit()


def test_available_models_blocks_private_host_zero_requests(owner_client, db, monkeypatch):
    _seed_ollama_url(db, _BLOCKED_HOST_URL)
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    response = owner_client.get("/api/component-models/available-models")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert not any(m["backend_type"] == "ollama" for m in body["models"])
    assert transport.requests == []


def test_available_models_probes_when_host_is_allowed(owner_client, db, monkeypatch):
    _seed_ollama_url(db, "http://192.0.2.10:11434")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)
    ssrf_spy_calls = []

    async def _fake_check_ssrf_safe(url):
        ssrf_spy_calls.append(url)
        return True, ""

    monkeypatch.setattr(component_models_module, "check_ollama_ssrf_safe", _fake_check_ssrf_safe)

    response = owner_client.get("/api/component-models/available-models")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["models"][0]["backend_type"] == "ollama"
    assert ssrf_spy_calls == ["http://192.0.2.10:11434/api/tags"]
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == "http://192.0.2.10:11434/api/tags"


@pytest.mark.asyncio
async def test_validate_model_exists_blocks_private_host_zero_requests(db, monkeypatch):
    _seed_ollama_url(db, _BLOCKED_HOST_URL)
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    result = await component_models_module.validate_model_exists("phi3:mini", db)

    assert result is False
    assert transport.requests == []


@pytest.mark.asyncio
async def test_validate_model_exists_probes_when_host_is_allowed(db, monkeypatch):
    _seed_ollama_url(db, "http://192.0.2.10:11434")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    async def _fake_check_ssrf_safe(url):
        return True, ""

    monkeypatch.setattr(component_models_module, "check_ollama_ssrf_safe", _fake_check_ssrf_safe)

    result = await component_models_module.validate_model_exists("phi3:mini", db)

    assert result is True
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == "http://192.0.2.10:11434/api/tags"
