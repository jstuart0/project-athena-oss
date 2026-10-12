"""shared.ha_link: the passive Home Assistant link status.

Nothing here talks to Home Assistant or Redis for real. Responses come from
httpx.MockTransport, Redis is an in-memory async fake, and every clock is
injected, so no test sleeps. The token ``sekret-token-123`` and the host
``ha.test`` are fixtures; neither may appear in any summary or log line.
"""
from __future__ import annotations

import ast
import asyncio
import json
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import structlog

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shared import ha_link  # noqa: E402
from shared.ha_client import HomeAssistantClient  # noqa: E402
from shared.ha_registry import HARegistry, create_registry  # noqa: E402

TOKEN = "sekret-token-123"
HOST = "ha.test"
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class Clock:
    """An injected wall clock that only moves when a test says so."""

    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeRedis:
    """The few async hash commands the publisher uses, in memory."""

    def __init__(self):
        self.hashes: dict = {}
        self.ttl: dict = {}
        self.fail = None
        self.calls: list = []

    def _check(self, name):
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail

    async def hset(self, key, field, value):
        self._check("hset")
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key, seconds):
        self._check("expire")
        self.ttl[key] = seconds

    async def hgetall(self, key):
        self._check("hgetall")
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, *fields):
        self._check("hdel")
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)


def monitor_at(clock=None) -> tuple:
    clock = clock or Clock()
    return ha_link.HALinkMonitor(wall=clock), clock


def publisher(monitor, redis, replica="pod-a", *, clock, registry=None, configured=True, mono=None, **kw):
    return ha_link.HALinkPublisher(
        monitor, registry if registry is not None else HARegistry(None), redis, replica,
        configured=configured, wall=clock, monotonic=mono or (lambda: 0.0), **kw,
    )


def request(path="/api/states", method="GET"):
    return httpx.Request(method, f"https://{HOST}{path}")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,status,expected",
    [
        ("GET", "/api/states", 200, "ok"),
        ("GET", "/api/", 200, "ok"),
        ("POST", "/api/services/light/turn_on", 200, "ok"),
        ("POST", "/api/services/light/turn_on", 204, "ok"),
        ("GET", "/api/states/light.missing", 404, "ok"),
        ("GET", "/api/states", 401, "auth_rejected"),
        ("POST", "/api/services/light/turn_on", 401, "auth_rejected"),
        ("GET", "/api/states", 403, "forbidden"),
        ("GET", "/api/states", 500, "ha_error"),
        ("POST", "/api/services/light/turn_on", 502, "ha_error"),
        ("GET", "/api/states", 503, "ha_error"),
        # admin-only HA paths say nothing about the link or the token
        ("GET", "/api/config/automation/config/x", 401, None),
        ("POST", "/api/config/automation/config/x", 200, None),
        ("POST", "/api/config/automation/config/x", 500, None),
        # a reload is admin-only too
        ("POST", "/api/services/automation/reload", 401, None),
        ("POST", "/api/services/automation/reload", 200, None),
        # a bad service call or payload is the caller's fault
        ("POST", "/api/services/light/turn_on", 400, None),
        ("POST", "/api/services/light/turn_on", 422, None),
        ("GET", "/api/states", 302, None),
        ("GET", "/some/other/path", 200, None),
    ],
)
def test_only_non_admin_ha_paths_count_and_only_these_statuses_classify(method, path, status, expected):
    assert ha_link.evidence(method, path, status) == expected


def _ssl_connect_error(attribute):
    error = httpx.ConnectError("handshake")
    cause = ssl.SSLCertVerificationError("bad certificate")
    setattr(error, attribute, cause)
    return error


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ConnectError("refused"), "unreachable"),
        (_ssl_connect_error("__cause__"), "tls_error"),
        (_ssl_connect_error("__context__"), "tls_error"),
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ReadTimeout("t"), "timeout"),
        (httpx.PoolTimeout("t"), "timeout"),
        (httpx.ReadError("r"), "unreachable"),
        (httpx.RemoteProtocolError("p"), "unreachable"),
        (ValueError("not a transport problem"), "error"),
    ],
)
def test_exceptions_classify_by_type(exc, expected):
    assert ha_link.exception_outcome(exc) == expected


