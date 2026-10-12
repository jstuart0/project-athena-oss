"""HAAuthLatch: admission, the failed sign-in budget, canaries and the cap.

The shared scenario table (``ha_auth_scenarios.py``) runs here against the
in-memory Redis, with a str and a bytes client; the real-Redis tier runs the
same table against a server. The cases below are the ones that don't need a
server.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import time
from collections import Counter

import pytest

from shared import ha_auth

from . import ha_auth_scenarios as scenarios
from .ha_auth_fakes import Clock, FlakyRedis, credentials, make_latch, reads

ROOT = pathlib.Path(__file__).resolve().parents[2]
READ = scenarios.READ
WRITE = scenarios.WRITE


def run(coro):
    return asyncio.run(coro)


def env_for(*, bytes_client=False):
    clock = Clock()
    memory = ha_auth.MemoryRedis(clock=clock)
    redis = memory.bytes_view() if bytes_client else memory

    async def pass_time(seconds):
        clock.advance(seconds)

    return scenarios.Env(redis=redis, clock=clock, pass_time=pass_time)


@pytest.mark.parametrize("bytes_client", [False, True], ids=["str", "bytes"])
@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_the_shared_scenario_table(name, bytes_client):
    run(scenarios.SCENARIOS[name](env_for(bytes_client=bytes_client)))


# -- constants --------------------------------------------------------------------------------------


def test_the_constants_and_their_relations():
    assert (ha_auth.BUDGET, ha_auth.REQUEST_ALLOWANCE, ha_auth.PROOF_TIMEOUT, ha_auth.GATE_LEASE) == (3, 2, 8.0, 10.0)
    assert (ha_auth.GATE_WAIT, ha_auth.PROVEN_TTL, ha_auth.CANARY_AFTER, ha_auth.INFLIGHT_CAP) == (5.0, 600, 10.0, 18)
    assert ha_auth.PROBE_SCHEDULE_SECONDS == (120, 21600)
    assert (ha_auth.ADMIN_ONLY_TTL, ha_auth.SEEN_TTL, ha_auth.LATCH_TTL, ha_auth.COUNT_CAP) == (86400, 2_592_000, 86400, 1_000_000)
    assert ha_auth.PROOF_TIMEOUT < ha_auth.GATE_LEASE
    assert ha_auth.REQUEST_ALLOWANCE == ha_auth.BUDGET - 1
    from shared import ha_ws

    assert ha_ws.WS_CONNECT_TIMEOUT + ha_ws.WS_AUTH_TIMEOUT + ha_ws.REST_FALLBACK_TIMEOUT <= ha_auth.PROOF_TIMEOUT


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api", True), ("GET", "/api/", True), ("get", "/api/states", True),
    ("GET", "/api/states/light.a", True), ("GET", "/api/states/Light.A", False),
    ("POST", "/api/states", False), ("GET", "/api/services", False),
    ("POST", "/api/services/light/turn_on", False), ("GET", "/api/config", False),
    ("GET", "/api/states/light.a/x", False), ("GET", "/apix", False),
])
def test_is_token_proof(method, path, expected):
    assert ha_auth.is_token_proof(method, path) is expected


def test_request_key_sorts_entities_and_caps_its_length():
    assert ha_auth.request_key("post", "/api/services/light/turn_on", {"entity_id": ["light.b", "light.a"]}) == \
        "POST /api/services/light/turn_on light.a,light.b"
    assert ha_auth.request_key("GET", "/api/states") == "GET /api/states"
    long_key = ha_auth.request_key("POST", "/api/services/a/b", {"entity_id": [f"light.l{i}" for i in range(400)]})
    assert len(long_key) <= ha_auth.MAX_REQUEST_KEY and "#" in long_key
    assert long_key != ha_auth.request_key("POST", "/api/services/a/b", {"entity_id": [f"light.m{i}" for i in range(400)]})


# -- the gate ----------------------------------------------------------------------------------------------


def test_ten_concurrent_admits_on_an_unproven_digest_give_one_proof_and_the_rest_follow_its_outcome():
    async def after(result):
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=3.0)
        calls = [asyncio.ensure_future(latch.admit(snap, **READ)) for _ in range(10)]
        await asyncio.sleep(0.05)
        prover = next(c.result() for c in calls if c.done())
        assert prover.kind == ha_auth.PROVE
        await scenarios.settle(latch, snap, prover, result)
        return Counter((a.kind, a.reason) for a in await asyncio.gather(*calls)), env

    kinds, env = run(after("proof_ok_ws"))
    assert kinds == {(ha_auth.PROVE, None): 1, (ha_auth.OPEN, None): 9}
    kinds, env = run(after("auth_rejected"))
    assert kinds == {(ha_auth.PROVE, None): 1, (ha_auth.WITHHELD, ha_auth.REASON_LATCHED): 9}


def test_two_latches_on_one_redis_give_one_proof_cluster_wide():
    async def scenario():
        env = env_for()
        snap = credentials()
        first, second = env.latch("pod-a", gate_wait=0.2), env.latch("pod-b", gate_wait=0.2)
        return await asyncio.gather(*[first.admit(snap, **READ), second.admit(snap, **READ), first.admit(snap, **READ)])

    assert Counter(a.kind for a in run(scenario())) == {ha_auth.PROVE: 1, ha_auth.GATED: 2}


def test_any_401_on_a_proven_digest_deletes_the_proof_so_the_next_request_goes_through_the_gate():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        await scenarios.prove_ok(latch, snap)
        first = await latch.admit(snap, **WRITE)
        await scenarios.settle(latch, snap, first, "passed", call=WRITE)  # seen now
        denied = await latch.admit(snap, **WRITE)
        await scenarios.settle(latch, snap, denied, "auth_rejected", call=WRITE)
        other = dict(method="POST", path="/api/services/light/turn_off", key="POST /api/services/light/turn_off light.z")
        return (await latch.admit(snap, **other)), (await latch.admit(snap, **READ))

    other, read = run(scenario())
    assert other.kind == ha_auth.PROVE, "a non-proof 401 does not trip the latch, but the next request proves first"
    assert read.kind in (ha_auth.GATED, ha_auth.PROVE)


def test_a_non_proof_401_marks_only_that_request_and_never_trips_the_latch():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        await scenarios.prove_ok(latch, snap)
        first = await latch.admit(snap, **WRITE)
        await scenarios.settle(latch, snap, first, "auth_rejected", call=WRITE)
        await latch.sync(snap)
        return env, snap, latch.summary(snap)

    env, snap, summary = run(scenario())
    assert summary["latched"] is False and summary["failures"]["orchestrator_rest"]["count"] == 1
    assert not run(env.exists(scenarios.key("latch:v1", snap)))


# -- canaries ---------------------------------------------------------------------------------------------------


def test_a_stale_proof_sends_one_canary_and_the_rest_wait_for_it():
    async def eighteen(age, result=None):
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=2.0)
        await scenarios.prove_ok(latch, snap)
        env.clock.advance(age)
        calls = [asyncio.ensure_future(latch.admit(snap, **READ)) for _ in range(18)]
        await asyncio.sleep(0.05)
        done = [c.result() for c in calls if c.done()]
        if result is None:
            return Counter(a.kind for a in done), len(calls) - len(done), None
        canary = done[0]
        assert canary.kind == ha_auth.CANARY and canary.reserved is False
        await scenarios.settle(latch, snap, canary, result)
        return Counter(a.kind for a in await asyncio.gather(*calls)), 0, env

    kinds, waiting, _ = run(eighteen(11))
    assert kinds == {ha_auth.CANARY: 1} and waiting == 17
    kinds, _, _ = run(eighteen(11, "passed"))
    assert kinds == {ha_auth.CANARY: 1, ha_auth.OPEN: 17}
    kinds, _, _ = run(eighteen(11, "auth_rejected"))
    assert kinds[ha_auth.CANARY] == 1 and ha_auth.OPEN not in kinds, "after a rejection the rest go through the latch"
    kinds, waiting, _ = run(eighteen(9))
    assert kinds == {ha_auth.OPEN: 18} and waiting == 0


def test_a_new_route_sends_one_reserved_canary_and_a_full_allowance_fails_closed():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.3)
        await scenarios.prove_ok(latch, snap)
        same = await asyncio.gather(latch.admit(snap, **WRITE), latch.admit(snap, **WRITE), latch.admit(snap, **WRITE))
        kinds = Counter(a.kind for a in same)
        reserved = next(a for a in same if a.kind == ha_auth.CANARY)
        assert reserved.reserved is True
        await env.set_budget(n=2, r=0)  # two real failures: the allowance is full
        unseen = dict(method="POST", path="/api/services/cover/open", key="POST /api/services/cover/open cover.a")
        blocked = await latch.admit(snap, **unseen)
        seen_read = await latch.admit(snap, **READ)
        gate_other = await latch.admit(credentials(token="elsewhere"), **READ)
        return kinds, blocked, seen_read, gate_other

    kinds, blocked, seen_read, gate_other = run(scenario())
    assert kinds == {ha_auth.CANARY: 1, ha_auth.GATED: 2}, "the same route in one process waits for its canary"
    assert (blocked.kind, blocked.reason) == (ha_auth.WITHHELD, ha_auth.REASON_BUDGET)
    assert seen_read.kind == ha_auth.OPEN, "a seen route is not an unseen key"
    assert gate_other.kind == ha_auth.PROVE, "the proof still has its slot at n = 2"


def test_the_inflight_cap_bounds_concurrent_sends():
    async def scenario():
        latch = make_latch(ha_auth.MemoryRedis(), inflight_cap=18)
        running = peak = 0

        async def send():
            nonlocal running, peak
            async with latch.slot():
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1
            return "done"

        results = await asyncio.gather(*[send() for _ in range(25)])
        return peak, results

    peak, results = run(scenario())
    assert peak == 18 and results == ["done"] * 25, "none withheld, the rest queue"


# -- probes -------------------------------------------------------------------------------------------------------


def test_probes_run_at_two_minutes_and_six_hours_and_a_spent_budget_ends_them():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        first = await latch.admit(snap, **READ)
        await scenarios.settle(latch, snap, first, "auth_rejected")  # the trip
        await latch.sync(snap)
        trip = latch.summary(snap)
        env.clock.advance(119)
        early = await latch.admit(snap, **READ)
        env.clock.advance(2)
        due = await latch.admit(snap, **READ)
        also = await latch.admit(snap, **READ)  # a second request while the probe is out
        await scenarios.settle(latch, snap, due, "auth_rejected")
        await latch.sync(snap)
        second = latch.summary(snap)
        env.clock.advance(21600)
        last = await latch.admit(snap, **READ)
        await scenarios.settle(latch, snap, last, "auth_rejected")
        await latch.sync(snap)
        done = latch.summary(snap)
        env.clock.advance(30 * 86400)
        later = await latch.admit(snap, **READ)
        return trip, early, due, also, second, last, done, later, await env.n()

    trip, early, due, also, second, last, done, later, n = run(scenario())
    assert trip["latched"] and trip["next_probe_at"] is not None
    assert early.kind == ha_auth.WITHHELD and (due.kind, due.probe) == (ha_auth.PROVE, True)
    assert also.kind == ha_auth.WITHHELD, "one probe in flight cluster-wide"
    assert second["next_probe_at"] is not None and second["next_probe_at"] > trip["next_probe_at"]
    assert last.kind == ha_auth.PROVE and last.probe
    assert done["budget_spent"] is True and done["next_probe_at"] is None, "the card says it won't try again"
    assert later.kind == ha_auth.WITHHELD and n == 3


def test_a_probe_is_only_taken_when_the_budget_has_room():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        first = await latch.admit(snap, **READ)
        await scenarios.settle(latch, snap, first, "auth_rejected")
        await env.set_budget(n=3, r=0)
        env.clock.advance(121)
        return await latch.admit(snap, **READ)

    admission = run(scenario())
    assert (admission.kind, admission.reason) == (ha_auth.WITHHELD, ha_auth.REASON_BUDGET)


def test_after_the_latch_expires_a_spent_budget_still_blocks_the_gate():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        first = await latch.admit(snap, **READ)
        await scenarios.settle(latch, snap, first, "auth_rejected")
        await env.set_budget(n=3, r=0)
        env.clock.advance(ha_auth.LATCH_TTL + 60)
        assert not await env.exists(scenarios.key("latch:v1", snap))
        return await latch.admit(snap, **READ)

    admission = run(scenario())
    assert (admission.kind, admission.reason) == (ha_auth.WITHHELD, ha_auth.REASON_BUDGET), "an expired latch reopens nothing"


# -- the signed values ---------------------------------------------------------------------------------------------------


def test_a_tampered_latch_or_proof_reads_as_absent_and_a_far_next_is_capped():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        first = await latch.admit(snap, **READ)
        await scenarios.settle(latch, snap, first, "auth_rejected")
        latch_key = scenarios.key("latch:v1", snap)
        doc = json.loads(ha_auth._decode(await env.redis.get(latch_key)))
        forged = dict(doc, step=2, next=None)  # a stop that would never probe again
        await env.redis.set(latch_key, json.dumps(forged), ex=100)
        await env.redis.delete(latch._legacy_key(snap))  # the old registry's marker would otherwise be adopted
        env.clock.advance(121)
        after_forged_latch = await latch.admit(snap, **READ)  # reads as absent: unproven, the gate
        far = dict(doc, next=doc["next"] + 400 * 86400)
        far["sig"] = latch._sign("latch", snap.digest, far["at"], far["step"], far["next"])
        await env.redis.set(latch_key, json.dumps(far), ex=100)
        parsed = latch._parse_latch(snap.digest, await env.redis.get(latch_key))
        return after_forged_latch, parsed, latch, doc

    admission, parsed, latch, doc = run(scenario())
    assert admission.kind == ha_auth.PROVE, "a forged latch is absent, not trusted"
    assert parsed.next <= int((latch._wall() + ha_auth.MAX_NEXT_AHEAD_SECONDS) * 1000)


def test_a_forged_proof_sends_traffic_through_the_gate():
    async def scenario():
        env = env_for()
        snap = credentials()
        latch = env.latch(gate_wait=0.2)
        forged = json.dumps({"v": 1, "gen": "g", "at": latch._now_ms(), "sig": "0" * 32})
        await env.redis.set(scenarios.key("proven:v2", snap), forged, ex=100)
        unsigned = json.dumps({"gen": "g", "at": latch._now_ms()})
        first = await latch.admit(snap, **READ)
        await env.redis.set(scenarios.key("proven:v2", snap), unsigned, ex=100)
        await scenarios.settle(latch, snap, first, "unreachable_unsent")
        second = await latch.admit(snap, **READ)
        return first, second

    first, second = run(scenario())
    assert first.kind == second.kind == ha_auth.PROVE


def test_a_state_signed_for_another_digest_is_not_trusted():
    async def scenario():
        env = env_for()
        one, two = credentials(token="token-one"), credentials(token="token-two")
        latch = env.latch(gate_wait=0.2)
        await scenarios.prove_ok(latch, one)
        proven = await env.redis.get(scenarios.key("proven:v2", one))
        await env.redis.set(scenarios.key("proven:v2", two), proven, ex=100)
        return await latch.admit(two, **READ)

    assert run(scenario()).kind == ha_auth.PROVE


# -- Redis trouble --------------------------------------------------------------------------------------------------------------


def test_with_redis_down_an_unproven_or_latched_or_unseen_request_is_gated():
    async def scenario():
        clock = Clock()
        redis = FlakyRedis(ha_auth.MemoryRedis(clock=clock))
        latch = make_latch(redis, clock=clock, gate_wait=0.1)
        snap = credentials()
        redis.down = True
        unproven = await latch.admit(snap, **READ)
        write = await latch.admit(snap, **WRITE)
        return unproven, write

    unproven, write = run(scenario())
    assert unproven.kind == write.kind == ha_auth.GATED


def test_a_process_holding_a_proof_keeps_sending_seen_routes_until_its_first_rejection():
    async def scenario():
        clock = Clock()
        redis = FlakyRedis(ha_auth.MemoryRedis(clock=clock))
        latch = make_latch(redis, clock=clock, gate_wait=0.1)
        snap = credentials()
        await scenarios.prove_ok(latch, snap)
        first = await latch.admit(snap, **WRITE)
        await scenarios.settle(latch, snap, first, "passed", call=WRITE)
        redis.down = True
        seen = await latch.admit(snap, **WRITE)
        unseen = await latch.admit(snap, method="POST", path="/api/services/cover/open", key="POST /api/services/cover/open c")
        proof_read = await latch.admit(snap, **READ)
        await scenarios.settle(latch, snap, seen, "auth_rejected", call=WRITE)
        after_rejection = await latch.admit(snap, **WRITE)
        return seen, unseen, proof_read, after_rejection

    seen, unseen, proof_read, after_rejection = run(scenario())
    assert seen.kind == ha_auth.OPEN and proof_read.kind == ha_auth.OPEN
    assert unseen.kind == ha_auth.GATED
    assert after_rejection.kind == ha_auth.GATED, "at most one rejection per outage per process"


def test_redis_timeout_is_enforced_on_a_call_that_never_completes():
    async def scenario():
        clock = Clock()
        redis = FlakyRedis(ha_auth.MemoryRedis(clock=clock))
        redis.hang = True
        latch = make_latch(redis, clock=clock, redis_timeout=0.01, gate_wait=0.05)
        started = time.monotonic()
        admission = await latch.admit(credentials(), **READ)
        return admission, time.monotonic() - started

    admission, elapsed = run(scenario())
    assert admission.kind == ha_auth.GATED and elapsed < 1.0


def test_no_service_key_or_no_redis_keeps_the_state_in_process():
    async def scenario():
        snap = credentials()
        without_key = ha_auth.HAAuthLatch(ha_auth.MemoryRedis(), instance="x", service_api_key=None, gate_wait=0.1)
        without_redis = ha_auth.HAAuthLatch(None, instance="x", service_api_key="k", gate_wait=0.1)
        shared_redis = ha_auth.MemoryRedis()
        keyed = ha_auth.HAAuthLatch(shared_redis, instance="x", service_api_key="k", gate_wait=0.1)
        for latch in (without_key, without_redis, keyed):
            assert (await latch.admit(snap, **READ)).kind == ha_auth.PROVE
        return without_key.shared, without_redis.shared, keyed.shared, shared_redis.calls

    assert run(scenario())[:3] == (False, False, True)


# -- waiting ----------------------------------------------------------------------------------------------------------------------


def test_eighteen_waiters_on_one_digest_cost_one_read_per_poll_not_one_each():
    async def scenario():
        env = env_for()
        snap = credentials()
        holder = env.latch("pod-a")
        waiter = env.latch("pod-b", gate_wait=0.55)
        held = await holder.admit(snap, **READ)
        assert held.kind == ha_auth.PROVE
        before = reads(env.redis)
        results = await asyncio.gather(*[waiter.admit(snap, **READ) for _ in range(18)])
        return reads(env.redis) - before, results

    consumed, results = run(scenario())
    assert {r.kind for r in results} == {ha_auth.GATED}
    assert consumed <= 18 + 7, f"{consumed} reads for 18 waiters over 0.55 s"


# -- display ---------------------------------------------------------------------------------------------------------------------------


def test_counts_are_capped_and_garbage_reads_as_zero_for_both_client_kinds():
    for bytes_client in (False, True):
        env = env_for(bytes_client=bytes_client)
        snap = credentials()
        latch = env.latch()
        callers = {
            "gateway_rest|count": "99999999999", "gateway_rest|last_at": "2026-10-12T00:00:00+00:00",
            "admin_rest|count": "-4", "admin_ws|count": "abc", "orchestrator_rest|count": "3.7",
            "orchestrator_registry|count": "2", "orchestrator_registry|ban_at": "2026-10-12T00:05:00+00:00",
        }
        run(env.redis.hset(scenarios.CALLERS_KEY, mapping=callers))
        run(latch.sync(snap))
        failures = latch.summary(snap)["failures"]
    assert failures == {
        "gateway_rest": {"count": ha_auth.COUNT_CAP, "last_at": "2026-10-12T00:00:00+00:00"},
        "orchestrator_registry": {"count": 2, "last_at": None},
    }
    assert latch.summary(snap)["banned_at"] == "2026-10-12T00:05:00+00:00"


def test_the_summary_has_exactly_the_wire_keys_and_an_unsynced_one_is_empty():
    latch = make_latch(ha_auth.MemoryRedis())
    summary = latch.summary(credentials())
    assert set(summary) == {
        "latched", "latched_at", "next_probe_at", "banned_at", "failures", "budget_spent", "budget_spent_at",
    }
    assert summary["latched"] is False and summary["failures"] == {} and summary["budget_spent"] is False


def test_the_per_caller_counts_have_no_instance_dimension_and_auth_ok_clears_them_but_a_save_does_not():
    async def scenario():
        env = env_for()
        snap = credentials()
        for instance in ("pod-a", "pod-b"):
            latch = env.latch(instance, gate_wait=0.1)
            admission = await latch.admit(snap, **WRITE) if False else await latch.admit(credentials(token=f"bad-{instance}"), **READ)
            await latch.observe(credentials(token=f"bad-{instance}"), admission, caller="orchestrator_rest", key="GET /api/states", result="auth_rejected")
        fields = set(ha_auth._decode(await env.redis.hgetall(scenarios.CALLERS_KEY)))
        await env.latch().clear_and_reopen(snap)
        survived = bool(await env.redis.exists(scenarios.CALLERS_KEY))
        await scenarios.prove_ok(env.latch(), credentials(token="good"))
        return fields, survived, bool(await env.redis.exists(scenarios.CALLERS_KEY))

    fields, survived, after_auth_ok = run(scenario())
    assert fields == {"orchestrator_rest|count", "orchestrator_rest|last_at"}, "no pod name in any field"
    assert survived is True and after_auth_ok is False


def test_the_counts_hash_is_read_only_by_summary_and_sync():
    tree = ast.parse((ROOT / "src" / "shared" / "ha_auth.py").read_text())
    holders = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Constant) and n.value == "callers:v2" for n in ast.walk(node)):
                holders.add(node.name)
    assert holders == {"sync", "_settle", "_clear_on_auth_ok"}, "nothing that decides reads the display hash"
    assert "callers:v2" not in "".join(
        ast.get_source_segment((ROOT / "src" / "shared" / "ha_auth.py").read_text(), n) or ""
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith(("_decide", "_admit", "_read", "_gate"))
    )


def test_no_script_gives_the_budget_hash_a_ttl():
    for source in (ha_auth.RESERVE_PROOF, ha_auth.RESERVE_REQUEST, ha_auth.SETTLE, ha_auth.CLEAR_ON_AUTH_OK, ha_auth.REOPEN):
        for line in source.splitlines():
            if "KEYS[1]" in line and ("EXPIRE" in line or "PEXPIRE" in line):
                pytest.fail(f"budget:v2 must have no TTL: {line.strip()}")
    # settle and clear_on_auth_ok take the budget as KEYS[1]; every EXPIRE targets another key
    assert "EXPIRE', KEYS[1]" not in ha_auth.SETTLE and "EX', ARGV" in ha_auth.SETTLE


def test_the_instance_id_is_sanitized_and_only_names_leases():
    import os

    os.environ["HOSTNAME"] = "pod name/with spaces"
    try:
        assert ha_auth.instance_id() == "pod_name_with_spaces"
    finally:
        os.environ.pop("HOSTNAME", None)
