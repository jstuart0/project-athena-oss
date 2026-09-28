"""ATHENA-118 Phase 3: T15 -- POST /api/settings/ollama-url SSRF write gate.

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T15.

Mocking strategy: check_ssrf_safe / validate_host are NOT mocked -- they are
the exact functions under test. httpx.AsyncClient is globally monkeypatched
to a MockTransport so a blocked-but-somehow-reached probe is provably zero
requests, not merely "the assertion happened to pass".
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

import httpx
import pytest

from app.models import SystemSetting

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"version": "0.1.0"})


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _stored_ollama_url(db):
    row = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    return row.value if row else None


IMMUTABLE_BAD_URLS = [
    pytest.param("http://169.254.169.254:80", id="imds"),
    pytest.param("http://[fe80::1]:11434", id="link_local_v6"),
    pytest.param("http://ollama.athena-prod.svc.cluster.local:11434", id="svc_cluster_local"),
    pytest.param("http://224.0.0.1:11434", id="multicast"),
    pytest.param("http://0.0.0.0:11434", id="unspecified"),
    pytest.param("http://127.0.0.1:11434", id="loopback_in_cluster"),
]


@pytest.mark.parametrize("bad_url", IMMUTABLE_BAD_URLS)
def test_write_gate_rejects_ssrf_class_hosts_in_cluster(owner_client, db, monkeypatch, bad_url):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    before = _stored_ollama_url(db)
    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": bad_url})

    assert response.status_code == 422
    assert _stored_ollama_url(db) == before
    assert transport.requests == []


def test_loopback_allowed_outside_k8s_pod(owner_client, db, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": "http://127.0.0.1:11434"})

    assert response.status_code == 200
    assert _stored_ollama_url(db) == "http://127.0.0.1:11434"


def test_rfc1918_house_value_is_saved(owner_client, db, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": "http://192.168.10.108:11434"})

    assert response.status_code == 200
    assert _stored_ollama_url(db) == "http://192.168.10.108:11434"


def test_non_http_scheme_rejected(owner_client, db, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    before = _stored_ollama_url(db)
    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": "ftp://x"})

    assert response.status_code == 422
    assert _stored_ollama_url(db) == before
    assert transport.requests == []