def test_an_ssl_error_that_is_not_a_connect_error_is_not_tls_error():
    error = httpx.ReadError("r")
    error.__cause__ = ssl.SSLError("x")
    assert ha_link.exception_outcome(error) == "unreachable"


# ---------------------------------------------------------------------------
# The monitor: timing and logging
# ---------------------------------------------------------------------------

def test_a_failure_before_any_success_leaves_success_empty():
    monitor, clock = monitor_at()
    monitor.record_response("GET", "/api/states", 401)
    s = monitor.summary()
    assert s["last_outcome"] == "auth_rejected"
    assert s["last_success_at"] is None
    assert s["consecutive_failures"] == 1


def test_a_success_then_three_failures_keeps_the_success_time_and_counts_three():
    monitor, clock = monitor_at()
    monitor.record_response("GET", "/api/states", 200)
    success_at = monitor.summary()["last_success_at"]
    for _ in range(3):
        clock.advance(1)
        monitor.record_response("GET", "/api/states", 500)
    s = monitor.summary()
    assert s["last_success_at"] == success_at
    assert s["consecutive_failures"] == 3
    assert s["last_outcome"] == "ha_error"


def test_a_success_resets_the_failure_count():
    monitor, clock = monitor_at()
    monitor.record_response("GET", "/api/states", 401)
    clock.advance(1)
    monitor.record_response("GET", "/api/states", 200)
    s = monitor.summary()
    assert (s["last_outcome"], s["consecutive_failures"]) == ("ok", 0)
    assert s["last_success_at"] == s["last_outcome_at"]


def test_the_summary_has_exactly_four_keys():
    monitor, _ = monitor_at()
    assert set(monitor.summary()) == {"last_outcome", "last_outcome_at", "last_success_at", "consecutive_failures"}


def test_a_backwards_clock_does_not_crash_and_the_latest_recorded_outcome_wins():
    monitor, clock = monitor_at()
    monitor.record_response("GET", "/api/states", 200)
    clock.advance(-3600)
    monitor.record_response("GET", "/api/states", 401)
    s = monitor.summary()
    assert s["last_outcome"] == "auth_rejected"
    assert datetime.fromisoformat(s["last_outcome_at"]) > datetime.fromisoformat(s["last_success_at"])
    assert ha_link.link_state(
        {"configured": True, "rest": s, "registry": ha_link.disabled_registry_summary()}
    ) == ("failing", "token_rejected")


def test_paths_that_do_not_count_record_nothing_and_do_not_wake_the_publisher():
    monitor, _ = monitor_at()
    monitor.record_response("GET", "/api/config/automation/config/x", 401)
    assert monitor.summary()["last_outcome"] is None
    assert not monitor.changed.is_set()


def test_every_recorded_outcome_sets_the_changed_event():
    monitor, _ = monitor_at()
    monitor.record_response("GET", "/api/states", 200)
    assert monitor.changed.is_set()


def test_two_consecutive_401s_log_one_state_change():
    monitor, clock = monitor_at()
    with structlog.testing.capture_logs() as logs:
        monitor.record_response("GET", "/api/states", 401)
        clock.advance(1)
        monitor.record_response("GET", "/api/states", 401)
    changes = [entry for entry in logs if entry["event"] == "ha_link_state_changed"]
    assert len(changes) == 1
    assert changes[0]["outcome"] == "auth_rejected"


def test_nothing_logged_or_summarised_contains_exception_text_a_host_or_the_token():
    monitor, clock = monitor_at()
    nasty = httpx.ConnectError(f"cannot reach https://{HOST}/api/states with Bearer {TOKEN}")
    with structlog.testing.capture_logs() as logs:
        monitor.record_exception(nasty)
        clock.advance(1)
        monitor.record_exception(ValueError(f"{TOKEN} {HOST}"))
    blob = json.dumps([monitor.summary(), logs], default=str)
    for secret in (TOKEN, HOST, "cannot reach", "Bearer"):
        assert secret not in blob
    assert {e["error_class"] for e in logs} == {"ConnectError", "ValueError"}


