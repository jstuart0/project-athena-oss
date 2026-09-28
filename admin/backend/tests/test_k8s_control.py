"""ATHENA-118 Phase 2: T5 (adapter), T6 (name validation), T7 (construction
and degraded states).

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T5-T7.

Mocking strategy: the Kubernetes API is a real HTTP boundary this repo
doesn't own -- faked at the httpx transport level with an ORDERED-response
script, because the adapter's contract is a strict sequence (GET-then-
remember-then-PATCH, poll-then-PATCH-in-finally) and these tests assert
that exact sequence, not just eventual success. What this does NOT prove:
that the real `/scale` subresource actually accepts merge-patch+json the
way the fake assumes, or that real RBAC matches T10's parsed YAML -- both
are closed by Phase 6's live checks, not here.
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

import asyncio
import json as _json
import ssl
from pathlib import Path

import httpx
import pytest
import structlog

from app.services import k8s_control as kc
from app.services import service_managers as sm
from app.services.service_control_settings import recall_replicas, remember_replicas
from shared.config import _clear_cache_for_tests

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _reset_client_cache():
    kc._clear_client_cache()
    yield
    kc._clear_client_cache()


class _ScriptedTransport:
    """Ordered-response fake. Each script entry is
    (method, path_suffix_or_None, status, json_body). `path_suffix_or_None`
    is matched with `endswith` so callers don't need the namespace prefix
    spelled out repeatedly."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.script:
            return httpx.Response(500, json={"error": "scripted transport exhausted"})
        method, path_suffix, status, body = self.script.pop(0)
        if method is not None:
            assert request.method == method, f"expected {method}, got {request.method} {request.url.path}"
        if path_suffix is not None:
            assert request.url.path.endswith(path_suffix), f"expected path ending {path_suffix}, got {request.url.path}"
        return httpx.Response(status, json=body)


def _client(script, token="tok-fake", protected=frozenset({"athena-admin-backend", "athena-admin-frontend"}), **kwargs):
    transport = _ScriptedTransport(script)
    token_dir = kwargs.pop("token_dir", None)
    token_path = str(token_dir / "token") if token_dir else "/nonexistent/token"
    if token_dir:
        (token_dir / "token").write_text(token)
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443",
        namespace="athena-prod",
        token_path=token_path,
        ca_path="/nonexistent/ca.crt",
        transport=httpx.MockTransport(transport.handler),
        protected_names=protected,
        **kwargs,
    )
    return client, transport


