"""codex diff-review Medium (2026-09-28, batch review): the Ollama SSRF
gates added for component_models.py/voice_tests.py (ATHENA-122) inherit
the health poller's default-deny allowlist with no carve-out, so the OSS
default http://localhost:11434 (bare-metal dev) and an in-cluster
http://ollama:11434 (ClusterIP is RFC1918) both stop working for model
discovery/voice tests unless HEALTH_POLL_ALLOWED_PRIVATE_HOSTS is set.

app.utils.rag_urls.check_ollama_ssrf_safe() closes the bare-metal-dev case
specifically, reusing the SAME not-in-cluster loopback/RFC1918/ULA carve-
out already established at the Ollama write-boundary
(app.utils.url_validators.is_local_host, POST /api/settings/ollama-url).
Posture is otherwise unchanged: in-cluster (KUBERNETES_SERVICE_HOST set)
gets no carve-out, ever, and link-local (IMDS-class) addresses are
excluded from the carve-out by is_local_host() itself.

No mocking of check_ssrf_safe / is_local_host -- both are the exact
functions under test, using real IP literals (resolve via getaddrinfo
with no network access needed for loopback/RFC1918/link-local).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

from app.utils.rag_urls import check_ollama_ssrf_safe
from shared.config import _clear_cache_for_tests


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("IN_CLUSTER", raising=False)
    monkeypatch.delenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.mark.asyncio
async def test_bare_metal_loopback_dev_allowed_without_allowlist(monkeypatch):
    """The exact OSS default: http://localhost:11434, no
    HEALTH_POLL_ALLOWED_PRIVATE_HOSTS set, not in a K8s pod."""
    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/api/tags")
    assert allowed is True
    assert reason == ""


@pytest.mark.asyncio
async def test_rfc1918_dev_host_allowed_outside_cluster_without_allowlist():
    allowed, reason = await check_ollama_ssrf_safe("http://192.168.1.50:11434/api/tags")
    assert allowed is True
    assert reason == ""


@pytest.mark.asyncio
async def test_in_cluster_loopback_still_blocked_without_allowlist(monkeypatch):
    """Posture unchanged inside a pod: is_local_host() returns False
    unconditionally when KUBERNETES_SERVICE_HOST is set, regardless of
    the host being loopback/RFC1918."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/api/tags")
    assert allowed is False
    assert reason != ""


@pytest.mark.asyncio
async def test_in_cluster_clusterip_service_still_blocked_without_allowlist(monkeypatch):
    """The exact scenario codex named: in-cluster http://ollama:11434 whose
    ClusterIP is RFC1918 stays blocked until the operator allowlists it --
    "in-cluster... needs the allowlist, same as any other in-cluster
    Service" (docs/CONFIGURATION.md), no built-in exception."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    allowed, reason = await check_ollama_ssrf_safe("http://10.96.5.20:11434/api/tags")
    assert allowed is False
    assert reason != ""


@pytest.mark.asyncio
async def test_in_cluster_clusterip_service_allowed_once_allowlisted(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "10.96.5.20")
    _clear_cache_for_tests()
    allowed, reason = await check_ollama_ssrf_safe("http://10.96.5.20:11434/api/tags")
    assert allowed is True
    assert reason == ""


@pytest.mark.asyncio
async def test_link_local_imds_not_covered_by_the_carveout():
    """is_local_host() explicitly excludes link-local (APIPA/IMDS-class)
    addresses from the "local dev" carve-out -- an operator-editable Ollama
    URL pointed at 169.254.169.254 must stay blocked regardless of
    in-cluster status."""
    allowed, reason = await check_ollama_ssrf_safe("http://169.254.169.254:80/api/tags")
    assert allowed is False
    assert reason != ""


@pytest.mark.asyncio
async def test_carveout_logs_a_warning_when_it_fires(monkeypatch):
    from app.utils import rag_urls as rag_urls_module

    calls = []
    monkeypatch.setattr(
        rag_urls_module.logger, "warning",
        lambda event, **kw: calls.append((event, kw)),
    )

    allowed, _ = await check_ollama_ssrf_safe("http://localhost:11434/api/tags")

    assert allowed is True
    assert any(event == "ollama_ssrf_local_dev_carveout" for event, _ in calls), calls


@pytest.mark.asyncio
async def test_carveout_does_not_fire_and_no_warning_when_allowed_normally(monkeypatch):
    """A host that was never blocked in the first place (allowed by
    check_ssrf_safe directly) must not log the carve-out warning -- it
    only fires on the "blocked, but covered" path."""
    from app.utils import rag_urls as rag_urls_module

    calls = []
    monkeypatch.setattr(
        rag_urls_module.logger, "warning",
        lambda event, **kw: calls.append((event, kw)),
    )
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "localhost")
    _clear_cache_for_tests()

    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/api/tags")

    assert allowed is True
    assert reason == ""
    assert not any(event == "ollama_ssrf_local_dev_carveout" for event, _ in calls), calls
