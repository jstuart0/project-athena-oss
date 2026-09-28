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
from shared.config import _clear_cache_for_tests

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _clear_all_config_caches() -> None:
    """Belt-and-suspenders cache clear (ATHENA-118 test-isolation note, same
    fix as test_service_control_ollama.py): test_rate_limit_active.py
    evicts and re-imports every app./shared.* module mid-suite, which can
    leave this file's captured `_clear_cache_for_tests` reference pointing
    at a stale, already-replaced shared.config module. Look up whichever
    module object is CURRENTLY live in sys.modules and clear that one too."""
    _clear_cache_for_tests()
    live = sys.modules.get('shared.config')
    if live is not None and hasattr(live, '_clear_cache_for_tests'):
        live._clear_cache_for_tests()


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

    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": "http://192.0.2.10:11434"})

    assert response.status_code == 200
    assert _stored_ollama_url(db) == "http://192.0.2.10:11434"


def test_non_http_scheme_rejected(owner_client, db, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    before = _stored_ollama_url(db)
    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": "ftp://x"})

    assert response.status_code == 422
    assert _stored_ollama_url(db) == before
    assert transport.requests == []


# ---------------------------------------------------------------------------
# tessa P3 mid-build fold-in item 1 (HIGH): GET /api/settings/ollama-url's
# own reachability probe (settings.py:867-877) has the D21 SSRF gate too --
# a stored value can be a pre-existing bad one, or the OLLAMA_URL env
# fallback, neither of which the write-time gate above ever saw. A blocked
# host must report unreachable with ZERO transport calls (never merely a
#200 with is_reachable=False from a real connection attempt that happened
# to fail); an allowed host must actually probe.
# ---------------------------------------------------------------------------

def _seed_ollama_url(db, url: str) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    if row:
        row.value = url
    else:
        db.add(SystemSetting(key="ollama_url", value=url, category="llm"))
    db.commit()


def test_get_ollama_url_reports_ssrf_blocked_for_stored_private_host_zero_requests(client, db, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _seed_ollama_url(db, "http://192.0.2.10:11434")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    response = client.get("/api/settings/ollama-url")

    assert response.status_code == 200
    data = response.json()
    assert data["is_reachable"] is False
    assert data["error"] is not None
    assert "ssrf_blocked" in data["error"]
    assert transport.requests == []


def test_get_ollama_url_probes_when_host_is_allowed(client, db, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "192.0.2.10")
    _clear_all_config_caches()
    _seed_ollama_url(db, "http://192.0.2.10:11434")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    response = client.get("/api/settings/ollama-url")

    assert response.status_code == 200
    data = response.json()
    assert data["is_reachable"] is True
    assert data["version"] == "0.1.0"
    assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# tessa P3 mid-build fold-in item 3 (LOW): the ordering claim "write-boundary
# validation runs before the reachability probe" is not falsifiable by the
# existing IMMUTABLE_BAD_URLS test above, because save_ollama_url's own
# reachability block ALSO calls check_ssrf_safe (settings.py) -- so even a
# save_ollama_url that (incorrectly) ran _validate_ollama_url_write() AFTER
# the reachability probe would still show zero transport calls on a blocked
# host, since the redundant in-block gate masks the reordering. Isolate the
# claim by neutralizing check_ssrf_safe entirely (pass-through) and proving
# the 422 with zero HTTP calls survives on _validate_ollama_url_write alone.
# ---------------------------------------------------------------------------

def test_write_validation_precedes_reachability_probe_isolated_from_redundant_gate(owner_client, db, monkeypatch):
    import app.routes.settings as settings_module

    async def _pass_through(_url):
        return True, None

    monkeypatch.setattr(settings_module, "check_ssrf_safe", _pass_through)

    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    before = _stored_ollama_url(db)
    response = owner_client.post("/api/settings/ollama-url", json={"ollama_url": "http://169.254.169.254:80"})

    assert response.status_code == 422
    assert _stored_ollama_url(db) == before
    assert transport.requests == [], (
        "with check_ssrf_safe neutralized, only _validate_ollama_url_write's "
        "own host blocklist can be responsible for the zero-request outcome -- "
        "proving it runs before (and independently of) the reachability probe"
    )
