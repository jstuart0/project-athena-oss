"""get_rag_service_url: an unreachable candidate is skipped, not returned first.

Probes a real loopback listener and a real closed port; nothing stubs
``asyncio.open_connection``. Time is an injected clock.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

import orchestrator.nodes  # noqa: F401,E402  (must load before helpers; see helpers.py docstring)
import orchestrator.helpers as h  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def reachability_state(monkeypatch):
    """Process-global cache, in-flight and WARN-once state start empty and end empty."""
    h._reset_reachability_state()
    clock = Clock()
    monkeypatch.setattr(h, "_reach_clock", clock)
    log = MagicMock()
    monkeypatch.setattr(h, "logger", log)
    yield clock
    h._reset_reachability_state()


@pytest.fixture
def clock(reachability_state):
    return reachability_state


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def listener(port: int = 0):
    async def handle(_r, w):
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    return server, server.sockets[0].getsockname()[1]


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def wire(monkeypatch, *, override=None, registry=None, env=None, intent="weather"):
    class Admin:
        async def get_intent_routing(self):
            return {intent: {"rag_service_url": override}} if override is not None else {}

    async def lookup(_intent):
        return registry

    monkeypatch.setattr(h, "get_admin_client", lambda: Admin())
    monkeypatch.setattr(h, "registry_get_service_url", lookup)
    monkeypatch.setattr(h, "WEATHER_SERVICE_URL", env)


def run(coro):
    return asyncio.run(coro)


def test_a_dead_override_falls_through_to_the_registry(monkeypatch):
    async def main():
        server, live = await listener()
        wire(monkeypatch, override=url(closed_port()), registry=url(live), env=url(closed_port()))
        try:
            assert await h.get_rag_service_url("weather") == url(live)
        finally:
            server.close()

    run(main())


def test_a_reachable_override_still_wins_over_a_working_registry(monkeypatch):
    async def main():
        a, pa = await listener()
        b, pb = await listener()
        wire(monkeypatch, override=url(pa), registry=url(pb), env=url(pb))
        try:
            assert await h.get_rag_service_url("weather") == url(pa)
        finally:
            a.close()
            b.close()

    run(main())


def test_a_null_registry_row_falls_to_env(monkeypatch):
    async def main():
        server, live = await listener()
        wire(monkeypatch, override=None, registry=None, env=url(live))
        try:
            assert await h.get_rag_service_url("weather") == url(live)
        finally:
            server.close()

    run(main())


def test_registry_miss_is_info_and_url_free(monkeypatch):
    async def main():
        server, live = await listener()
        wire(monkeypatch, registry=None, env=url(live))
        try:
            await h.get_rag_service_url("weather")
        finally:
            server.close()

    run(main())
    names = [c.args[0] for c in h.logger.info.call_args_list]
    assert "rag_url_registry_miss" in names
    assert "rag_url_resolved" in names


def test_all_candidates_dead_returns_the_first_non_empty_one(monkeypatch):
    override, registry, env = url(closed_port()), url(closed_port()), url(closed_port())
    wire(monkeypatch, override=override, registry=registry, env=env)
    assert run(h.get_rag_service_url("weather")) == override
    wire(monkeypatch, override=None, registry=registry, env=env)
    assert run(h.get_rag_service_url("weather")) == registry
    wire(monkeypatch, override=None, registry=None, env=env)
    assert run(h.get_rag_service_url("weather")) == env


def test_no_candidate_at_all_is_none(monkeypatch):
    wire(monkeypatch, override=None, registry=None, env=None)
    assert run(h.get_rag_service_url("weather")) is None


def test_the_warning_fires_once_per_dead_url_and_never_carries_the_url(monkeypatch):
    dead = url(closed_port())
    wire(monkeypatch, override=None, registry=None, env=dead)
    run(h.get_rag_service_url("weather"))
    run(h.get_rag_service_url("weather"))
    warnings = [c for c in h.logger.warning.call_args_list if c.args[0] == "rag_url_unreachable"]
    assert len(warnings) == 1
    assert warnings[0].kwargs == {"source": "env", "intent": "weather"}
    everything = repr(h.logger.mock_calls)
    assert "127.0.0.1" not in everything


def test_a_second_dead_url_warns_again(monkeypatch):
    wire(monkeypatch, env=url(closed_port()))
    run(h.get_rag_service_url("weather"))
    wire(monkeypatch, env=url(closed_port()))
    run(h.get_rag_service_url("weather"))
    assert sum(c.args[0] == "rag_url_unreachable" for c in h.logger.warning.call_args_list) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.10.10:8080",
        "http://[fe80::1]:8080",
        "http://[::ffff:169.254.169.254]/",
        "http://0.0.0.0:8080",
        "http://224.0.0.1:8080",
        "ftp://127.0.0.1:21",
        "file:///etc/passwd",
        "http://",
        "not a url",
    ],
)
def test_metadata_link_local_and_non_http_urls_are_refused_without_a_connection(bad):
    started = time.monotonic()
    assert run(h._reachable(bad)) is False
    assert time.monotonic() - started < h.REACH_TIMEOUT_SECONDS


def test_an_unresolvable_host_is_bounded_and_negative():
    started = time.monotonic()
    assert run(h._reachable("http://no-such-host.invalid:8080")) is False
    assert time.monotonic() - started < h.REACH_TIMEOUT_SECONDS + 1.0


def test_positive_results_are_cached_for_60_seconds_then_probed_again(clock):
    async def main():
        server, live = await listener()
        assert await h._reachable(url(live)) is True
        server.close()
        await server.wait_closed()
        clock.now += h.REACH_POSITIVE_TTL_SECONDS - 1
        assert await h._reachable(url(live)) is True  # still cached
        clock.now += 2
        assert await h._reachable(url(live)) is False  # expired, probed again, dead

    run(main())


def test_negative_results_are_cached_for_15_seconds_then_probed_again(clock):
    async def main():
        port = closed_port()
        assert await h._reachable(url(port)) is False
        server, _ = await listener(port)
        try:
            clock.now += h.REACH_NEGATIVE_TTL_SECONDS - 1
            assert await h._reachable(url(port)) is False  # still cached
            clock.now += 2
            assert await h._reachable(url(port)) is True
        finally:
            server.close()

    run(main())


def test_two_concurrent_callers_share_one_probe(monkeypatch):
    async def main():
        server, live = await listener()
        real_probe = h._probe_url
        calls = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated(u):
            calls.append(u)
            entered.set()
            await release.wait()
            return await real_probe(u)

        monkeypatch.setattr(h, "_probe_url", gated)
        first = asyncio.create_task(h._reachable(url(live)))
        await entered.wait()
        second = asyncio.create_task(h._reachable(url(live)))
        for _ in range(5):
            await asyncio.sleep(0)  # the second caller is now parked on the first's future
        assert len(h._reach_inflight) == 1
        release.set()
        try:
            assert await asyncio.gather(first, second) == [True, True]
        finally:
            server.close()
        assert len(calls) == 1
        assert h._reach_inflight == {}

    run(main())


def test_the_cache_never_holds_more_than_the_cap():
    async def main():
        base = 30000
        for offset in range(h.REACH_CACHE_MAX_ENTRIES + 6):
            await h._reachable(url(base + offset))
        assert len(h._reach_cache) == h.REACH_CACHE_MAX_ENTRIES
        assert f"http://127.0.0.1:{base}" not in h._reach_cache  # oldest evicted
        assert f"http://127.0.0.1:{base + h.REACH_CACHE_MAX_ENTRIES + 5}" in h._reach_cache

    run(main())


def test_the_warned_set_is_bounded():
    for n in range(h._REACH_WARNED_MAX + 10):
        h._warn_unreachable_once(f"http://warn-{n}.example:80", "env", "weather")
    assert len(h._reach_warned) == h._REACH_WARNED_MAX


def test_a_non_http_scheme_is_refused_even_when_something_is_listening():
    async def main():
        server, live = await listener()
        try:
            assert await h._reachable(f"http://127.0.0.1:{live}") is True  # positive control
            assert await h._reachable(f"ftp://127.0.0.1:{live}") is False
            assert await h._reachable(f"gopher://127.0.0.1:{live}") is False
        finally:
            server.close()

    run(main())


def test_link_local_addresses_are_forbidden_and_loopback_is_not():
    async def main():
        server, live = await listener()
        try:
            assert h._forbidden_probe_address("127.0.0.1") is False  # loopback stays allowed
            assert await h._reachable(f"http://127.0.0.1:{live}") is True
            assert h._forbidden_probe_address("169.254.169.254") is True
            assert h._forbidden_probe_address("fe80::1%en0") is True
        finally:
            server.close()

    run(main())


def test_a_cancelled_probe_caches_nothing_warns_nothing_and_does_not_poison_a_waiter(monkeypatch):
    async def main():
        server, live = await listener()
        real_probe = h._probe_url
        entered = asyncio.Event()
        hang = True

        async def probe(u):
            nonlocal hang
            if hang:
                hang = False
                entered.set()
                await asyncio.sleep(30)
            return await real_probe(u)

        monkeypatch.setattr(h, "_probe_url", probe)
        leader = asyncio.create_task(h._reachable(url(live)))
        await entered.wait()
        waiter = asyncio.create_task(h._reachable(url(live)))
        for _ in range(5):
            await asyncio.sleep(0)
        leader.cancel()
        try:
            assert await waiter is True  # re-probed on its own, not handed the leader's failure
        finally:
            server.close()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert h._reach_inflight == {}
        assert h._reach_cache[f"http://127.0.0.1:{live}"][0] is True

    run(main())
    assert not h.logger.warning.called


def test_a_cancelled_probe_leaves_no_negative_entry_and_no_warning():
    async def main():
        port = closed_port()
        wire_url = url(port)
        task = asyncio.create_task(h._reachable(wire_url))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h._reach_cache == {} and h._reach_inflight == {}

    run(main())
    assert not h.logger.warning.called


@pytest.mark.parametrize(
    "addr",
    [
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "2002:a9fe:a9fe::1",  # 6to4 embedding 169.254.169.254
        "64:ff9b::a9fe:a9fe",  # NAT64 embedding 169.254.169.254
        "64:ff9b::169.254.169.254",
        "2002:0000:0000::1",  # 6to4 embedding 0.0.0.0
        "64:ff9b::e000:1",  # NAT64 embedding 224.0.0.1
    ],
)
def test_embedded_forbidden_ipv4_addresses_are_forbidden(addr):
    assert h._forbidden_probe_address(addr) is True


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "::ffff:10.1.2.3", "2002:0a01:0203::1", "64:ff9b::a01:203", "::1"])
def test_embedded_allowed_addresses_stay_allowed(addr):
    assert h._forbidden_probe_address(addr) is False


@pytest.mark.parametrize(
    "bad",
    [
        "http://169.254.169.254/x",
        "http://[2002:a9fe:a9fe::1]:8080",
        "http://[64:ff9b::a9fe:a9fe]:8080",
        "http://[::ffff:169.254.169.254]/",
        "ftp://example.test/x",
    ],
)
def test_a_forbidden_candidate_is_never_returned_even_when_every_candidate_is_dead(monkeypatch, bad):
    dead = url(closed_port())
    wire(monkeypatch, override=bad, registry=bad, env=bad)
    assert run(h.get_rag_service_url("weather")) is None
    wire(monkeypatch, override=bad, registry=dead, env=bad)
    assert run(h.get_rag_service_url("weather")) == dead  # first allowed candidate


def test_a_forbidden_override_does_not_stop_a_working_registry(monkeypatch):
    async def main():
        server, live = await listener()
        wire(monkeypatch, override="http://169.254.169.254/", registry=url(live), env=None)
        try:
            assert await h.get_rag_service_url("weather") == url(live)
        finally:
            server.close()

    run(main())


def test_a_probe_failure_that_is_not_an_oserror_is_unreachable_and_logged_once_by_type(monkeypatch):
    async def odd(host, port):
        raise ValueError("odd")

    monkeypatch.setattr(h, "_connect_probe", odd)
    monkeypatch.setattr(h, "_probe_error_types_logged", set())
    assert run(h._probe_url("http://rag.example.test:8000")) is False
    assert run(h._probe_url("http://rag.example.test:8000")) is False
    warned = [c for c in h.logger.warning.call_args_list if c.args and c.args[0] == "rag_url_probe_failed"]
    assert len(warned) == 1 and warned[0].kwargs == {"error_type": "ValueError"}  # the type, never the text


def test_a_cancelled_probe_is_still_cancelled(monkeypatch):
    async def hang(host, port):
        await asyncio.sleep(60)

    async def main():
        monkeypatch.setattr(h, "_connect_probe", hang)
        task = asyncio.create_task(h._probe_url("http://rag.example.test:8000"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())
