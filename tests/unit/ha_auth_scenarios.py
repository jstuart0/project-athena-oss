"""One table of scenarios for the failed sign-in budget's Redis behaviour.

Each scenario is a function of an ``Env``: a Redis client, a clock and a way to
let time pass. ``test_ha_auth_latch.py`` runs the table against the in-memory
Redis with the Python mirror of each script; ``tests/integration/
test_ha_auth_real_redis.py`` runs the same table against a real server, with a
str client and a bytes client. That is what keeps the mirror honest.

Not a test file.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from shared import ha_auth

from .ha_auth_fakes import Clock, credentials, make_latch

BUDGET_KEY = ha_auth.PREFIX + "budget:v2"
CALLERS_KEY = ha_auth.PREFIX + "callers:v2"


def key(name: str, snapshot=None) -> str:
    return f"{ha_auth.PREFIX}{name}:{snapshot.digest}" if snapshot is not None else ha_auth.PREFIX + name


@dataclass
class Env:
    redis: Any
    clock: Clock
    pass_time: Callable[[float], Awaitable[None]]

    def latch(self, instance: str = "pod-a", **kw) -> ha_auth.HAAuthLatch:
        return make_latch(self.redis, clock=self.clock, instance=instance, **kw)

    async def budget(self) -> Dict[str, str]:
        return ha_auth._decode(await self.redis.hgetall(BUDGET_KEY))

    async def n(self) -> int:
        return int((await self.budget()).get("n", 0))

    async def r(self) -> int:
        return int((await self.budget()).get("r", 0))

    async def epoch(self) -> int:
        return int((await self.budget()).get("epoch", 0))

    async def set_budget(self, **fields) -> None:
        await self.redis.hset(BUDGET_KEY, mapping={k: str(v) for k, v in fields.items()})

    async def ttl(self, name: str) -> int:
        return await self.redis.ttl(name)

    async def exists(self, name: str) -> bool:
        return bool(await self.redis.exists(name))

    async def members(self, name: str) -> set:
        return set(ha_auth._decode(await self.redis.smembers(name)))


READ = dict(method="GET", path="/api/states", key="GET /api/states")
WRITE = dict(method="POST", path="/api/services/light/turn_on", key="POST /api/services/light/turn_on light.a")
CALLER = "orchestrator_rest"


async def settle(latch, snap, admission, result, *, call=READ):
    await latch.observe(snap, admission, caller=CALLER, key=call["key"], result=result)


async def prove_ok(latch, snap):
    """Run one admitted proof to a successful WebSocket sign-in."""
    admission = await latch.admit(snap, **READ)
    assert admission.kind == ha_auth.PROVE, admission
    await settle(latch, snap, admission, "proof_ok_ws")
    return admission


async def no_ttl_on_budget(env: Env) -> None:
    assert await env.ttl(BUDGET_KEY) in (-1, -2), "the budget hash must never expire"


# -- scenarios --------------------------------------------------------------


async def gate_contention(env: Env) -> None:
    """20 workers on two latches racing for an unproven digest: one proof."""
    snap = credentials()
    latches = [env.latch("pod-a", gate_wait=0.4), env.latch("pod-b", gate_wait=0.4)]
    results = await asyncio.gather(*[latches[i % 2].admit(snap, **READ) for i in range(20)])
    kinds = Counter(r.kind for r in results)
    assert kinds[ha_auth.PROVE] == 1 and kinds[ha_auth.GATED] == 19, kinds
    assert (await env.n(), await env.r()) == (1, 1)
    await no_ttl_on_budget(env)


async def two_digests_one_slot(env: Env) -> None:
    """At n = 2, two digests with free leases: exactly one reserves."""
    await env.set_budget(n=2, r=0)
    first, second = credentials(token="token-one"), credentials(token="token-two")
    latch = env.latch(gate_wait=0.2)
    admissions = await asyncio.gather(latch.admit(first, **READ), latch.admit(second, **READ))
    kinds = sorted(a.kind for a in admissions)
    assert kinds == [ha_auth.PROVE, ha_auth.WITHHELD], kinds
    assert next(a for a in admissions if a.kind == ha_auth.WITHHELD).reason == ha_auth.REASON_BUDGET
    assert await env.n() == 3
    winner = next(a for a in admissions if a.kind == ha_auth.PROVE)
    await settle(latch, first if admissions[0] is winner else second, winner, "unreachable_unsent")
    assert (await env.n(), await env.r()) == (2, 0), "an unsent proof gives its reservation back"
    await no_ttl_on_budget(env)


async def unreachable_sent_stays_counted(env: Env) -> None:
    snap = credentials()
    latch = env.latch()
    admission = await latch.admit(snap, **READ)
    await settle(latch, snap, admission, "unreachable_sent")
    assert (await env.n(), await env.r()) == (1, 0), "an auth frame that may have been counted stays counted"
    assert not await env.exists(key("gate:v1", snap)), "the lease is released"


async def lease_expiry(env: Env) -> None:
    """A holder that never settles: the lease expires, a second proof is
    admitted, and the dead reservation stays counted."""
    snap = credentials()
    latch = env.latch(gate_lease=1.0, gate_wait=0.2)
    first = await latch.admit(snap, **READ)
    assert first.kind == ha_auth.PROVE
    assert (await latch.admit(snap, **READ)).kind == ha_auth.GATED
    await env.pass_time(1.3)
    second = await latch.admit(snap, **READ)
    assert second.kind == ha_auth.PROVE
    assert await env.n() == 2
    await no_ttl_on_budget(env)


async def refund_never_crosses_an_epoch(env: Env) -> None:
    await env.set_budget(epoch=5)
    snap = credentials()
    latch = env.latch()
    old = await latch.admit(snap, **READ)  # reserved in epoch 5
    assert old.kind == ha_auth.PROVE and old.epoch == 5
    other = credentials(token="other-token")
    await prove_ok(latch, other)  # an auth_ok: n = r = 0, epoch 6
    assert (await env.n(), await env.epoch()) == (0, 6)
    failing = await latch.admit(other, **WRITE)  # proven now; a real failure follows
    await settle(latch, other, failing, "auth_rejected", call=WRITE)
    assert await env.n() == 1
    await settle(latch, snap, old, "passed")  # the old refund must not eat the new failure
    assert await env.n() == 1, "a refund from epoch 5 must not lower epoch 6's count"


async def auth_ok_clears_everything(env: Env) -> None:
    snap = credentials()
    latch = env.latch()
    await env.set_budget(n=2, r=1, spent_at="x")
    await env.redis.hset(CALLERS_KEY, mapping={"gateway_rest|count": "2", "gateway_rest|last_at": "t"})
    await env.redis.set(key("latch:v1", snap), "junk", ex=100)
    before = await env.epoch()
    admission = await latch.admit(snap, **READ)  # junk latch is invalid: unproven, gate
    assert admission.kind == ha_auth.PROVE
    await settle(latch, snap, admission, "proof_ok_ws")
    budget = await env.budget()
    assert (int(budget["n"]), int(budget["r"]), int(budget["epoch"])) == (0, 0, before + 1)
    assert "spent_at" not in budget
    assert not await env.exists(CALLERS_KEY) and not await env.exists(key("latch:v1", snap))
    assert await env.exists(key("proven:v2", snap))
    await no_ttl_on_budget(env)


async def rest_proof_does_not_reset_the_count(env: Env) -> None:
    """Home Assistant never resets its count on a REST success, so neither do we."""
    snap = credentials()
    latch = env.latch()
    await env.set_budget(n=2, r=0)
    admission = await latch.admit(snap, **READ)
    assert admission.kind == ha_auth.PROVE and await env.n() == 3
    await settle(latch, snap, admission, "proof_ok_rest")
    assert await env.n() == 2, "the proof's own reservation is refunded, the earlier failures stay"
    assert await env.exists(key("proven:v2", snap))


async def reopen_lowers_to_one_attempt(env: Env) -> None:
    snap = credentials()
    latch = env.latch()
    # (3, 3) is a leaked reservation count with no live reservation behind it: a save recovers.
    for before, reserved, after in ((3, 0, 2), (1, 0, 1), (3, 3, 2), (0, 0, 0)):
        await env.redis.delete(BUDGET_KEY)
        await env.set_budget(n=before, r=reserved)
        await latch.clear_and_reopen(snap)
        assert await env.n() == after, (before, reserved)
    await no_ttl_on_budget(env)


async def generation_compare_and_set(env: Env) -> None:
    snap = credentials()
    latch = env.latch(refresh_gap=0)
    await prove_ok(latch, snap)
    admitted = await latch.admit(snap, **READ)
    assert admitted.kind == ha_auth.OPEN
    killer = await latch.admit(snap, **READ)
    await settle(latch, snap, killer, "auth_rejected")  # a 401 deletes the proof
    assert not await env.exists(key("proven:v2", snap))
    await settle(latch, snap, admitted, "passed")  # a response that started earlier returns 2xx
    assert not await env.exists(key("proven:v2", snap)), "a deleted proof is never revived by an older generation"
    await latch.clear_and_reopen(snap)
    await prove_ok(latch, snap)
    current = ha_auth._decode(await env.redis.get(key("proven:v2", snap)))
    newer = json.loads(current)["gen"]
    assert newer != admitted.gen
    await settle(latch, snap, admitted, "passed")  # still the older generation
    assert json.loads(ha_auth._decode(await env.redis.get(key("proven:v2", snap))))["gen"] == newer


async def seen_and_admin_only(env: Env) -> None:
    snap = credentials()
    latch_a, latch_b = env.latch("pod-a"), env.latch("pod-b")
    await prove_ok(latch_a, snap)
    first = await latch_a.admit(snap, **WRITE)
    assert first.kind == ha_auth.CANARY and first.reserved
    await settle(latch_a, snap, first, "passed", call=WRITE)
    assert await env.members(key("seen:v1", snap)) == {"POST /api/services/light/turn_on"}
    assert (await latch_b.admit(snap, **WRITE)).kind == ha_auth.OPEN, "seen is shared across processes"
    denied = await latch_a.admit(snap, **WRITE)
    await settle(latch_a, snap, denied, "auth_rejected", call=WRITE)
    assert await env.members(key("admin_only:v1", snap)) == {WRITE["key"]}
    withheld = await latch_b.admit(snap, **WRITE)
    assert (withheld.kind, withheld.reason) == (ha_auth.WITHHELD, ha_auth.REASON_ADMIN_ONLY)
    await latch_a.clear_and_reopen(snap)
    assert not await env.exists(key("admin_only:v1", snap))


async def legacy_marker_is_adopted_once(env: Env) -> None:
    snap = credentials()
    latch = env.latch()
    legacy = latch._legacy_key(snap)
    await env.redis.set(legacy, "1", ex=3600)
    first = await latch.admit(snap, **READ)
    assert (first.kind, first.reason) == (ha_auth.WITHHELD, ha_auth.REASON_LATCHED)
    assert await env.n() == 1
    again = await latch.admit(snap, **READ)
    assert again.reason == ha_auth.REASON_LATCHED and await env.n() == 1, "adopted once"
    assert 0 < await env.ttl(legacy) <= 86400
    await latch.clear_and_reopen(snap)
    assert not await env.exists(legacy)


async def latch_trip_writes_the_legacy_marker(env: Env) -> None:
    snap = credentials()
    latch = env.latch()
    admission = await latch.admit(snap, **READ)
    await settle(latch, snap, admission, "auth_rejected")
    legacy = latch._legacy_key(snap)
    assert await env.exists(key("latch:v1", snap)) and await env.exists(legacy)
    assert 0 < await env.ttl(legacy) <= 86400
    assert await env.n() == 1 and await env.r() == 0
    counts = ha_auth._decode(await env.redis.hgetall(CALLERS_KEY))
    assert counts[f"{CALLER}|count"] == "1"
    await prove_ok_after_probe(env, latch, snap)
    assert not await env.exists(legacy) and not await env.exists(key("latch:v1", snap))


async def prove_ok_after_probe(env: Env, latch, snap) -> None:
    env.clock.advance(121)  # the wall clock only: the first probe is due at +120 s
    probe = await latch.admit(snap, **READ)
    assert probe.kind == ha_auth.PROVE and probe.probe
    await settle(latch, snap, probe, "proof_ok_ws")


async def reply_types_decode(env: Env) -> None:
    """Every script's reply reads the same through ``_decode`` for a str and a
    bytes client."""
    snap = credentials()
    latch = env.latch(gate_wait=0.2)
    reply = await latch._call("reserve_proof", [BUDGET_KEY, key("gate:v1", snap), key("latch:v1", snap), key("proven:v2", snap)], ["h", 1000, ha_auth.BUDGET, "t", "", "", 1000, 60000])
    assert reply == [1, 0]
    reply = await latch._call("reserve_request", [BUDGET_KEY], [ha_auth.REQUEST_ALLOWANCE, "rq", 1000, 60000])
    assert reply[0] in (0, 1, 2) and isinstance(reply[1], int)
    assert isinstance(await latch._call("reopen", [BUDGET_KEY, key("gate:v1", snap)], [ha_auth.BUDGET, 1000]), int)
    assert (await latch.admit(snap, **READ)).kind in (ha_auth.PROVE, ha_auth.GATED)


async def request_allowance_keeps_the_proofs_slot(env: Env) -> None:
    """Unseen routes can raise n only to REQUEST_ALLOWANCE; the proof still reserves."""
    snap = credentials()
    latch = env.latch(gate_wait=0.3)
    await prove_ok(latch, snap)
    routes = [dict(method="POST", path=f"/api/services/light/s{i}", key=f"POST /api/services/light/s{i}") for i in range(6)]
    results = await asyncio.gather(*[latch.admit(snap, **r) for r in routes])
    kinds = Counter(a.kind for a in results)
    assert kinds[ha_auth.CANARY] == ha_auth.REQUEST_ALLOWANCE, kinds
    assert await env.n() == ha_auth.REQUEST_ALLOWANCE
    for admission, call in zip(results, routes):
        if admission.kind == ha_auth.CANARY:
            await settle(latch, snap, admission, "auth_rejected", call=call)
    assert await env.n() <= ha_auth.REQUEST_ALLOWANCE
    other = credentials(token="another-token")
    assert (await latch.admit(other, **READ)).kind == ha_auth.PROVE, "the proof's slot is never taken by requests"


async def reservation_rechecks_what_the_caller_read(env: Env) -> None:
    """A latch that appeared between the read and the reservation sends the
    caller back to read again, with nothing reserved."""
    snap = credentials()
    latch = env.latch()
    gate = key("gate:v1", snap)
    arguments = ["holder", 1000, ha_auth.BUDGET, "t", "", "", 1000, 60000]
    keys = [BUDGET_KEY, gate, key("latch:v1", snap), key("proven:v2", snap)]
    assert await latch._call("reserve_proof", keys, arguments) == [1, 0]
    await env.redis.delete(gate, BUDGET_KEY)
    await env.redis.set(key("latch:v1", snap), "appeared", ex=100)
    assert await latch._call("reserve_proof", keys, arguments) == [3, 0]
    assert not await env.exists(gate) and await env.n() == 0
    await env.redis.delete(key("latch:v1", snap))
    await env.redis.set(key("proven:v2", snap), "appeared", ex=100)
    assert await latch._call("reserve_proof", keys, arguments) == [3, 0]
    assert await latch._call("reserve_proof", keys, arguments[:4] + ["", "appeared"] + arguments[6:]) == [1, 0]


async def a_proof_started_from_a_stale_read_starts_over(env: Env) -> None:
    """Another process trips the latch after this one has read the state: this
    one must not run a second proof."""
    snap = credentials()
    slow, other = env.latch("pod-a", gate_wait=0.3), env.latch("pod-b", gate_wait=0.3)
    original = slow._read
    calls = 0

    async def stale_then_fresh(*args, **kwargs):
        nonlocal calls
        state = await original(*args, **kwargs)  # the state before the other process acts
        calls += 1
        if calls == 1:
            admission = await other.admit(snap, **READ)
            assert admission.kind == ha_auth.PROVE
            await settle(other, snap, admission, "auth_rejected")
        return state

    slow._read = stale_then_fresh
    admission = await slow.admit(snap, **READ)
    assert (admission.kind, admission.reason) == (ha_auth.WITHHELD, ha_auth.REASON_LATCHED), admission
    assert await env.n() == 1, "one failed sign-in, not two"


async def a_rest_success_never_clears_the_count(env: Env) -> None:
    """Only a WebSocket sign-in resets Home Assistant's count, so only that resets ours."""
    snap = credentials()
    latch = env.latch()
    await prove_ok(latch, snap)
    await env.set_budget(n=2, r=0)
    admission = await latch.admit(snap, **READ)
    assert admission.kind == ha_auth.OPEN
    await settle(latch, snap, admission, "passed")
    assert (await env.n(), await env.epoch()) == (2, 1)