def test_cancellation_records_nothing():
    monitor, _ = monitor_at()
    monitor.record_exception(asyncio.CancelledError())
    assert monitor.summary()["last_outcome"] is None


# ---------------------------------------------------------------------------
# RecordingTransport
# ---------------------------------------------------------------------------

def test_the_transport_returns_the_identical_response_and_records_it():
    monitor, _ = monitor_at()
    response = httpx.Response(200, json=[])

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            return response

    transport = ha_link.RecordingTransport(Inner(), monitor)
    assert run(transport.handle_async_request(request())) is response
    assert monitor.summary()["last_outcome"] == "ok"


def test_the_transport_reraises_the_identical_exception_and_records_it():
    monitor, _ = monitor_at()
    boom = httpx.ConnectError("refused")

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            raise boom

    transport = ha_link.RecordingTransport(Inner(), monitor)
    with pytest.raises(httpx.ConnectError) as caught:
        run(transport.handle_async_request(request()))
    assert caught.value is boom
    assert monitor.summary()["last_outcome"] == "unreachable"


def test_a_cancelled_request_records_nothing_and_propagates():
    monitor, _ = monitor_at()

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            raise asyncio.CancelledError()

    transport = ha_link.RecordingTransport(Inner(), monitor)
    with pytest.raises(asyncio.CancelledError):
        run(transport.handle_async_request(request()))
    assert monitor.summary()["last_outcome"] is None


def test_a_body_read_failure_after_the_status_line_is_not_recorded_again():
    monitor, _ = monitor_at()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("mid-body")
            yield b""

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            return httpx.Response(200, stream=Body())

    async def go():
        async with httpx.AsyncClient(transport=ha_link.RecordingTransport(Inner(), monitor)) as client:
            with pytest.raises(httpx.ReadError):
                await client.get(f"https://{HOST}/api/states")

    run(go())
    s = monitor.summary()
    assert (s["last_outcome"], s["consecutive_failures"]) == ("ok", 0)


# ---------------------------------------------------------------------------
# The real clients, over a mock inner transport
# ---------------------------------------------------------------------------

@pytest.fixture
def inner_mock(monkeypatch):
    """Replace the real network transport under both HA clients with a
    MockTransport whose responses a test queues. Records the verify flag."""
    state = {"responses": [], "requests": [], "verify": []}

    def handler(req):
        state["requests"].append(req)
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json=[]) if isinstance(item, int) else item

    def factory(**kwargs):
        state["verify"].append(kwargs.get("verify"))
        return httpx.MockTransport(handler)

    monkeypatch.setattr(ha_link.httpx, "AsyncHTTPTransport", factory)
    return state


def _ha_client(monitor):
    return HomeAssistantClient(url=f"https://{HOST}", token=TOKEN, link_monitor=monitor)


def _entity_manager(monitor):
    from orchestrator.ha_entity_manager import HAEntityManager

    return HAEntityManager(f"https://{HOST}", TOKEN, link_monitor=monitor)


@pytest.mark.parametrize(
    "build,call",
    [
        (_ha_client, lambda c: c.get_states()),
        (_entity_manager, lambda c: c.refresh_entities()),
    ],
    ids=["HomeAssistantClient.get_states", "HAEntityManager.refresh_entities"],
)
def test_a_real_client_records_a_401_then_a_success(inner_mock, build, call):
    monitor, clock = monitor_at()
    client = build(monitor)
    inner_mock["responses"] += [401, 200]

    async def go():
        with pytest.raises(httpx.HTTPStatusError):
            await call(client)
        after_401 = monitor.summary()
        clock.advance(1)
        await call(client)
        return after_401, monitor.summary()

    after_401, after_200 = run(go())
    assert after_401["last_outcome"] == "auth_rejected"
    assert after_401["last_success_at"] is None
    assert (after_200["last_outcome"], after_200["consecutive_failures"]) == ("ok", 0)
    assert after_200["last_success_at"] is not None
    assert inner_mock["verify"] == [False]