def _fake_token_client(tmp_path, script, token="tok-fake", **kwargs):
    return _client(script, token=token, token_dir=tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# T5.1 — set_replicas exact request shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_replicas_exact_patch_shape(tmp_path):
    client, transport = _fake_token_client(tmp_path, [
        ("PATCH", "/scale", 200, {"spec": {"replicas": 3}}),
    ])
    await client.set_replicas("athena-rag-tesla", 3)

    req = transport.requests[0]
    assert req.method == "PATCH"
    assert req.url.path.endswith("/deployments/athena-rag-tesla/scale")
    assert req.headers["Content-Type"] == "application/merge-patch+json"
    assert _json.loads(req.content) == {"spec": {"replicas": 3}}


# ---------------------------------------------------------------------------
# T5.2/3 — stop: GET -> remember -> PATCH; idempotent no-op at 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_at_replicas_2_remembers_then_patches_zero(tmp_path):
    client, transport = _fake_token_client(tmp_path, [
        ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv-1"}}),
        ("PATCH", "/scale", 200, {}),
    ])
    remembered = []
    result = await client.stop("athena-rag-tesla", remember=remembered.append)

    assert remembered == [2]
    assert result.success is True
    patch_req = transport.requests[1]
    body = _json.loads(patch_req.content)
    assert body["spec"]["replicas"] == 0
    assert body["metadata"]["resourceVersion"] == "rv-1"


@pytest.mark.asyncio
async def test_stop_already_at_zero_is_idempotent_no_op(tmp_path):
    client, transport = _fake_token_client(tmp_path, [
        ("GET", "/scale", 200, {"spec": {"replicas": 0}, "status": {"replicas": 0}, "metadata": {"resourceVersion": "rv-1"}}),
    ])
    remembered = []
    result = await client.stop("athena-rag-tesla", remember=remembered.append)

    assert remembered == []
    assert result.success is True
    assert result.message == "already stopped"
    assert len(transport.requests) == 1  # only the GET -- no PATCH


# ---------------------------------------------------------------------------
# T5.4 — start: recall value forwarded verbatim into the PATCH body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("recalled,expected", [(3, 3), (1, 1), (10, 10)])
async def test_start_forwards_recalled_replica_count(tmp_path, recalled, expected):
    client, transport = _fake_token_client(tmp_path, [
        ("GET", "/scale", 200, {"spec": {"replicas": 0}, "status": {"replicas": 0}, "metadata": {"resourceVersion": "rv-2"}}),
        ("PATCH", "/scale", 200, {}),
    ])
    result = await client.start("athena-rag-tesla", recall=lambda: recalled)

    assert result.success is True
    body = _json.loads(transport.requests[1].content)
    assert body["spec"]["replicas"] == expected
    assert body["metadata"]["resourceVersion"] == "rv-2"


def test_recall_replicas_clamps_absent_and_out_of_range(db):
    """The clamp itself lives in recall_replicas (service_control_settings),
    not in the adapter -- start() just forwards whatever recall() returns."""
    assert recall_replicas(db, "never-remembered") == 1
    remember_replicas(db, "athena-rag-tesla", 3)
    assert recall_replicas(db, "athena-rag-tesla") == 3
    remember_replicas(db, "athena-rag-tesla", 99)
    assert recall_replicas(db, "athena-rag-tesla") == 10
    remember_replicas(db, "athena-rag-tesla", 0)  # no-op: a 0 is never stored
    assert recall_replicas(db, "athena-rag-tesla") == 10


# ---------------------------------------------------------------------------
# T5.5/6/7/8 — restart: happy path, timeout, mid-poll exception, invariant scan
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def _instant_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_restart_happy_path_sequence(tmp_path):
    clock = _FakeClock()

    async def sleep_and_advance(seconds):
        clock.advance(seconds)

    client, transport = _fake_token_client(
        tmp_path,
        [
            ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv-3"}}),
            ("PATCH", "/scale", 200, {}),  # scale to 0
            ("GET", "/scale", 200, {"spec": {"replicas": 0}, "status": {"replicas": 0}, "metadata": {"resourceVersion": "rv-4"}}),  # first poll: settled
            ("PATCH", "/scale", 200, {}),  # scale back to 2
        ],
        clock=clock, sleep_fn=sleep_and_advance,
    )
    remembered = []
    result = await client.restart("athena-rag-tesla", remember=remembered.append, recall=lambda: 2)

    assert remembered == [2]
    assert result.success is True
    scale_to_zero_body = _json.loads(transport.requests[1].content)
    assert scale_to_zero_body["spec"]["replicas"] == 0
    scale_back_body = _json.loads(transport.requests[3].content)
    assert scale_back_body == {"spec": {"replicas": 2}}  # no resourceVersion on the must-land PATCH


@pytest.mark.asyncio
async def test_restart_timeout_still_scales_back_and_reports_failure(tmp_path):
    clock = _FakeClock()

    async def sleep_and_advance(seconds):
        clock.advance(seconds)

    # The poll GET never reports status.replicas == 0 -- every poll returns 1.
    script = [
        ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv"}}),
        ("PATCH", "/scale", 200, {}),  # scale to 0
    ]
    for _ in range(60):  # exactly 60 polls at 1s/poll before the 60s bound trips
        script.append(("GET", "/scale", 200, {"spec": {"replicas": 0}, "status": {"replicas": 1}, "metadata": {"resourceVersion": "rv"}}))
    script.append(("PATCH", "/scale", 200, {}))  # the must-land scale-back

    client, transport = _fake_token_client(tmp_path, script, clock=clock, sleep_fn=sleep_and_advance, poll_interval=1.0, wait_timeout=60.0)
    result = await client.restart("athena-rag-tesla", remember=lambda n: None, recall=lambda: 2)

    assert result.success is False
    assert "did not terminate" in result.message
    last_request_body = _json.loads(transport.requests[-1].content)
    assert last_request_body == {"spec": {"replicas": 2}}
    assert transport.requests[-1].method == "PATCH"


@pytest.mark.asyncio
async def test_restart_exception_mid_poll_still_scales_back_and_reraises(tmp_path):
    clock = _FakeClock()

    async def sleep_and_advance(seconds):
        clock.advance(seconds)

    class _RaisingTransport(_ScriptedTransport):
        def handler(self, request):
            self.requests.append(request)
            method, path_suffix, status, body = self.script.pop(0)
            if body == "RAISE":
                raise httpx.ConnectError("simulated failure", request=request)
            return httpx.Response(status, json=body)

    script = [
        ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv"}}),
        ("PATCH", "/scale", 200, {}),  # scale to 0
        ("GET", "/scale", 500, "RAISE"),  # 2nd poll GET raises
        ("PATCH", "/scale", 200, {}),  # must-land scale-back
    ]
    transport = _RaisingTransport(script)
    (tmp_path / "token").write_text("tok-fake")
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443", namespace="athena-prod",
        token_path=str(tmp_path / "token"), ca_path="/nonexistent/ca.crt",
        transport=httpx.MockTransport(transport.handler), protected_names=frozenset(),
        clock=clock, sleep_fn=sleep_and_advance,
    )

    with pytest.raises(kc.K8sControlError):
        await client.restart("athena-rag-tesla", remember=lambda n: None, recall=lambda: 2)

    last_request_body = _json.loads(transport.requests[-1].content)
    assert last_request_body == {"spec": {"replicas": 2}}
    assert transport.requests[-1].method == "PATCH"