async def leaked_reservations_cannot_freeze_the_budget(env: Env) -> None:
    """Three proofs reserve and their holders die before settling: n = 3 and r = 3.
    The old reopen kept n at r, so a save could not recover. Now it does, and
    exactly one more failed sign-in follows before the next success."""
    latch = env.latch()
    leaked = [credentials(token=f"leaked-{i}") for i in range(3)]
    for snap in leaked:
        assert (await latch.admit(snap, **READ)).kind == ha_auth.PROVE
    assert (await env.n(), await env.r()) == (3, 3)
    assert (await latch.admit(credentials(token="after"), **READ)).kind == ha_auth.WITHHELD
    await latch.clear_and_reopen(leaked[0])
    assert (await env.n(), await env.r()) == (2, 0), "the save drops the leaked reservations and lowers to one attempt"
    admission = await latch.admit(leaked[0], **READ)
    assert admission.kind == ha_auth.PROVE and await env.n() == 3
    await settle(latch, leaked[0], admission, "auth_rejected")
    assert await env.n() == 3 and (await latch.admit(leaked[0], **READ)).kind == ha_auth.WITHHELD
    await no_ttl_on_budget(env)


async def a_reservation_that_is_never_settled_expires(env: Env) -> None:
    """Reservations are per holder with an expiry: a dead holder's stops counting
    in r by itself, a live one still counts."""
    latch = env.latch()
    first = await latch.admit(credentials(token="dies"), **READ)
    assert first.kind == ha_auth.PROVE and (await env.n(), await env.r()) == (1, 1)
    env.clock.advance(ha_auth.RESERVATION_TTL - 5)
    second = await latch.admit(credentials(token="lives"), **READ)
    assert second.kind == ha_auth.PROVE and await env.r() == 2, "before the expiry both still count"
    env.clock.advance(10)
    third = await latch.admit(credentials(token="third"), **READ)
    assert third.kind == ha_auth.PROVE
    assert (await env.n(), await env.r()) == (3, 2), "the first one expired; n still holds its possible failure"
    await settle(latch, credentials(token="lives"), second, "refund")
    await settle(latch, credentials(token="third"), third, "refund")
    assert await env.r() == 0 and await env.n() == 1