@pytest.mark.parametrize("build", [_ha_client, _entity_manager], ids=["HomeAssistantClient", "HAEntityManager"])
def test_the_proxy_environment_variables_are_ignored_by_the_ha_clients(monkeypatch, inner_mock, build):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    monitor, _ = monitor_at()
    client = build(monitor)
    inner_mock["responses"].append(200)
    run(client.client.get("/api/states"))
    assert len(inner_mock["requests"]) == 1
    assert inner_mock["requests"][0].url.host == HOST


def test_a_client_without_a_monitor_is_unchanged(monkeypatch):
    monkeypatch.setattr(ha_link.httpx, "AsyncHTTPTransport", lambda **kw: pytest.fail("no recording transport expected"))
    client = HomeAssistantClient(url=f"https://{HOST}", token=TOKEN)
    assert client.client is not None


@pytest.mark.parametrize(
    "url,token,expected",
    [(None, TOKEN, False), (f"https://{HOST}", None, False), (f"https://{HOST}", TOKEN, True)],
    ids=["token-only", "url-only", "both"],
)
def test_configured_is_the_clients_own_url_and_token_rule(monkeypatch, url, token, expected):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monitor, clock = monitor_at()
    # main.py builds the client only when a token exists; mirror that.
    client = HomeAssistantClient(url=url, token=token, link_monitor=monitor) if token else None
    pub = ha_link.create_publisher(monitor, HARegistry(None), None, client, replica_id="pod-a")
    assert pub.view()["configured"] is expected
    wanted = "not_configured" if not expected else "unknown"
    assert ha_link.link_state(pub.view())[0] == wanted


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def iso(offset_seconds=0, base=T0):
    return (base + timedelta(seconds=offset_seconds)).isoformat()


def rest_part(outcome=None, at=None, ok=None, fails=0):
    return {"last_outcome": outcome, "last_outcome_at": at, "last_success_at": ok, "consecutive_failures": fails}


def registry_part(state="ok", areas=3, ok=None, fail=None, cls=None):
    return {"state": state, "areas": areas, "last_success_at": ok, "last_failure_at": fail, "last_failure_class": cls}


def entry(published=0, *, configured=True, rest=None, registry=None):
    return {
        "configured": configured,
        "rest": rest or rest_part(),
        "registry": registry or registry_part("disabled", None),
        "published_at": iso(published),
    }


def merged(entries, now=T0, seen=None, **kw):
    return ha_link.merge(entries, now, {} if seen is None else seen, **kw)


def test_the_newest_outcome_wins_and_success_is_the_max():
    a = entry(-5, rest=rest_part("auth_rejected", iso(-10), iso(-300), 2))
    b = entry(-5, rest=rest_part("ok", iso(-60), iso(-60), 0))
    view = merged({"a": a, "b": b})
    assert view["rest"] == {
        "last_outcome": "auth_rejected", "last_outcome_at": iso(-10),
        "last_success_at": iso(-60), "consecutive_failures": 2,
    }
    assert view["replicas_reporting"] == 2


def test_a_later_success_on_one_replica_beats_an_earlier_failure_on_another():
    a = entry(-5, rest=rest_part("auth_rejected", iso(-120), None, 1))
    b = entry(-5, rest=rest_part("ok", iso(-30), iso(-30), 0))
    view = merged({"a": a, "b": b})
    assert view["rest"]["last_outcome"] == "ok"
    assert ha_link.link_state(view) == ("connected", "ok")


def test_the_worst_registry_state_wins():
    entries = {
        "a": entry(registry=registry_part("ok", 4, iso(-100))),
        "b": entry(registry=registry_part("refused", None, None, iso(-50), "auth")),
        "c": entry(registry=registry_part("unavailable", None, None, iso(-20), "timeout")),
    }
    view = merged(entries)
    assert view["registry"]["state"] == "refused"
    assert view["registry"]["last_failure_class"] == "timeout"  # from the newest failure
    assert view["registry"]["last_failure_at"] == iso(-20)
    assert view["registry"]["areas"] == 4  # from the entry with the newest registry success
    assert view["registry"]["last_success_at"] == iso(-100)


