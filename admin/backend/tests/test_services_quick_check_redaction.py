"""codex r3 diff-review Low (2026-09-28): app.routes.services._ssrf_check_url's
quick_check_ssrf_blocked warning logged the raw url (including any
ENV_FALLBACKS-sourced value, e.g. an operator-set REDIS_URL/QDRANT_URL/
OLLAMA_URL potentially carrying userinfo) verbatim. Now redacted via
app.utils.url_validators.redact_url_userinfo().

Asserts against the real rendered log output (capsys), not a monkeypatched
`logger.warning` -- see test_settings_ollama_url_ssrf.py's
test_save_ollama_url_never_logs_userinfo for why: a full-suite run's
mid-suite app.*/shared.* module eviction can leave a monkeypatched module
object different from whichever module the already-registered call site
actually resolves against, but capsys captures the real stdout writer
regardless.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

from app.routes.services import _ssrf_check_url
from shared.config import _clear_cache_for_tests


def _clear_all_config_caches() -> None:
    _clear_cache_for_tests()
    live = sys.modules.get('shared.config')
    if live is not None and hasattr(live, '_clear_cache_for_tests'):
        live._clear_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("IN_CLUSTER", raising=False)
    monkeypatch.delenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", raising=False)
    _clear_all_config_caches()
    yield
    _clear_all_config_caches()


@pytest.mark.asyncio
async def test_ssrf_check_url_never_logs_userinfo(monkeypatch, capsys):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _clear_all_config_caches()

    capsys.readouterr()
    result = await _ssrf_check_url("http://redisuser:redispass@10.96.5.20:6379")
    out = capsys.readouterr().out

    assert result is not None
    assert result["status"] == "ssrf_blocked"
    assert "quick_check_ssrf_blocked" in out, out
    assert "10.96.5.20:6379" in out, out
    assert "redisuser:redispass" not in out, out
    assert "redispass" not in out, out


@pytest.mark.asyncio
async def test_ssrf_check_url_allows_and_returns_none_when_allowlisted(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "10.96.5.20")
    _clear_all_config_caches()

    result = await _ssrf_check_url("http://10.96.5.20:6379")

    assert result is None
