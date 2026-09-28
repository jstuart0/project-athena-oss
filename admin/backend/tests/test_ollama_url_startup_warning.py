"""codex diff-review Medium (2026-09-28, batch review): main.py's startup
event must surface the Ollama SSRF gate's requirements as a discoverable
WARNING (`ollama_url_blocked_by_ssrf_guard`) rather than only as a
per-request error an operator has to trigger first -- and that warning
must apply the SAME not-in-cluster loopback carve-out
check_ollama_ssrf_safe() itself uses, so it does not false-positive-nag a
bare-metal dev's default http://localhost:11434.

get_db_context() is monkeypatched to a no-op context manager (the function
under test only uses the yielded `db` to call get_ollama_url(db), which is
independently monkeypatched below) -- this isolates the test from
main.py's own engine/get_db_context DB wiring, which is a different
SQLAlchemy engine than conftest.py's test `db` fixture.

Patches are applied to freshly re-resolved module objects (`import
app.database as db_mod` at the START of each patch call, not once at
collection time), never pytest's monkeypatch string-path form
("app.database.get_db_context"). Two independent full-suite-only failure
modes, both closed by this: (1) the string form resolves via
getattr(sys.modules['app'], 'database'), which breaks whenever some other
module evicts and re-imports `app`/`shared` submodules mid-suite (e.g.
test_rate_limit_active.py's own documented FastAPI-version-regression
repro) and the fresh `app` package hasn't had `.database` re-bound as an
attribute yet; (2) even a plain module-level `import app.database as
db_mod` captured once at THIS file's collection time can go stale if that
same eviction happens later, before this file's tests run -- re-importing
inside the patch call instead always resolves whatever is CURRENTLY in
sys.modules, matching exactly what main.py's own local `from app.database
import get_db_context` resolves against at call time.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

import main as main_module
from shared.config import _clear_cache_for_tests


def _clear_all_config_caches() -> None:
    """Belt-and-suspenders cache clear (ATHENA-118 test-isolation note, same
    fix as test_service_control_ollama.py / test_voice_tests_ssrf_guard.py):
    some other module in a full-suite run evicts and re-imports shared.config
    mid-suite, which can leave this file's own `_clear_cache_for_tests`
    reference pointing at a stale, already-replaced module -- check_ssrf_safe's
    lazy get_config() import would then resolve against a DIFFERENT (live)
    module's lru_cache this file's own reference never touches."""
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


@contextmanager
def _fake_db_context():
    yield None


def _capture_warnings(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_module.logger, "warning",
        lambda event, **kw: calls.append((event, kw)),
    )
    return calls


def _patch_db_and_ollama_url(monkeypatch, url):
    import app.database as db_mod
    from app.routes import service_control as sc_mod
    monkeypatch.setattr(db_mod, "get_db_context", _fake_db_context)
    monkeypatch.setattr(sc_mod, "get_ollama_url", lambda db: url)


@pytest.mark.asyncio
async def test_no_warning_for_bare_metal_loopback_default(monkeypatch):
    """The OSS default (http://localhost:11434, not in a K8s pod) is
    covered by the carve-out -- no warning."""
    _patch_db_and_ollama_url(monkeypatch, "http://localhost:11434")
    calls = _capture_warnings(monkeypatch)

    await main_module._warn_if_ollama_url_ssrf_blocked()

    assert not any(event == "ollama_url_blocked_by_ssrf_guard" for event, _ in calls), calls


@pytest.mark.asyncio
async def test_warning_fires_for_in_cluster_url_without_allowlist(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _patch_db_and_ollama_url(monkeypatch, "http://ollama:11434")
    calls = _capture_warnings(monkeypatch)

    await main_module._warn_if_ollama_url_ssrf_blocked()

    matches = [kw for event, kw in calls if event == "ollama_url_blocked_by_ssrf_guard"]
    assert len(matches) == 1, calls
    assert matches[0]["url"] == "http://ollama:11434"
    assert "HEALTH_POLL_ALLOWED_PRIVATE_HOSTS" in matches[0]["message"]


@pytest.mark.asyncio
async def test_no_warning_once_in_cluster_url_is_allowlisted(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "ollama")
    _clear_all_config_caches()
    _patch_db_and_ollama_url(monkeypatch, "http://ollama:11434")
    calls = _capture_warnings(monkeypatch)

    await main_module._warn_if_ollama_url_ssrf_blocked()

    assert not any(event == "ollama_url_blocked_by_ssrf_guard" for event, _ in calls), calls


@pytest.mark.asyncio
async def test_never_raises_on_internal_error(monkeypatch):
    """Discoverability aid, not a startup gate: an internal failure (e.g. a
    broken get_ollama_url) must not abort startup."""
    def _boom(db):
        raise RuntimeError("db unreachable")

    import app.database as db_mod
    from app.routes import service_control as sc_mod
    monkeypatch.setattr(db_mod, "get_db_context", _fake_db_context)
    monkeypatch.setattr(sc_mod, "get_ollama_url", _boom)
    calls = _capture_warnings(monkeypatch)

    await main_module._warn_if_ollama_url_ssrf_blocked()  # must not raise

    assert any(event == "ollama_url_ssrf_check_failed" for event, _ in calls), calls
