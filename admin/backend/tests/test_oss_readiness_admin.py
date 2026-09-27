"""ATHENA-89 Phase 1 — admin-backend/admin-frontend maintainer-default removal.

Covers the plan/test-contract's A1-A7 (+A6b) and M1-M6 (+M4b) assertions:
- A-series: music_config's Music Assistant URL precedence, internal.py's
  default_location, voice_tests.py's _rag_probe_url, main.py's SearXNG
  status (extracted to _check_searxng_status for testability), and
  migration 027's text.
- M-series: mcp_security.py's fail-open fix (A20/D14) — empty-allowlist
  deny-all-remote defaults, dotted-suffix matching, bare "*" handling on
  both the read path (_domain_matches / check_domain) and the write path
  (update_mcp_security PUT strip-and-save, add_allowed_domain single-item
  reject).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog.testing

from app.models import Feature, MCPSecurity, MusicConfig
from app.routes import internal as internal_module
from app.routes import mcp_security as mcp_security_module
from app.routes import voice_tests as voice_tests_module
from shared.config import _clear_cache_for_tests

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def _seed_music_config(db, *, music_assistant_enabled: bool, music_assistant_url):
    config = MusicConfig(
        music_assistant_enabled=music_assistant_enabled,
        music_assistant_url=music_assistant_url,
    )
    db.add(config)
    db.commit()
    return config


def _seed_feature(db, name: str, enabled: bool):
    db.add(Feature(name=name, display_name=name, category="integration", enabled=enabled))
    db.commit()


class _FakeAsyncpgConn:
    """Stands in for an asyncpg connection: every SELECT resolves to "no row"."""

    async def fetchrow(self, *_args, **_kwargs):
        return None

    async def fetch(self, *_args, **_kwargs):
        return []

    async def close(self):
        pass


class _FakeHttpxClient:
    """Stands in for httpx.AsyncClient, tracking whether .get was awaited."""

    def __init__(self, response=None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeHttpxResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


# ---------------------------------------------------------------------------
# A1-A3: music_config.get_browser_playback_config precedence
# ---------------------------------------------------------------------------


def test_A1_no_db_no_env_music_assistant_not_configured(client, db, monkeypatch):
    monkeypatch.delenv("MUSIC_ASSISTANT_URL", raising=False)
    _clear_cache_for_tests()
    _seed_music_config(db, music_assistant_enabled=True, music_assistant_url=None)
    _seed_feature(db, "music_playback", True)
    _seed_feature(db, "browser_music_playback", True)

    resp = client.get("/api/music-config/browser-playback")

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "error": "Music Assistant not configured"}


def test_A2_env_used_when_db_unset(client, db, monkeypatch):
    monkeypatch.setenv("MUSIC_ASSISTANT_URL", "http://ma.test:8095")
    _clear_cache_for_tests()
    _seed_music_config(db, music_assistant_enabled=True, music_assistant_url=None)
    _seed_feature(db, "music_playback", True)
    _seed_feature(db, "browser_music_playback", True)

    resp = client.get("/api/music-config/browser-playback")

    body = resp.json()
    assert body["enabled"] is True
    assert body["ws_url"] == "ws://ma.test:8095/ws"


def test_A3_db_value_wins_over_env(client, db, monkeypatch):
    monkeypatch.setenv("MUSIC_ASSISTANT_URL", "http://env-ma.test:8095")
    _clear_cache_for_tests()
    _seed_music_config(db, music_assistant_enabled=True, music_assistant_url="http://db-ma.test:8095")
    _seed_feature(db, "music_playback", True)
    _seed_feature(db, "browser_music_playback", True)

    resp = client.get("/api/music-config/browser-playback")

    body = resp.json()
    assert body["enabled"] is True
    assert body["ws_url"] == "ws://db-ma.test:8095/ws"


# ---------------------------------------------------------------------------
# A4: internal.py default_location is None, not "Baltimore, MD"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A4_get_base_knowledge_default_location_none(monkeypatch):
    async def _fake_conn():
        return _FakeAsyncpgConn()

    monkeypatch.setattr(internal_module, "get_athena_db_connection", _fake_conn)

    result = await internal_module.get_base_knowledge()

    assert result["default_location"] is None


@pytest.mark.asyncio
async def test_A4_get_all_config_base_knowledge_default_location_none(monkeypatch):
    async def _fake_conn():
        return _FakeAsyncpgConn()

    monkeypatch.setattr(internal_module, "get_admin_db_connection", _fake_conn)
    monkeypatch.setattr(internal_module, "get_athena_db_connection", _fake_conn)

    result = await internal_module.get_all_config()

    assert result["base_knowledge"]["default_location"] is None


# ---------------------------------------------------------------------------
# A5: voice_tests._rag_probe_url
# ---------------------------------------------------------------------------


def test_A5_rag_probe_url_weather_skipped_when_city_empty():
    assert voice_tests_module._rag_probe_url("weather", 8010, "", "") is None


def test_A5_rag_probe_url_weather_with_city_and_state():
    url = voice_tests_module._rag_probe_url("weather", 8010, "Denver", "CO")
    assert url.endswith("location=Denver,CO")


def test_A5_rag_probe_url_airports_uses_fixed_probe_code():
    url = voice_tests_module._rag_probe_url("airports", 8011, "", "")
    assert url is not None
    assert url.endswith(f"/airports/{voice_tests_module.PROBE_AIRPORT_CODE}")
    assert url.endswith("/airports/JFK")


# ---------------------------------------------------------------------------
# A6 / A6b: SearXNG status (extracted _check_searxng_status)
# ---------------------------------------------------------------------------


def test_A6_empty_searxng_url_not_configured_no_network_call(monkeypatch):
    import main as main_module

    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    _clear_cache_for_tests()
    fake_client = _FakeHttpxClient()

    status = asyncio.run(main_module._check_searxng_status(fake_client))

    assert status.status == "not configured"
    assert fake_client.calls == []


def test_A6b_configured_url_connect_error_not_deployed(monkeypatch):
    import main as main_module
    import httpx

    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng.example.com")
    _clear_cache_for_tests()
    fake_client = _FakeHttpxClient(exc=httpx.ConnectError("boom"))

    status = asyncio.run(main_module._check_searxng_status(fake_client))

    assert status.status == "not deployed"
    assert status.error == "SearXNG service not accessible"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["follow_redirects"] is False
    assert fake_client.calls[0]["url"] == "http://searxng.example.com/healthz"


def test_A6b_configured_url_running(monkeypatch):
    import main as main_module

    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng.example.com")
    _clear_cache_for_tests()
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(200))

    status = asyncio.run(main_module._check_searxng_status(fake_client))

    assert status.healthy is True
    assert status.status == "running"


# ---------------------------------------------------------------------------
# A7: migration 027 text
# ---------------------------------------------------------------------------


def test_A7_migration_027_no_home_dir_path_and_correct_updates():
    text = (
        _REPO_ROOT / "admin" / "backend" / "migrations" / "027_enable_no_thinking_defaults.sql"
    ).read_text(encoding="utf-8")

    assert "/Users/" not in text
    assert "ILIKE '%Qwen3%'" in text
    assert "backend_type = 'mlx'" in text
    assert "|| '{\"chat_template_kwargs\"" in text


# ---------------------------------------------------------------------------
# M1-M2: check_domain fail-open fix
# ---------------------------------------------------------------------------


def test_M1_no_row_evil_domain_denied(client, db):
    resp = client.post("/api/mcp-security/check-domain", json={"url": "https://evil.example"})
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.parametrize("url", ["http://localhost", "http://127.0.0.1"])
def test_M2_no_row_localhost_allowed(client, db, url):
    resp = client.post("/api/mcp-security/check-domain", json={"url": url})
    assert resp.json()["allowed"] is True


@pytest.mark.parametrize("url", ["http://localhost", "http://127.0.0.1"])
def test_M2_row_with_empty_allowlist_localhost_allowed(client, db, url):
    db.add(MCPSecurity(allowed_domains=[], blocked_domains=[]))
    db.commit()
    resp = client.post("/api/mcp-security/check-domain", json={"url": url})
    assert resp.json()["allowed"] is True


# ---------------------------------------------------------------------------
# M3: _domain_matches dotted-suffix fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("notexample.com", False),
        ("evilexample.com", False),
        ("example.com", True),
        ("a.example.com", True),
    ],
)
def test_M3_domain_matches_dotted_suffix(domain, expected):
    assert mcp_security_module._domain_matches(domain, ["*.example.com"]) is expected


# ---------------------------------------------------------------------------
# M4 / M4b: bare "*" is never a wildcard, per-item not per-list
# ---------------------------------------------------------------------------


def test_M4_bare_wildcard_alone_is_no_match_with_one_warning():
    with structlog.testing.capture_logs() as captured:
        result = mcp_security_module._domain_matches("anything.example", ["*"])

    assert result is False
    warnings = [e for e in captured if e.get("event") == "mcp_allowlist_bare_wildcard_ignored"]
    assert len(warnings) == 1


def test_M4b_mixed_list_bare_wildcard_ignored_real_entry_still_matches():
    assert mcp_security_module._domain_matches(
        "good.example.com", ["*", "good.example.com"]
    ) is True
    assert mcp_security_module._domain_matches(
        "evil.example", ["*", "good.example.com"]
    ) is False


def test_M4b_check_domain_mixed_allowlist(client, db):
    db.add(MCPSecurity(allowed_domains=["*", "good.example.com"], blocked_domains=[]))
    db.commit()

    good = client.post("/api/mcp-security/check-domain", json={"url": "https://good.example.com"})
    evil = client.post("/api/mcp-security/check-domain", json={"url": "https://evil.example"})

    assert good.json()["allowed"] is True
    assert evil.json()["allowed"] is False


# ---------------------------------------------------------------------------
# M5: PUT strips "*"/"" and saves the rest; single-item POST still 400s
# ---------------------------------------------------------------------------


def test_M5_put_strips_bare_wildcards_and_saves_rest(client, db):
    with structlog.testing.capture_logs() as captured:
        resp = client.put(
            "/api/mcp-security",
            json={"allowed_domains": ["localhost", "*", ""]},
        )

    assert resp.status_code == 200
    assert resp.json()["allowed_domains"] == ["localhost"]
    stripped_warnings = [
        e for e in captured if e.get("event") == "mcp_allowlist_bare_wildcard_stripped"
    ]
    assert len(stripped_warnings) == 1
    assert set(stripped_warnings[0]["stripped"]) == {"*", ""}


def test_M5_put_only_bare_wildcard_saves_empty_list_200(client, db):
    resp = client.put("/api/mcp-security", json={"allowed_domains": ["*"]})

    assert resp.status_code == 200
    assert resp.json()["allowed_domains"] == []


@pytest.mark.parametrize("domain", ["*", ""])
def test_M5_add_allowed_domain_single_item_wildcard_400(client, db, domain):
    resp = client.post("/api/mcp-security/domains/allow", params={"domain": domain})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# M6: no-row GET creates/returns exactly ["localhost","127.0.0.1"]
# ---------------------------------------------------------------------------


def test_M6_get_mcp_security_no_row_creates_clean_defaults(client, db):
    resp = client.get("/api/mcp-security")
    assert resp.status_code == 200
    assert resp.json()["allowed_domains"] == ["localhost", "127.0.0.1"]


def test_M6_get_mcp_security_public_no_row_clean_defaults(client, db):
    resp = client.get("/api/mcp-security/public")
    assert resp.status_code == 200
    assert resp.json()["allowed_domains"] == ["localhost", "127.0.0.1"]