async def an_unsettled_route_canary_expires_too(env: Env) -> None:
    latch = env.latch()
    snap = credentials()
    await prove_ok(latch, snap)
    canary = await latch.admit(snap, **WRITE)
    assert canary.kind == ha_auth.CANARY and await env.r() == 1
    cover = dict(method="POST", path="/api/services/cover/open", key="POST /api/services/cover/open cover.a")
    second = await latch.admit(snap, **dict(cover, path="/api/services/lock/lock", key="POST /api/services/lock/lock lock.a"))
    assert second.kind == ha_auth.CANARY and await env.r() == 2, "each canary holds its own reservation while it is live"
    env.clock.advance(ha_auth.RESERVATION_TTL + 1)
    again = await latch.admit(snap, **cover)
    assert (again.kind, again.reason) == (ha_auth.WITHHELD, ha_auth.REASON_BUDGET), "two possibly-counted attempts fill the allowance"
    assert await env.r() == 0 and await env.n() == 2, "the dead canaries stopped counting as reserved but stay counted as attempts"
    await settle(latch, snap, canary, "refund")  # a late settle of an expired reservation is harmless
    assert await env.r() == 0


async def a_late_settlement_after_a_save_counts_as_unreserved(env: Env) -> None:
    """A save drops reservations and bumps the epoch. The failure of an attempt that
    was reserved before the save is then counted again: Home Assistant did count it."""
    latch = env.latch()
    late = await latch.admit(credentials(token="before-the-save"), **READ)
    assert late.kind == ha_auth.PROVE and (await env.n(), await env.r()) == (1, 1)
    await latch.clear_and_reopen(credentials(token="the-saved-one"))
    assert (await env.n(), await env.r()) == (1, 0)
    await settle(latch, credentials(token="before-the-save"), late, "auth_rejected")
    assert await env.n() == 2, "counted again, not treated as the reservation it used to be"
    await no_ttl_on_budget(env)


