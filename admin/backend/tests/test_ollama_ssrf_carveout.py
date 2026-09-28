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

from app.utils.rag_urls import check_ollama_ssrf_safe, check_ssrf_safe
from shared.config import _clear_cache_for_tests


def _clear_all_config_caches() -> None:
    """Belt-and-suspenders cache clear (ATHENA-118 test-isolation note, same
    fix as test_service_control_ollama.py / test_voice_tests_ssrf_guard.py):
    some other module in a full-suite run evicts and re-imports shared.config
    mid-suite, which can leave this file's own `_clear_cache_for_tests`
    reference pointing at a stale, already-replaced module."""
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
async def test_bare_metal_loopback_dev_allowed_without_allowlist(monkeypatch):
    """The exact OSS default: http://localhost:11434, no
    HEALTH_POLL_ALLOWED_PRIVATE_HOSTS set, not in a K8s pod."""
    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/api/tags")
    assert allowed is True
    assert reason == ""


@pytest.mark.asyncio
async def test_rfc1918_dev_host_allowed_outside_cluster_without_allowlist():
    allowed, reason = await check_ollama_ssrf_safe("http://10.55.55.55:11434/api/tags")
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
    _clear_all_config_caches()
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
    _clear_all_config_caches()

    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/api/tags")

    assert allowed is True
    assert reason == ""
    assert not any(event == "ollama_ssrf_local_dev_carveout" for event, _ in calls), calls


# ---------------------------------------------------------------------------
# codex r2 diff-review (2026-09-28)
# High: never log URL userinfo.
# Medium: the carve-out must match on the SPECIFIC private-host denial
# reason, not "blocked for any reason" -- a loopback URL with a CRLF/NUL/
# traversal path must stay blocked outside a cluster too.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_carveout_warning_never_logs_url_userinfo(monkeypatch):
    from app.utils import rag_urls as rag_urls_module

    calls = []
    monkeypatch.setattr(
        rag_urls_module.logger, "warning",
        lambda event, **kw: calls.append((event, kw)),
    )

    allowed, _ = await check_ollama_ssrf_safe("http://u:p@localhost:11434/api/tags")

    assert allowed is True
    matches = [kw for event, kw in calls if event == "ollama_ssrf_local_dev_carveout"]
    assert len(matches) == 1, calls
    assert matches[0]["url"] == "http://localhost:11434"
    serialized = repr(matches[0])
    assert "u:p" not in serialized, serialized
    assert "u:p@" not in serialized, serialized


@pytest.mark.asyncio
async def test_loopback_with_traversal_path_stays_blocked_outside_cluster():
    """The carve-out must NOT override a path-sanitization denial. A ".."
    traversal segment is a request-smuggling-adjacent vector regardless of
    how the host classifies -- is_local_host() says nothing about path
    safety. (urllib.parse.urlparse strips literal \\r\\n from a URL string
    before check_ssrf_safe ever sees it, so CRLF can't be exercised via a
    plain URL string here; the path-check's CRLF branch is defensive for a
    caller that builds `path` some other way. NUL and ".." both survive
    urlparse intact and exercise the same "denial reason isn't the
    private-IP one" property this fix protects.)"""
    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/../../etc/passwd")
    assert allowed is False
    assert reason != ""
    assert "traversal" in reason


@pytest.mark.asyncio
async def test_loopback_with_null_byte_path_stays_blocked_outside_cluster():
    allowed, reason = await check_ollama_ssrf_safe("http://localhost:11434/api/tags\x00.txt")
    assert allowed is False
    assert reason != ""
    assert "NUL" in reason


@pytest.mark.asyncio
async def test_k8s_control_plane_hostname_stays_blocked_even_outside_cluster():
    """k8s control-plane hostnames are blocked unconditionally by
    check_ssrf_safe itself (not a private-IP-resolution denial) -- the
    carve-out's reason-prefix match must not accidentally cover this
    denial class either."""
    allowed, reason = await check_ollama_ssrf_safe("http://kubernetes.default.svc:11434/api/tags")
    assert allowed is False
    assert reason == "k8s control-plane hostname blocked"


@pytest.mark.asyncio
async def test_private_ip_denial_prefix_matches_the_real_validator_reason():
    """Pin the exact reason text the carve-out matches against, so a
    future wording change in shared.url_safety.validate_url_not_private()
    that silently drifts from this prefix is caught here, not by the
    carve-out quietly going dead."""
    from app.utils.rag_urls import _PRIVATE_IP_DENIAL_PREFIX

    allowed, reason = await check_ssrf_safe("http://10.55.55.55:11434/api/tags")
    assert allowed is False
    assert reason.startswith(_PRIVATE_IP_DENIAL_PREFIX), reason


# ---------------------------------------------------------------------------
# codex r2 diff-review High: redact_url_userinfo() itself.
# ---------------------------------------------------------------------------

def test_redact_url_userinfo_strips_credentials():
    from app.utils.url_validators import redact_url_userinfo

    assert redact_url_userinfo("http://u:p@ollama:11434") == "http://ollama:11434"
    assert redact_url_userinfo("http://u:p@ollama:11434/api/tags?x=1") == "http://ollama:11434"
    assert redact_url_userinfo("https://token@ollama.example.com/v1") == "https://ollama.example.com"


def test_redact_url_userinfo_preserves_scheme_host_port_no_userinfo():
    from app.utils.url_validators import redact_url_userinfo

    assert redact_url_userinfo("http://localhost:11434") == "http://localhost:11434"
    assert redact_url_userinfo("http://ollama:11434/api/tags") == "http://ollama:11434"


def test_redact_url_userinfo_brackets_ipv6():
    from app.utils.url_validators import redact_url_userinfo

    assert redact_url_userinfo("http://u:p@[::1]:11434/api/tags") == "http://[::1]:11434"


def test_redact_url_userinfo_never_raises_on_garbage_input():
    from app.utils.url_validators import redact_url_userinfo

    assert redact_url_userinfo("not a url at all ][") == "<unparseable-url>"
    assert redact_url_userinfo("") == "<unparseable-url>"