def test_registry_order_is_refused_over_unavailable_over_ok_over_disabled():
    def state_of(*states):
        return merged({str(i): entry(registry=registry_part(s, None)) for i, s in enumerate(states)})["registry"]["state"]

    assert state_of("disabled", "ok") == "ok"
    assert state_of("ok", "unavailable") == "unavailable"
    assert state_of("unavailable", "refused", "ok") == "refused"
    assert state_of("disabled", "disabled") == "disabled"


def test_an_entry_older_than_the_window_is_dropped_by_the_readers_clock():
    seen = {}
    old = entry(-1)
    assert merged({"a": old}, now=T0, seen=seen) is not None
    later = T0 + timedelta(seconds=901)
    assert merged({"a": old}, now=later, seen=seen) is None
    assert merged({"a": old}, now=T0 + timedelta(seconds=900), seen=seen) is not None


def test_malformed_entries_are_dropped_and_good_ones_kept():
    bad = [
        None, "x", [], {}, entry(0) | {"configured": "yes"}, entry(0) | {"published_at": "not a time"},
        entry(0) | {"rest": rest_part("zzz")}, entry(0) | {"rest": rest_part("ok", "yesterday")},
        entry(0) | {"rest": rest_part(fails=-1)}, entry(0) | {"registry": registry_part("zzz")},
        entry(0) | {"registry": registry_part(areas="7")}, entry(0) | {"registry": registry_part(cls="weird")},
        entry(0) | {"rest": "nope"},
    ]
    entries = {f"bad{i}": value for i, value in enumerate(bad)}
    assert merged(entries) is None
    entries["good"] = entry(0, rest=rest_part("ok", iso(-1), iso(-1)))
    view = merged(entries)
    assert view["replicas_reporting"] == 1 and view["rest"]["last_outcome"] == "ok"


def test_configured_is_true_when_any_replica_is_configured():
    view = merged({"a": entry(configured=False), "b": entry(configured=True)})
    assert view["configured"] is True
    assert merged({"a": entry(configured=False)})["configured"] is False


# -- clock skew --------------------------------------------------------------

def test_published_at_five_seconds_ahead_is_kept_and_six_is_dropped():
    assert merged({"a": entry(5)}) is not None
    assert merged({"a": entry(6)}) is None


def test_a_future_event_time_is_clamped_and_a_real_later_success_elsewhere_wins():
    seen = {}
    skewed = entry(0, rest=rest_part("auth_rejected", iso(600), None, 1))
    good = entry(0, rest=rest_part("ok", iso(2), iso(2), 0))
    view = merged({"skewed": skewed, "good": good}, now=T0, seen=seen)
    clamped = datetime.fromisoformat(view["rest"]["last_outcome_at"])
    assert clamped <= T0
    # the real success happens after the first sight of the skewed entry
    later_now = T0 + timedelta(seconds=30)
    good2 = entry(30, rest=rest_part("ok", iso(20), iso(20), 0))
    view2 = merged({"skewed": skewed, "good": good2}, now=later_now, seen=seen)
    assert view2["rest"]["last_outcome"] == "ok"


def test_a_dead_replica_publishing_ten_minutes_ahead_is_dropped_while_it_is_ahead():
    dead = entry(600, rest=rest_part("auth_rejected", iso(600), None, 1))
    assert merged({"dead": dead}) is None


def test_an_accepted_skewed_replica_never_outranks_later_events_and_expires_from_first_sight():
    seen = {}
    ahead = entry(4, rest=rest_part("auth_rejected", iso(4), None, 1))
    assert merged({"a": ahead}, now=T0, seen=seen)["rest"]["last_outcome"] == "auth_rejected"
    # a real success seen 20 s after the first sight is newer than anything the
    # skewed replica can claim (its events are capped at first_seen + skew)
    later = T0 + timedelta(seconds=20)
    fresh = entry(20, rest=rest_part("ok", iso(20), iso(20), 0))
    assert merged({"a": ahead, "b": fresh}, now=later, seen=seen)["rest"]["last_outcome"] == "ok"
    # it stops publishing: gone `window` seconds after the reader first saw it,
    # not `window` after its own published_at
    assert merged({"a": ahead}, now=T0 + timedelta(seconds=900), seen=seen) is not None
    assert merged({"a": ahead}, now=T0 + timedelta(seconds=901), seen=seen) is None