@pytest.mark.asyncio
async def test_restart_cancellation_during_wait_still_scales_back(tmp_path):
    clock = _FakeClock()
    release_event = asyncio.Event()

    async def blocking_sleep(_seconds):
        await release_event.wait()

    client, transport = _fake_token_client(
        tmp_path,
        [
            ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv"}}),
            ("PATCH", "/scale", 200, {}),  # scale to 0
            ("PATCH", "/scale", 200, {}),  # must-land scale-back (shielded)
        ],
        clock=clock, sleep_fn=blocking_sleep,
    )

    task = asyncio.ensure_future(client.restart("athena-rag-tesla", remember=lambda n: None, recall=lambda: 2))
    await asyncio.sleep(0.05)  # let it reach the blocked sleep inside the poll loop
    task.cancel()
    release_event.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Give the shielded scale-back task a moment to actually complete.
    for _ in range(20):
        if len(transport.requests) >= 3:
            break
        await asyncio.sleep(0.01)

    assert len(transport.requests) == 3
    last_request_body = _json.loads(transport.requests[-1].content)
    assert last_request_body == {"spec": {"replicas": 2}}


@pytest.mark.asyncio
async def test_restart_second_cancellation_while_scaleback_patch_in_flight_still_completes(tmp_path):
    """tessa P2 mid-build High: dropping `asyncio.shield` around the
    `finally`'s scale-back PATCH leaves every existing test green, because
    the prior cancellation test only ever cancels once, before the
    scale-back PATCH has even been sent -- the shield's actual job (a
    SECOND cancellation landing while the shielded PATCH is genuinely
    in-flight, waiting on the transport) was never exercised. This test
    blocks the transport's response to the scale-back PATCH on its own
    event, delivers a second cancel while that PATCH is in flight, and
    proves the PATCH still completes with the remembered replica count
    regardless."""
    clock = _FakeClock()
    poll_release = asyncio.Event()
    patch_in_flight = asyncio.Event()
    patch_release = asyncio.Event()
    patch_count = {"n": 0}
    patch_completed = {"done": False}

    async def blocking_sleep(_seconds):
        await poll_release.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={
                "spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv"},
            })
        # PATCH
        patch_count["n"] += 1
        if patch_count["n"] == 1:
            return httpx.Response(200, json={})  # scale-to-0
        # The must-land scale-back PATCH: block until released. If the
        # second cancellation reaches this coroutine (the bug this test
        # exists to catch), CancelledError propagates out of
        # `patch_release.wait()` and `patch_completed["done"]` is NEVER
        # set -- distinct from merely being entered, which happens either way.
        patch_in_flight.set()
        await patch_release.wait()
        patch_completed["done"] = True
        return httpx.Response(200, json={})

    (tmp_path / "token").write_text("tok-fake")
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443", namespace="athena-prod",
        token_path=str(tmp_path / "token"), ca_path="/nonexistent/ca.crt",
        transport=httpx.MockTransport(handler), protected_names=frozenset(),
        clock=clock, sleep_fn=blocking_sleep,
    )

    task = asyncio.ensure_future(client.restart("athena-rag-tesla", remember=lambda n: None, recall=lambda: 2))
    await asyncio.sleep(0.05)  # let it reach the blocked poll-loop sleep
    task.cancel()  # first cancellation: unblocks the poll sleep's wait()

    # Let the CancelledError propagate into the `finally` clause, which
    # starts the shielded scale-back PATCH -- wait until that PATCH is
    # actually in flight (blocked on the transport) before cancelling again.
    for _ in range(50):
        if patch_in_flight.is_set():
            break
        await asyncio.sleep(0.01)
    assert patch_in_flight.is_set(), "scale-back PATCH never reached the transport"

    task.cancel()  # second cancellation: must NOT abort the in-flight shielded PATCH
    patch_release.set()  # let the blocked PATCH response through

    with pytest.raises(asyncio.CancelledError):
        await task

    # Give the shielded scale-back task a moment to actually finish running
    # in the background (it's independent of `task`'s own completion).
    for _ in range(50):
        if patch_completed["done"]:
            break
        await asyncio.sleep(0.01)

    assert patch_count["n"] == 2
    assert patch_completed["done"] is True, (
        "the scale-back PATCH was entered but never completed -- the second "
        "cancellation reached it, meaning asyncio.shield isn't protecting it"
    )


