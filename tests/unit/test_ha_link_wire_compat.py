"""A rolling upgrade: old and new replicas share one Redis hash and one marker.

The old code (9e80241) is frozen here as a replica: its vocabularies, its strict
``_Entry.parse``, and the way the area registry read its refusal marker. Every
value the new code writes into a field the old code reads must be a word the old
code knows, and every key the old code reads must still be there.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import httpx
import pytest

from shared import ha_auth, ha_link

from . import ha_auth_scenarios as scenarios
from .ha_auth_fakes import Clock as WallClock, credentials, make_latch

ROOT = pathlib.Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

# -- 9e80241's vocabulary and reader, frozen ---------------------------------------------------

V1_OUTCOMES = frozenset({"ok", "auth_rejected", "forbidden", "ha_error", "timeout", "unreachable", "tls_error", "error"})
V1_REGISTRY_STATES = ("refused", "unavailable", "ok", "disabled")
V1_FAILURE_CLASSES = frozenset({"auth", "timeout", "unreachable", "other"})
V1_REST_KEYS = {"last_outcome", "last_outcome_at", "last_success_at", "consecutive_failures"}
V1_REGISTRY_KEYS = {"state", "areas", "last_success_at", "last_failure_at", "last_failure_class"}
V1_ENTRY_KEYS = {"configured", "rest", "registry", "published_at"}


def _nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def v1_parse(raw: Any) -> Optional[dict]:
    """The strict reader old replicas run: any unknown word drops the whole entry."""
    if not isinstance(raw, Mapping):
        return None
    rest, registry = raw.get("rest"), raw.get("registry")
    if not isinstance(raw.get("configured"), bool) or not isinstance(rest, Mapping) or not isinstance(registry, Mapping):
        return None
    if ha_link._parse_time(raw.get("published_at")) is None:
        return None
    outcome = rest.get("last_outcome")
    if outcome is not None and not (isinstance(outcome, str) and outcome in V1_OUTCOMES):
        return None
    if _nonneg_int(rest.get("consecutive_failures")) is None:
        return None
    for source in (rest.get("last_outcome_at"), rest.get("last_success_at"), registry.get("last_success_at"), registry.get("last_failure_at")):
        if source is not None and ha_link._parse_time(source) is None:
            return None
    state = registry.get("state")
    if not (isinstance(state, str) and state in V1_REGISTRY_STATES):
        return None
    areas = registry.get("areas")
    if areas is not None and _nonneg_int(areas) is None:
        return None
    failure_class = registry.get("last_failure_class")
    if failure_class is not None and not (isinstance(failure_class, str) and failure_class in V1_FAILURE_CLASSES):
        return None
    return dict(raw)


def v1_marker_key(token: str, service_api_key: Optional[str]) -> Optional[str]:
    if not token or not service_api_key:
        return None
    return hmac.new(service_api_key.encode(), token.encode(), hashlib.sha256).hexdigest()[:16]


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


STATUSES = [
    ("GET", "/api/states", 200), ("GET", "/api/states", 401), ("GET", "/api/states", 403), ("GET", "/api/states", 500),
    ("POST", "/api/services/light/turn_on", 200), ("POST", "/api/services/light/turn_on", 401),
    ("POST", "/api/services/light/turn_on", 403), ("POST", "/api/services/light/turn_on", 502),
    ("GET", "/api", 403), ("GET", "/api/states/light.a", 403), ("GET", "/api/states/light.a", 404),
]
EXCEPTIONS = [
    httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"),
    ValueError("x"), RuntimeError("x"),
]


def _monitors():
    for method, path, status in STATUSES:
        monitor = ha_link.HALinkMonitor(wall=Clock())
        monitor.record_response(method, path, status)
        yield f"{method} {path} {status}", monitor
    for exc in EXCEPTIONS:
        monitor = ha_link.HALinkMonitor(wall=Clock())
        monitor.record_exception(exc)
        yield type(exc).__name__, monitor
    monitor = ha_link.HALinkMonitor(wall=Clock())
    for method, path, status in STATUSES:
        monitor.record_response(method, path, status)
    yield "every status in a row", monitor


def _entry(monitor, registry=None, clock=None):
    clock = clock or Clock()
    from shared.ha_registry import HARegistry

    class _Redis:
        pass

    publisher = ha_link.HALinkPublisher(monitor, registry or HARegistry(None), None, "pod-a", configured=True, wall=clock)
    return json.loads(json.dumps(publisher._local_entry()))


@pytest.mark.parametrize("name,monitor", list(_monitors()), ids=lambda v: v if isinstance(v, str) else "")
def test_every_rest_state_the_monitor_can_reach_is_a_word_the_old_reader_knows(name, monitor):
    summary = monitor.summary()
    assert summary["last_outcome"] in V1_OUTCOMES | {None}
    assert V1_REST_KEYS <= set(summary)
    entry = _entry(monitor)
    assert v1_parse(entry) is not None, "the strict old reader must accept the new entry"
    assert V1_ENTRY_KEYS <= set(entry) and V1_REGISTRY_KEYS <= set(entry["registry"])


def test_the_registry_states_and_failure_classes_it_can_reach_are_v1_words():
    from shared.ha_registry import AuthInvalid, HARegistry

    async def reach(error):
        async def fetcher():
            raise error

        registry = HARegistry(fetcher, min_refresh_interval=0)
        await registry.ensure(1.0)
        return registry

    for error in (AuthInvalid(), OSError("x"), TimeoutError(), asyncio.TimeoutError(), ValueError("x"), RuntimeError("x")):
        registry = run(reach(error))
        summary = registry.link_summary()
        assert summary["state"] in V1_REGISTRY_STATES
        assert summary["last_failure_class"] in V1_FAILURE_CLASSES | {None}
        assert v1_parse(_entry(ha_link.HALinkMonitor(wall=Clock()), registry)) is not None


def test_a_golden_entry_from_the_new_publisher_parses_in_the_old_reader_and_keeps_its_old_keys():
    monitor = ha_link.HALinkMonitor(wall=Clock())
    monitor.record_response("GET", "/api/states", 403)
    entry = _entry(monitor)
    assert entry["rest"]["ban_suspected"] is True, "the new fact rides in a new key"
    assert v1_parse(entry) is not None
    assert set(entry["rest"]) == V1_REST_KEYS | {"ban_suspected"}
    assert set(entry["registry"]) == V1_REGISTRY_KEYS | {"ban_suspected"}


def test_an_entry_the_old_code_published_is_read_by_the_new_reader_with_no_ban_flag():
    entry = {
        "configured": True,
        "rest": {"last_outcome": "forbidden", "last_outcome_at": T0.isoformat(), "last_success_at": None, "consecutive_failures": 1},
        "registry": {"state": "ok", "areas": 3, "last_success_at": (T0 - timedelta(seconds=60)).isoformat(), "last_failure_at": None, "last_failure_class": None},
        "published_at": T0.isoformat(),
    }
    assert v1_parse(entry) is not None
    view = ha_link.merge({"old-pod": entry}, T0, {})
    assert view["rest"]["ban_suspected"] is False and view["registry"]["ban_suspected"] is False
    assert ha_link.link_state({**view, "configured": True}) == ("attention", "forbidden")


def test_an_old_replica_drops_an_entry_with_a_word_it_does_not_know_and_the_new_reader_does_not():
    entry = _entry(ha_link.HALinkMonitor(wall=Clock()))
    entry["rest"]["last_outcome"] = "a_word_from_the_future"
    assert v1_parse(entry) is None
    assert ha_link.merge({"x": entry}, T0, {}) is not None


# -- the values the code can write are literals from the v1 sets (a static check) -------------------


def _string_constants(node: ast.AST):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_the_outcome_words_ha_link_can_write_are_v1_words():
    tree = ast.parse((ROOT / "src" / "shared" / "ha_link.py").read_text())
    written = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("evidence", "exception_outcome"):
            for ret in [n for n in ast.walk(node) if isinstance(n, ast.Return)]:
                written |= _string_constants(ret) if ret.value is not None else set()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_record":
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    written.add(arg.value)
    written.discard(None)
    assert written and written <= V1_OUTCOMES, written - V1_OUTCOMES


def test_the_registry_states_and_failure_classes_ha_registry_can_write_are_v1_words():
    tree = ast.parse((ROOT / "src" / "shared" / "ha_registry.py").read_text())
    states, classes = set(), set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "").startswith("STATE_"):
            states |= _string_constants(node.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_note_failure":
            classes |= {a.value for a in node.args if isinstance(a, ast.Constant)}
        if isinstance(node, ast.FunctionDef) and node.name == "_failure_class":
            classes |= {r.value.value for r in ast.walk(node) if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)}
    assert states and states <= set(V1_REGISTRY_STATES), states
    assert classes and classes <= V1_FAILURE_CLASSES, classes


# -- the old registry still reads the marker ----------------------------------------------------------


def test_the_marker_the_old_registry_reads_exists_exactly_while_the_new_latch_is_set():
    async def scenario():
        clock = WallClock()
        from shared import ha_auth

        redis = ha_auth.MemoryRedis(clock=clock)
        snap = credentials()
        latch = make_latch(redis, clock=clock)
        old_marker = ha_auth.LEGACY_MARKER_PREFIX + v1_marker_key(snap.token.reveal(), "service-key-for-tests")
        admission = await latch.admit(snap, **scenarios.READ)
        before = await redis.exists(old_marker)
        await scenarios.settle(latch, snap, admission, "auth_rejected")
        during = (await redis.exists(old_marker), await redis.get(old_marker), await redis.ttl(old_marker))
        clock.advance(121)
        probe = await latch.admit(snap, **scenarios.READ)
        await scenarios.settle(latch, snap, probe, "proof_ok_ws")
        return before, during, await redis.exists(old_marker)

    before, during, after = run(scenario())
    assert before == 0 and after == 0
    exists, value, ttl = during
    assert exists == 1 and value == "1" and 0 < ttl <= 86400


def test_the_new_code_reads_a_marker_an_old_registry_wrote_as_a_latch():
    async def scenario():
        clock = WallClock()
        from shared import ha_auth

        redis = ha_auth.MemoryRedis(clock=clock)
        snap = credentials()
        await redis.set(ha_auth.LEGACY_MARKER_PREFIX + v1_marker_key(snap.token.reveal(), "service-key-for-tests"), "1", ex=86400)
        return await make_latch(redis, clock=clock).admit(snap, **scenarios.READ)

    admission = run(scenario())
    assert (admission.kind, admission.reason) == ("withheld", "latched")