def test_a_replica_whose_clock_runs_behind_only_looks_older():
    behind = entry(-300, rest=rest_part("auth_rejected", iso(-300), None, 1))
    fresh = entry(0, rest=rest_part("ok", iso(-10), iso(-10), 0))
    view = merged({"behind": behind, "fresh": fresh})
    assert view["rest"]["last_outcome"] == "ok"


# ---------------------------------------------------------------------------
# The publisher
# ---------------------------------------------------------------------------

def two_replicas():
    redis, clock = FakeRedis(), Clock()
    ma, _ = monitor_at(clock)
    mb, _ = monitor_at(clock)
    mono = {"t": 0.0}
    pa = publisher(ma, redis, "pod-a", clock=clock, mono=lambda: mono["t"])
    pb = publisher(mb, redis, "pod-b", clock=clock, mono=lambda: mono["t"])
    return redis, clock, ma, mb, pa, pb


def test_a_401_on_one_replica_is_visible_in_the_others_view_after_one_cycle():
    redis, clock, ma, mb, pa, pb = two_replicas()
    ma.record_response("GET", "/api/states", 401)

    async def go():
        await pa.publish_once()
        await pb.publish_once()

    run(go())
    view = pb.view()
    assert view["coverage"] == "cluster" and view["replicas_reporting"] == 2
    assert view["rest"]["last_outcome"] == "auth_rejected"
    assert ha_link.link_state(view) == ("failing", "token_rejected")


def test_the_publisher_writes_only_enums_numbers_and_timestamps_and_expires_the_key():
    redis, clock, ma, mb, pa, pb = two_replicas()
    ma.record_response("GET", "/api/states", 200)
    run(pa.publish_once())
    raw = redis.hashes[ha_link.REDIS_KEY]["pod-a"]
    assert set(json.loads(raw)) == {"configured", "rest", "registry", "published_at"}
    assert redis.ttl[ha_link.REDIS_KEY] == 86400
    assert TOKEN not in raw and HOST not in raw


def test_a_redis_failure_falls_back_to_this_replicas_view_and_logs_once():
    redis, clock, ma, mb, pa, pb = two_replicas()
    ma.record_response("GET", "/api/states", 200)
    redis.fail = ConnectionError("redis down " + TOKEN)
    with structlog.testing.capture_logs() as logs:
        run(pa.publish_once())
        run(pa.publish_once())
    view = pa.view()
    assert (view["coverage"], view["coverage_reason"]) == ("replica", "redis_error")
    assert view["configured"] is True  # the fallback still says so
    assert view["rest"]["last_outcome"] == "ok"
    failures = [e for e in logs if e["event"] == "ha_link_cluster_view_unavailable"]
    assert len(failures) == 1 and failures[0]["error_class"] == "ConnectionError"
    assert TOKEN not in json.dumps(logs, default=str)


def test_the_view_recovers_when_redis_returns():
    redis, clock, ma, mb, pa, pb = two_replicas()
    redis.fail = ConnectionError("down")
    run(pa.publish_once())
    assert pa.view()["coverage_reason"] == "redis_error"
    redis.fail = None
    run(pa.publish_once())
    assert pa.view()["coverage"] == "cluster"


def test_no_redis_means_a_local_view_that_says_why():
    monitor, clock = monitor_at()
    pub = publisher(monitor, None, clock=clock)
    run(pub.publish_once())
    view = pub.view()
    assert (view["coverage"], view["coverage_reason"]) == ("replica", "redis_unconfigured")
    assert run(pub.run()) is None  # never starts without Redis