@pytest.mark.asyncio
async def test_no_request_targets_base_deployment_resource_except_list_get(tmp_path):
    """Invariant scan (T5.8): every recorded request across every scenario
    above either is the list GET, or ends in /scale."""
    scenarios = []

    clock = _FakeClock()

    async def sleep_and_advance(seconds):
        clock.advance(seconds)

    client, transport = _fake_token_client(
        tmp_path,
        [
            ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv"}}),
            ("PATCH", "/scale", 200, {}),
            ("GET", "/scale", 200, {"spec": {"replicas": 0}, "status": {"replicas": 0}, "metadata": {"resourceVersion": "rv2"}}),
            ("PATCH", "/scale", 200, {}),
        ],
        clock=clock, sleep_fn=sleep_and_advance,
    )
    await client.restart("athena-rag-tesla", remember=lambda n: None, recall=lambda: 2)
    scenarios.extend(transport.requests)

    for req in scenarios:
        path = req.url.path
        if req.method == "GET":
            assert path.endswith("/scale") or path.endswith("/deployments")
        else:
            assert path.endswith("/scale"), f"non-GET request to non-scale path: {req.method} {path}"


# ---------------------------------------------------------------------------
# T5.9 — bearer token re-read per request (rotation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_is_re_read_per_request(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("tok-v1")
    transport = _ScriptedTransport([
        ("GET", "/scale", 200, {"spec": {"replicas": 1}, "status": {"replicas": 1}, "metadata": {"resourceVersion": "rv"}}),
        ("GET", "/scale", 200, {"spec": {"replicas": 1}, "status": {"replicas": 1}, "metadata": {"resourceVersion": "rv"}}),
    ])
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443", namespace="athena-prod",
        token_path=str(token_file), ca_path="/nonexistent/ca.crt",
        transport=httpx.MockTransport(transport.handler), protected_names=frozenset(),
    )
    await client.get_scale("athena-rag-tesla")
    token_file.write_text("tok-v2")
    await client.get_scale("athena-rag-tesla")

    assert transport.requests[0].headers["Authorization"] == "Bearer tok-v1"
    assert transport.requests[1].headers["Authorization"] == "Bearer tok-v2"


# ---------------------------------------------------------------------------
# T5.10 — status -> kind mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_kind", [
    (401, "forbidden"), (403, "forbidden"), (404, "not_found"),
    (409, "conflict"), (422, "api_error"), (429, "api_error"), (500, "api_error"),
])
async def test_status_to_kind_mapping(tmp_path, status, expected_kind):
    client, _transport = _fake_token_client(tmp_path, [("GET", "/scale", status, {})])
    with pytest.raises(kc.K8sControlError) as exc_info:
        await client.get_scale("athena-rag-tesla")
    assert exc_info.value.kind == expected_kind


@pytest.mark.asyncio
async def test_connect_error_and_timeout_map_to_unavailable(tmp_path):
    def _raise_connect(request):
        raise httpx.ConnectError("nope", request=request)

    def _raise_timeout(request):
        raise httpx.TimeoutException("slow", request=request)

    for handler in (_raise_connect, _raise_timeout):
        transport = httpx.MockTransport(handler)
        (tmp_path / "token").write_text("tok")
        client = kc.K8sDeploymentClient(
            api_base="https://k8s.test:443", namespace="athena-prod",
            token_path=str(tmp_path / "token"), ca_path="/nonexistent/ca.crt",
            transport=transport, protected_names=frozenset(),
        )
        with pytest.raises(kc.K8sControlError) as exc_info:
            await client.get_scale("athena-rag-tesla")
        assert exc_info.value.kind == "unavailable"


