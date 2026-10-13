"""The guard's door read (ATHENA-233): bounded by Home Assistant's own request
timeout, never cancelled from outside, and a read that fails or times out
refuses the opening instead of hiding a group.

The auth-budget facts are measured through the real channel and latch: a
timeout on a read is settled as ``unreachable_sent`` with no reservation held,
so the failed-sign-in budget does not move; a connect timeout is refunded.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import mode_permission as mp  # noqa: E402
from shared import ha_auth  # noqa: E402
from shared.ha_client import HomeAssistantClient  # noqa: E402

from .ha_auth_fakes import make_stack  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class StateTransport(httpx.AsyncBaseTransport):
    """Answers state reads from a map, or raises ``error`` on them."""

    def __init__(self, error=None, states=None):
        self.error = error
        self.states = states or {}
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        if request.url.path.startswith("/api/states/") and self.error is not None:
            raise self.error
        if request.url.path.startswith("/api/states/"):
            body = self.states.get(request.url.path.rsplit("/", 1)[1], {"state": "locked", "attributes": {}})
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


async def proven_stack(transport):
    stack = make_stack(gate_wait=0.1)
    await stack.channel.request("GET", "/api/states")
    stack.channel._transport._inner = transport
    observed = []
    original = stack.latch.observe

    async def spy(snapshot, admission, **kw):
        observed.append((admission.reserved, kw["result"]))
        return await original(snapshot, admission, **kw)

    stack.latch.observe = spy
    stack.budget = lambda: int(ha_auth._decode(stack.redis._hash(ha_auth.PREFIX + "budget:v2") or {}).get("n", 0))
    stack.observed = observed
    return stack


def test_get_state_hands_its_timeout_to_the_http_client():
    async def go():
        transport = StateTransport()
        stack = await proven_stack(transport)
        client = HomeAssistantClient(stack.channel)
        await client.get_state("lock.a", timeout=mp.DOOR_READ_TIMEOUT_SECONDS)
        await client.get_state("lock.b")
        await stack.channel.aclose()
        return [r.extensions["timeout"] for r in transport.requests if r.url.path.startswith("/api/states/")]

    short, default = run(go())
    assert short["read"] == short["connect"] == short["write"] == short["pool"] == mp.DOOR_READ_TIMEOUT_SECONDS
    assert default["read"] != mp.DOOR_READ_TIMEOUT_SECONDS, "other callers keep the channel default"


def test_the_door_timeout_is_not_below_the_canary_cut():
    assert mp.DOOR_READ_TIMEOUT_SECONDS >= ha_auth.CANARY_TIMEOUT, (
        "a canary read is already cut off at CANARY_TIMEOUT and stays counted; "
        "a shorter door timeout would spend a reservation the default would not"
    )
    assert mp.DOOR_READ_TIMEOUT_SECONDS < 30.0


def test_a_read_timeout_on_a_state_read_does_not_spend_the_failed_sign_in_budget():
    async def go():
        stack = await proven_stack(StateTransport(error=httpx.ReadTimeout("slow")))
        before = stack.budget()
        with pytest.raises(httpx.ReadTimeout):
            await HomeAssistantClient(stack.channel).get_state("lock.a", timeout=mp.DOOR_READ_TIMEOUT_SECONDS)
        await stack.channel.aclose()
        return stack.observed, stack.budget() - before

    observed, spent = run(go())
    assert observed == [(False, "unreachable_sent")], "settled with no reservation held"
    assert spent == 0


def test_a_connect_timeout_is_refunded():
    async def go():
        stack = await proven_stack(StateTransport(error=httpx.ConnectTimeout("c")))
        before = stack.budget()
        with pytest.raises(httpx.ConnectTimeout):
            await HomeAssistantClient(stack.channel).get_state("lock.a", timeout=mp.DOOR_READ_TIMEOUT_SECONDS)
        await stack.channel.aclose()
        return stack.observed, stack.budget() - before

    observed, spent = run(go())
    assert observed == [(False, "other")] and spent == 0


def _guarded(transport_error=None, states=None):
    async def go(call):
        transport = StateTransport(error=transport_error, states=states)
        stack = await proven_stack(transport)
        guard = mp.PermissionEnforcingHAClient(HomeAssistantClient(stack.channel))
        before = stack.budget()
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner") as scope:
            outcome = await call(guard, scope)
        await stack.channel.aclose()
        writes = [r for r in transport.requests if r.method == "POST"]
        return outcome, scope, writes, stack.budget() - before

    return go


def test_a_timed_out_door_read_refuses_the_unlock_and_spends_no_budget():
    async def call(guard, scope):
        with pytest.raises(mp.HAWritePermissionDenied) as excinfo:
            await guard.call_service("lock", "unlock", {"entity_id": "lock.all_doors"})
        return excinfo.value.reason

    reason, scope, writes, spent = run(_guarded(httpx.ReadTimeout("slow"))(call))
    assert reason == mp.DOOR_UNVERIFIED and writes == [] and spent == 0
    assert scope.opened == set() and scope.halted is True


def test_a_readable_plain_lock_unlocks_through_the_real_channel():
    async def call(guard, scope):
        await guard.call_service("lock", "unlock", {"entity_id": "lock.front"})
        return scope.opened

    opened, scope, writes, spent = run(_guarded()(call))
    assert opened == {"lock.front"} and [w.url.path for w in writes] == ["/api/services/lock/unlock"]


def test_a_group_found_by_the_real_read_is_refused():
    states = {"lock.all_doors": {"state": "locked", "attributes": {"entity_id": ["lock.a", "lock.b"]}}}

    async def call(guard, scope):
        with pytest.raises(mp.HAWritePermissionDenied) as excinfo:
            await guard.call_service("lock", "unlock", {"entity_id": "lock.all_doors"})
        return excinfo.value.reason

    reason, _, writes, _ = run(_guarded(states=states)(call))
    assert reason == mp.OPENING_NEEDS_NAMED_TARGET and writes == []