def test_a_merge_older_than_three_intervals_falls_back_to_the_local_view():
    redis, clock, ma, mb, pa, pb = two_replicas()
    mono = {"t": 0.0}
    pa = publisher(ma, redis, clock=clock, mono=lambda: mono["t"])
    run(pa.publish_once())
    mono["t"] = 3 * 15.0
    assert pa.view()["coverage"] == "cluster"
    mono["t"] = 3 * 15.0 + 0.1
    assert (pa.view()["coverage"], pa.view()["coverage_reason"]) == ("replica", "redis_error")


def test_view_does_no_io():
    redis, clock, ma, mb, pa, pb = two_replicas()
    run(pa.publish_once())
    calls = len(redis.calls)
    for _ in range(5):
        pa.view()
    assert len(redis.calls) == calls


def test_stale_peers_are_removed_best_effort_but_never_this_replica():
    redis, clock, ma, mb, pa, pb = two_replicas()
    redis.hashes[ha_link.REDIS_KEY] = {"ancient": json.dumps(entry(-7200)), "pod-a": json.dumps(entry(-7200))}
    run(pa.publish_once())
    assert "ancient" not in redis.hashes[ha_link.REDIS_KEY]
    assert "pod-a" in redis.hashes[ha_link.REDIS_KEY]


def test_a_malformed_value_in_the_hash_is_ignored():
    redis, clock, ma, mb, pa, pb = two_replicas()
    redis.hashes[ha_link.REDIS_KEY] = {"junk": "{not json"}
    run(pa.publish_once())
    view = pa.view()
    assert view["coverage"] == "cluster" and view["replicas_reporting"] == 1


def test_the_loop_publishes_on_a_recorded_outcome_without_waiting_the_interval():
    redis, clock, ma, mb, pa, pb = two_replicas()
    gaps = []

    async def fake_sleep(seconds):
        gaps.append(seconds)

    pub = publisher(ma, redis, clock=clock, sleep=fake_sleep, interval=3600.0)

    async def go():
        task = asyncio.create_task(pub.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first = len(redis.calls)
        ma.record_response("GET", "/api/states", 401)
        for _ in range(10):
            await asyncio.sleep(0)
        second = len(redis.calls)
        await pub.close()
        return first, second, task

    first, second, task = run(go())
    assert second > first
    stored = json.loads(redis.hashes[ha_link.REDIS_KEY]["pod-a"])
    assert stored["rest"]["last_outcome"] == "auth_rejected"
    assert gaps and set(gaps) == {1.0}
    assert task.done()


def test_close_stops_the_loop():
    redis, clock, ma, mb, pa, pb = two_replicas()

    async def go():
        task = asyncio.create_task(pa.run())
        await asyncio.sleep(0)
        await pa.close()
        return task

    assert run(go()).done()


# -- configured --------------------------------------------------------------

def test_an_unconfigured_install_with_a_disabled_registry_reads_not_configured():
    monitor, clock = monitor_at()
    pub = publisher(monitor, None, clock=clock, configured=False)
    view = pub.view()
    assert view["configured"] is False
    assert ha_link.link_state(view) == ("not_configured", "not_configured")


def test_ha_areas_off_with_a_configured_client_and_a_rest_success_is_connected():
    monitor, clock = monitor_at()
    registry = create_registry(ha_url=f"https://{HOST}", ha_token=TOKEN, mode="off")
    pub = publisher(monitor, None, clock=clock, registry=registry, configured=True)
    monitor.record_response("GET", "/api/states", 200)
    assert pub.view()["registry"]["state"] == "disabled"
    assert ha_link.link_state(pub.view()) == ("connected", "ok")


def test_one_configured_replica_makes_the_merged_view_configured():
    redis, clock = FakeRedis(), Clock()
    ma, _ = monitor_at(clock)
    mb, _ = monitor_at(clock)
    pa = publisher(ma, redis, "pod-a", clock=clock, configured=False)
    pb = publisher(mb, redis, "pod-b", clock=clock, configured=True)

    async def go():
        await pa.publish_once()
        await pb.publish_once()
        await pa.publish_once()

    run(go())
    assert pa.view()["configured"] is True


# ---------------------------------------------------------------------------
# Every HA REST channel in src/ records into the monitor
# ---------------------------------------------------------------------------

_HA_NAMES = {"ha_url", "HA_URL"}


def _functions(tree):
    def walk(node, quals):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield ".".join(quals + [child.name]), child
                yield from walk(child, quals + [child.name])
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, quals + [child.name])
            else:
                yield from walk(child, quals)

    yield from walk(tree, [])