# ---------------------------------------------------------------------------
# T5.11 — IPv6 KUBERNETES_SERVICE_HOST bracketing
# ---------------------------------------------------------------------------

def test_ipv6_host_is_bracketed():
    assert kc._bracket_if_ipv6("fd00::1") == "[fd00::1]"
    assert kc._bracket_if_ipv6("10.0.0.1") == "10.0.0.1"


@pytest.mark.asyncio
async def test_get_k8s_client_ipv6_api_base(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "true")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    _clear_cache_for_tests()

    token_path = tmp_path / "token"
    token_path.write_text("tok")
    monkeypatch.setattr(kc, "TOKEN_PATH_DEFAULT", str(token_path))
    monkeypatch.setattr(kc, "NAMESPACE_PATH_DEFAULT", str(tmp_path / "namespace"))

    client, reason = kc.get_k8s_client(force=True)
    assert reason is None
    assert client is not None
    assert client._api_base == "https://[fd00::1]:443"
    _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# T5.12 — resourceVersion on stop/start PATCHes, absent on restart scale-back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resource_version_present_on_stop_absent_on_restart_scaleback(tmp_path):
    clock = _FakeClock()

    async def sleep_and_advance(seconds):
        clock.advance(seconds)

    client, transport = _fake_token_client(
        tmp_path,
        [
            ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv-x"}}),
            ("PATCH", "/scale", 200, {}),
        ],
    )
    await client.stop("athena-rag-tesla", remember=lambda n: None)
    stop_body = _json.loads(transport.requests[1].content)
    assert stop_body["metadata"]["resourceVersion"] == "rv-x"

    client2, transport2 = _fake_token_client(
        tmp_path,
        [
            ("GET", "/scale", 200, {"spec": {"replicas": 2}, "status": {"replicas": 2}, "metadata": {"resourceVersion": "rv-y"}}),
            ("PATCH", "/scale", 200, {}),
            ("GET", "/scale", 200, {"spec": {"replicas": 0}, "status": {"replicas": 0}, "metadata": {"resourceVersion": "rv-z"}}),
            ("PATCH", "/scale", 200, {}),
        ],
        clock=clock, sleep_fn=sleep_and_advance,
    )
    await client2.restart("athena-rag-tesla", remember=lambda n: None, recall=lambda: 2)
    scaleback_body = _json.loads(transport2.requests[-1].content)
    assert "metadata" not in scaleback_body


# ---------------------------------------------------------------------------
# T5.13 — bearer never leaks (4 separate failure modes)
# ---------------------------------------------------------------------------

SECRET_TOKEN = "tok-SECRET-123"


def _assert_token_absent(exc, logs):
    haystacks = [str(exc), repr(exc)]
    cause = exc.__cause__
    context = exc.__context__
    if cause is not None:
        haystacks.append(str(cause))
    if context is not None:
        haystacks.append(str(context))
    for entry in logs:
        haystacks.append(str(entry))
    for haystack in haystacks:
        assert SECRET_TOKEN not in haystack


@pytest.mark.asyncio
async def test_bearer_never_leaks_connect_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)
    (tmp_path / "token").write_text(SECRET_TOKEN)
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443", namespace="athena-prod",
        token_path=str(tmp_path / "token"), ca_path="/nonexistent/ca.crt",
        transport=httpx.MockTransport(handler), protected_names=frozenset(),
    )
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(kc.K8sControlError) as exc_info:
            await client.get_scale("athena-rag-tesla")
    _assert_token_absent(exc_info.value, logs)


@pytest.mark.asyncio
async def test_bearer_never_leaks_403(tmp_path):
    client, _t = _fake_token_client(tmp_path, [("GET", "/scale", 403, {})], token=SECRET_TOKEN)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(kc.K8sControlError) as exc_info:
            await client.get_scale("athena-rag-tesla")
    _assert_token_absent(exc_info.value, logs)


@pytest.mark.asyncio
async def test_bearer_never_leaks_500(tmp_path):
    client, _t = _fake_token_client(tmp_path, [("GET", "/scale", 500, {})], token=SECRET_TOKEN)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(kc.K8sControlError) as exc_info:
            await client.get_scale("athena-rag-tesla")
    _assert_token_absent(exc_info.value, logs)


