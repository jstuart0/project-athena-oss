"""codex BLOCK (2026-09-27-diagnose-athena-mission-control review): RAG
registry/env URLs were live-probed by voice_tests.py without the health
poller's SSRF/runtime-DNS allowlist. Registry hosts are operator data, but
that's a write-time trust decision only -- DNS can change afterward, so
every live probe against a resolved URL must still pass
app.utils.rag_urls.check_ssrf_safe (which imports, not reimplements,
app.services.health_poller._validate_service_url).

These tests use a real private IP literal as the registry host (resolves
via getaddrinfo with no network access needed) rather than a mocked
resolver, so the allowlist decision exercised here is the real one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote as urlquote

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from app.auth.oidc import get_current_user
from app.models import RagService, SystemSetting
from app.routes import voice_tests as voice_tests_module
from app.utils import rag_urls
from main import app
from shared.config import _clear_cache_for_tests

_PRIVATE_HOST = "10.66.66.66"  # RFC1918, never allowlisted unless a test opts in
_BLOCKED_OLLAMA_URL = "http://169.254.169.254:80"  # link-local IMDS, always blocked


def _clear_all_config_caches() -> None:
    """Belt-and-suspenders cache clear (ATHENA-118 test-isolation note, same
    fix as test_service_control_ollama.py): some other module in a full-
    suite run evicts and re-imports shared.config mid-suite, which can
    leave this file's own `_clear_cache_for_tests` reference pointing at a
    stale, already-replaced module -- check_ssrf_safe's lazy `from
    app.services.health_poller import _validate_service_url` would then
    resolve against the live (different) module's get_config() lru_cache,
    which this file's import-time reference never touches."""
    _clear_cache_for_tests()
    live = sys.modules.get('shared.config')
    if live is not None and hasattr(live, '_clear_cache_for_tests'):
        live._clear_cache_for_tests()


def _seed_ollama_url(db, url: str) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    if row:
        row.value = url
    else:
        db.add(SystemSetting(key="ollama_url", value=url, category="llm"))
    db.commit()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for name in (
        "RAG_HOST", "RAG_SERVICE_HOST", "RAG_WEATHER_URL", "HEALTH_POLL_ALLOWED_PRIVATE_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)
    rag_urls._reset_legacy_warning_cache()
    _clear_all_config_caches()
    yield
    rag_urls._reset_legacy_warning_cache()
    _clear_all_config_caches()


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


def _never_called_session(*args, **kwargs):
    raise AssertionError("aiohttp.ClientSession must not be constructed when SSRF-blocked")


def test_rag_test_endpoint_blocks_unallowlisted_private_host_with_no_network_call(owner_client, db, monkeypatch):
    db.add(RagService(
        name="weather", display_name="Weather", host=_PRIVATE_HOST,
        port=8010, protocol="http", enabled=True,
    ))
    db.commit()

    monkeypatch.setattr(voice_tests_module.aiohttp, "ClientSession", _never_called_session)

    response = owner_client.post("/api/voice-tests/rag/test", json={"connector": "weather", "text": "Denver"})

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "ssrf_blocked"


def test_rag_test_endpoint_allows_allowlisted_private_host_and_probes_it(owner_client, db, monkeypatch):
    """Regression for the allowed case: when check_ssrf_safe reports the
    resolved host as allowed, the probe actually happens. The allowlist
    parsing itself (HEALTH_POLL_ALLOWED_PRIVATE_HOSTS -> allowed) is
    covered independently and exhaustively by test_phase4_health_poller.py
    / test_phase4_reconcile.py / test_athena_109_tcp_poller.py; stubbing
    check_ssrf_safe directly here isolates "allowed -> probe happens" from
    get_config()'s process-wide lru_cache, which those other suites'
    direct os.environ mutation (not monkeypatch-scoped) can leave in a
    state this test doesn't control.

    codex r3: also spies on check_ssrf_safe's own call argument, proving
    the URL it validates is the EXACT final URL -- built with the
    user-supplied text already urllib.parse.quote-encoded -- and that
    session.get() is given that same URL (not a pre-encoding or
    differently-encoded variant of it). Uses text with characters that
    must be percent-encoded so the assertion can't pass on a no-op quote()."""
    ssrf_spy = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(voice_tests_module, "check_ssrf_safe", ssrf_spy)
    db.add(RagService(
        name="weather", display_name="Weather", host=_PRIVATE_HOST,
        port=8010, protocol="http", enabled=True,
    ))
    db.commit()

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"temp": 72})
    mock_response.headers = {}

    mock_get_ctx = MagicMock()
    mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_get_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_get_ctx)
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(voice_tests_module.aiohttp, "ClientSession", MagicMock(return_value=mock_session_ctx))

    response = owner_client.post("/api/voice-tests/rag/test", json={"connector": "weather", "text": "Denver, CO"})

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True

    expected_url = f"http://{_PRIVATE_HOST}:8010/weather/current?location={urlquote('Denver, CO', safe='')}"
    assert expected_url == f"http://{_PRIVATE_HOST}:8010/weather/current?location=Denver%2C%20CO"  # sanity: proves quoting actually changed the text

    assert ssrf_spy.called
    ssrf_called_url = ssrf_spy.call_args.args[0]
    assert ssrf_called_url == expected_url

    assert mock_session.get.called
    session_called_url = mock_session.get.call_args.args[0]
    assert session_called_url == expected_url
    assert session_called_url == ssrf_called_url  # the exact same URL, not just equal-looking


def test_full_pipeline_rag_enhancement_blocks_unallowlisted_private_host(owner_client, db, monkeypatch):
    """test_full_pipeline's RAG-enhancement step must not be reached over the
    network for a private, non-allowlisted registry host -- the LLM step
    ahead of it in the same pipeline legitimately uses aiohttp too, so this
    asserts by URL (the private RAG host is never requested), not by
    forbidding ClientSession construction outright.

    ATHENA-122: the pipeline's LLM step now runs through check_ssrf_safe too
    (before this RAG step is ever reached), so the default ollama_url
    (http://localhost:11434) must be explicitly allowlisted here the same
    way test_service_control_ollama.py's _allow_host() does for every other
    Ollama-probing test -- loopback is blocked by default."""
    monkeypatch.setenv("DEFAULT_CITY", "Denver")
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "localhost")
    _clear_all_config_caches()
    db.add(RagService(
        name="weather", display_name="Weather", host=_PRIVATE_HOST,
        port=8010, protocol="http", enabled=True,
    ))
    db.commit()

    requested_urls = []

    def _fake_get_response(status=200, json_body=None):
        resp = MagicMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_body or {})
        return resp

    def _make_ctx(resp):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    mock_session = MagicMock()

    def _post(url, **kwargs):
        requested_urls.append(url)
        return _make_ctx(_fake_get_response(200, {"response": "It's sunny in Denver."}))

    def _get(url, **kwargs):
        requested_urls.append(url)
        return _make_ctx(_fake_get_response(200, {}))

    mock_session.post = MagicMock(side_effect=_post)
    mock_session.get = MagicMock(side_effect=_get)
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(voice_tests_module.aiohttp, "ClientSession", MagicMock(return_value=mock_session_ctx))

    response = owner_client.post("/api/voice-tests/pipeline/test", json={"text": "what's the weather like"})

    assert response.status_code == 200, response.text
    body = response.json()
    results = body.get("results", {})
    # codex r3: exact equality on the marker -- "ssrf_blocked" is the
    # contract, not merely a substring of some longer human-readable string.
    assert results.get("rag_error") == "ssrf_blocked", body
    assert results.get("rag_error_reason", "") != ""
    assert "private ip" in results["rag_error_reason"].lower()
    assert not any(_PRIVATE_HOST in u for u in requested_urls), requested_urls


def test_llm_test_endpoint_blocks_private_ollama_host_with_no_network_call(owner_client, db, monkeypatch):
    """ATHENA-122: POST /api/voice-tests/llm/test probes the operator-set
    ollama_url without going through check_ssrf_safe first. A blocked host
    must 403 before aiohttp.ClientSession is ever constructed."""
    _seed_ollama_url(db, _BLOCKED_OLLAMA_URL)
    monkeypatch.setattr(voice_tests_module.aiohttp, "ClientSession", _never_called_session)

    response = owner_client.post("/api/voice-tests/llm/test", json={"text": "hello"})

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "ssrf_blocked"


def test_llm_test_endpoint_allows_allowed_ollama_host_and_probes_it(owner_client, db, monkeypatch):
    _seed_ollama_url(db, "http://192.0.2.10:11434")
    ssrf_spy = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(voice_tests_module, "check_ssrf_safe", ssrf_spy)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"response": "hi", "eval_count": 1})

    mock_post_ctx = MagicMock()
    mock_post_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_ctx)
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(voice_tests_module.aiohttp, "ClientSession", MagicMock(return_value=mock_session_ctx))

    response = owner_client.post("/api/voice-tests/llm/test", json={"text": "hello"})

    assert response.status_code == 200, response.text
    assert ssrf_spy.called
    assert ssrf_spy.call_args.args[0] == "http://192.0.2.10:11434/api/generate"
    assert mock_session.post.called
    assert mock_session.post.call_args.args[0] == "http://192.0.2.10:11434/api/generate"


def test_pipeline_test_endpoint_blocks_private_ollama_host_with_no_network_call(owner_client, db, monkeypatch):
    """ATHENA-122: POST /api/voice-tests/pipeline/test's first (LLM) step
    probes the operator-set ollama_url without going through
    check_ssrf_safe. A blocked host must 403 before the pipeline's LLM step
    -- and therefore before aiohttp.ClientSession is ever constructed for
    any stage of the pipeline."""
    _seed_ollama_url(db, _BLOCKED_OLLAMA_URL)
    monkeypatch.setattr(voice_tests_module.aiohttp, "ClientSession", _never_called_session)

    response = owner_client.post("/api/voice-tests/pipeline/test", json={"text": "hello"})

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "ssrf_blocked"