def _holds_bearer(fn) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str) and "Bearer" in part.value:
                    return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("Bearer "):
            return True
    return False


def _names_ha_url(fn) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in _HA_NAMES:
            return True
        if (isinstance(node, ast.Attribute) and node.attr == "url"
                and isinstance(node.value, ast.Name) and node.value.id == "self"):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "HA_URL" in node.value:
            return True
    return False


def _is_httpx_client(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "AsyncClient"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx")


def ha_client_sites(source: str, relpath: str):
    """``(relpath, qualified function, passes transport=)`` for every function
    that builds an httpx.AsyncClient with a Bearer header and names the HA URL."""
    sites = set()
    for qualified, fn in _functions(ast.parse(source)):
        clients = [n for n in ast.walk(fn) if _is_httpx_client(n)]
        if clients and _holds_bearer(fn) and _names_ha_url(fn):
            recorded = any(kw.arg == "transport" for c in clients for kw in c.keywords)
            sites.add((relpath, qualified, recorded))
    return sites


EXPECTED_HA_CLIENT_SITES = {
    ("shared/ha_client.py", "HomeAssistantClient.__init__"),
    ("orchestrator/ha_entity_manager.py", "HAEntityManager.__init__"),
    ("orchestrator/room_probe.py", "_live_states"),
    ("gateway/main.py", "_fetch_assist_satellite_states"),
    ("gateway/main.py", "send_satellite_announcement"),
}
RECORDED_SITES = {
    ("shared/ha_client.py", "HomeAssistantClient.__init__"),
    ("orchestrator/ha_entity_manager.py", "HAEntityManager.__init__"),
}


class TestHALinkChannels:
    @staticmethod
    def _scan():
        found = set()
        for path in sorted(SRC.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8")
                found |= ha_client_sites(text, path.relative_to(SRC).as_posix())
            except SyntaxError:  # src/jetson/athena_lite_llm.py has never parsed; unrelated to HA
                continue
        return found

    def test_the_population_of_ha_rest_channels_is_exactly_the_known_five(self):
        found = self._scan()
        assert len(found) >= 5
        assert {(p, q) for p, q, _ in found} == EXPECTED_HA_CLIENT_SITES, (
            "a new httpx client holding the HA bearer token appeared (or one moved); "
            "the orchestrator's must pass transport=recording_transport(...)"
        )

    def test_both_orchestrator_channels_pass_a_recording_transport(self):
        recorded = {(p, q) for p, q, passes in self._scan() if passes}
        assert recorded == RECORDED_SITES

    def test_an_extra_unrecorded_ha_client_is_detected(self):
        synthetic = (
            "import httpx\n"
            "class Sneaky:\n"
            "    def __init__(self, ha_url, token):\n"
            "        self.c = httpx.AsyncClient(base_url=ha_url, headers={'Authorization': f'Bearer {token}'})\n"
        )
        sites = ha_client_sites(synthetic, "orchestrator/sneaky.py")
        assert sites == {("orchestrator/sneaky.py", "Sneaky.__init__", False)}

    def test_an_unrelated_bearer_client_is_not_flagged(self):
        other = (
            "import httpx\n"
            "def search(api_key):\n"
            "    return httpx.AsyncClient(headers={'Authorization': f'Bearer {api_key}'})\n"
        )
        assert ha_client_sites(other, "orchestrator/search.py") == set()


def test_lifespan_builds_the_publisher_from_the_raw_ha_client():
    """main.py's `configured` must come from the HA client's own answer (a URL and a token), not from
    whether a client object exists."""
    tree = ast.parse((SRC / "orchestrator" / "main.py").read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "create_publisher"
    ]
    assert len(calls) == 1
    assert any(isinstance(a, ast.Name) and a.id == "ha_client_raw" for a in calls[0].args)