@pytest.mark.asyncio
async def test_bearer_never_leaks_timeout(tmp_path):
    def handler(request):
        raise httpx.TimeoutException("slow", request=request)
    (tmp_path / "token").write_text(SECRET_TOKEN)
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443", namespace="athena-prod",
        token_path=str(tmp_path / "token"), ca_path="/nonexistent/ca.crt",
        transport=httpx.MockTransport(handler), protected_names=frozenset(),
    )
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(kc.K8sControlError) as exc_info:
            await client.get_scale("athena-rag-tesla")
    _assert_token_absent(exc_info.value, logs)


# ---------------------------------------------------------------------------
# T5.14 — verify= is an SSLContext, not a string
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_is_ssl_context_not_string(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("tok")
    client = kc.K8sDeploymentClient(
        api_base="https://k8s.test:443", namespace="athena-prod",
        token_path=str(tmp_path / "token"), ca_path="/nonexistent/ca.crt",
        transport=None, protected_names=frozenset(),
    )
    captured = {}
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        captured['verify'] = kwargs.get('verify')
        kwargs['transport'] = httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []}))
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await client.list_deployments()
    assert isinstance(captured['verify'], ssl.SSLContext)


# ---------------------------------------------------------------------------
# T6 — name validation (population of 6, named tests, zero transport calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_name_traversal_rejected(tmp_path):
    client, transport = _fake_token_client(tmp_path, [])
    with pytest.raises(kc.K8sControlError):
        await client.get_scale("../x")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_name_uppercase_rejected(tmp_path):
    client, transport = _fake_token_client(tmp_path, [])
    with pytest.raises(kc.K8sControlError):
        await client.get_scale("Athena")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_name_too_long_rejected(tmp_path):
    client, transport = _fake_token_client(tmp_path, [])
    with pytest.raises(kc.K8sControlError):
        await client.get_scale("a" * 64)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_name_empty_rejected(tmp_path):
    client, transport = _fake_token_client(tmp_path, [])
    with pytest.raises(kc.K8sControlError):
        await client.get_scale("")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_name_protected_admin_backend_rejected_distinct_kind(tmp_path):
    client, transport = _fake_token_client(tmp_path, [])
    with pytest.raises(kc.K8sControlError) as exc_info:
        await client.get_scale("athena-admin-backend")
    assert transport.requests == []
    assert exc_info.value.kind == "invalid_name"


@pytest.mark.asyncio
async def test_name_protected_admin_frontend_rejected(tmp_path):
    client, transport = _fake_token_client(tmp_path, [])
    with pytest.raises(kc.K8sControlError):
        await client.get_scale("athena-admin-frontend")
    assert transport.requests == []


# ---------------------------------------------------------------------------
# T7 — construction and degraded states
# ---------------------------------------------------------------------------

def test_get_k8s_client_disabled_never_touches_filesystem(monkeypatch):
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "false")
    _clear_cache_for_tests()

    def _raise(*args, **kwargs):
        raise AssertionError("filesystem touched while flag is disabled")

    monkeypatch.setattr(Path, "exists", _raise)
    try:
        client, reason = kc.get_k8s_client(force=True)
    finally:
        monkeypatch.undo()
    assert client is None
    assert reason == "disabled"


def test_get_k8s_client_not_in_cluster(monkeypatch):
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "true")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    _clear_cache_for_tests()
    client, reason = kc.get_k8s_client(force=True)
    assert client is None
    assert reason == "not_in_cluster"


def test_get_k8s_client_no_token_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "true")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _clear_cache_for_tests()
    monkeypatch.setattr(kc, "TOKEN_PATH_DEFAULT", str(tmp_path / "no-such-token"))
    client, reason = kc.get_k8s_client(force=True)
    assert client is None
    assert reason == "no_service_account_token"


@pytest.mark.asyncio
async def test_gather_inventory_wrong_sa_forbidden_on_list(monkeypatch, tmp_path):
    """[otto #4] Token present but the wrong SA -- a real, reachable-in-
    practice state distinct from 'no token file at all'."""
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "true")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _clear_cache_for_tests()
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    monkeypatch.setattr(kc, "TOKEN_PATH_DEFAULT", str(token_path))
    monkeypatch.setattr(kc, "NAMESPACE_PATH_DEFAULT", str(tmp_path / "namespace"))

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(403, json={}))
        kwargs.pop("verify", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    kc._clear_client_cache()

    inv = await sm._gather_kubernetes_inventory()
    assert inv.enabled is True
    assert inv.available is False
    assert inv.reason == "forbidden"
    kc._clear_client_cache()