SCENARIOS = {
    "a_late_settlement_after_a_save_counts_as_unreserved": a_late_settlement_after_a_save_counts_as_unreserved,
    "leaked_reservations_cannot_freeze_the_budget": leaked_reservations_cannot_freeze_the_budget,
    "a_reservation_that_is_never_settled_expires": a_reservation_that_is_never_settled_expires,
    "an_unsettled_route_canary_expires_too": an_unsettled_route_canary_expires_too,
    "gate_contention": gate_contention,
    "two_digests_one_slot": two_digests_one_slot,
    "unreachable_sent_stays_counted": unreachable_sent_stays_counted,
    "lease_expiry": lease_expiry,
    "refund_never_crosses_an_epoch": refund_never_crosses_an_epoch,
    "auth_ok_clears_everything": auth_ok_clears_everything,
    "rest_proof_does_not_reset_the_count": rest_proof_does_not_reset_the_count,
    "reopen_lowers_to_one_attempt": reopen_lowers_to_one_attempt,
    "generation_compare_and_set": generation_compare_and_set,
    "seen_and_admin_only": seen_and_admin_only,
    "legacy_marker_is_adopted_once": legacy_marker_is_adopted_once,
    "latch_trip_writes_the_legacy_marker": latch_trip_writes_the_legacy_marker,
    "reply_types_decode": reply_types_decode,
    "request_allowance_keeps_the_proofs_slot": request_allowance_keeps_the_proofs_slot,
    "a_rest_success_never_clears_the_count": a_rest_success_never_clears_the_count,
    "reservation_rechecks_what_the_caller_read": reservation_rechecks_what_the_caller_read,
    "a_proof_started_from_a_stale_read_starts_over": a_proof_started_from_a_stale_read_starts_over,
}
