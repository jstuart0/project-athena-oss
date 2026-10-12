"""How many failed sign-ins Athena can cause at Home Assistant, measured.

A fake Home Assistant implements the parts of ``http/ban.py`` and
``websocket_api/auth.py`` the budget depends on: it counts every REST 401 and
every WebSocket ``auth_invalid`` per source address with no decay, resets an
address only on a WebSocket ``auth_ok``, and bans at a threshold. The stack is
real: ``HAAuthLatch`` per process on one shared Redis, ``HAChannel`` per
caller, and ``ha_ws_session`` for the registry and the pipelines.

Case letters are the rows of the plan's worst-case table. Unless a case says
otherwise every caller shares one source address (the stated model) and the
fake's ban threshold is 5, which no case other than [i]'s second run and [g2]
may reach.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from .ha_auth_fakes import (
    Clock, FakeHA, FlakyRedis, TOKEN, URL, credentials, make_latch, make_stack,
)

try:
    from shared import ha_auth, ha_ws
except ImportError:  # the base tree
    ha_auth = ha_ws = None

MINUTE, HOUR, DAY = 60, 3600, 86400
ROUTE = "/api/services/light/turn_on"
ENTITIES = [f"light.l{i}" for i in range(40)]


def run(coro):
    return asyncio.run(coro)


# -- the base-client factory: the same case [a] against the old clients -----------------------------


class _OldClient:
    def __init__(self, fake, token=TOKEN):
        from shared.ha_client import HomeAssistantClient

        self._client = HomeAssistantClient(URL, token)
        self._client.client = httpx.AsyncClient(base_url=URL, headers=self._client.headers, transport=fake.rest_transport())

    async def get(self):
        try:
            await self._client.get_states()
            return 200
        except httpx.HTTPStatusError as exc:
            return exc.response.status_code

    async def close(self):
        await self._client.close()


class _Caller:
    def __init__(self, stack):
        self.stack = stack

    async def get(self):
        return (await self.stack.channel.request("GET", "/api/states")).status_code

    async def close(self):
        await self.stack.channel.aclose()


class World:
    """orchestrator x2 replicas, the gateway and admin-backend (bytes Redis
    client) over one shared Redis, one clock and one fake Home Assistant."""

    CALLERS = (
        ("pod-a", "orchestrator_rest"), ("pod-b", "orchestrator_rest"),
        ("gateway-1", "gateway_rest"), ("admin-1", "admin_rest"),
    )

    def __init__(self, fake=None, *, token=TOKEN, address=lambda caller: "A", **latch_kw):
        self.clock = Clock()
        self.fake = fake or FakeHA()
        self.redis = ha_auth.MemoryRedis(clock=self.clock)
        self.stacks = {}
        for instance, caller in self.CALLERS:
            redis = self.redis.bytes_view() if instance.startswith("admin") else self.redis
            self.stacks[instance] = make_stack(
                fake=self.fake, token=token, redis=redis, clock=self.clock, instance=instance, caller=caller,
                address=address(instance), **latch_kw,
            )

    def stack(self, instance="pod-a"):
        return self.stacks[instance]

    async def request(self, instance, method, path, **kw):
        return await self.stacks[instance].channel.request(method, path, **kw)

    async def everyone_gets(self, n=1):
        calls = [self.request(instance, "GET", "/api/states") for instance in self.stacks for _ in range(n)]
        return await asyncio.gather(*calls)

    async def session(self, instance, profile=None):
        stack = self.stacks[instance]
        profile = profile or ha_ws.REGISTRY_READ
        caller = {"pod-a": "orchestrator_registry", "pod-b": "orchestrator_registry", "gateway-1": "gateway_registry"}.get(instance, "admin_ws")
        return ha_ws.ha_ws_session(stack.creds, stack.latch, caller=caller, profile=profile, connect=self.fake.ws_connect(stack.address))

    async def close(self):
        for stack in self.stacks.values():
            await stack.channel.aclose()

    def total(self):
        return self.fake.failed_total


# -- [a] a burst on a fresh bad token ----------------------------------------------------------------


def test_a_twenty_requests_across_every_caller_on_a_bad_token_cost_one_failed_sign_in():
    async def scenario():
        fake = FakeHA(valid=())
        if ha_auth is None:
            callers = [_OldClient(fake) for _ in range(4)]
        else:
            world = World(fake, gate_wait=1.0)
            callers = [_Caller(s) for s in world.stacks.values()]
        statuses = await asyncio.gather(*[callers[i % 4].get() for i in range(20)])
        first = fake.failed_total
        statuses += await asyncio.gather(*[callers[i % 4].get() for i in range(20)])
        for caller in callers:
            await caller.close()
        return fake, first, statuses

    fake, first, statuses = run(scenario())
    assert fake.failed_total == 1, f"{fake.failed_total} failed sign-ins reached Home Assistant (banned: {sorted(fake.banned)})"
    assert first == 1 and fake.banned == set()
    assert set(statuses) <= {401, 503}


# -- [b] REST then WebSocket, and the reverse -----------------------------------------------------------


def test_b_a_rest_rejection_then_a_websocket_attempt_and_the_reverse_cost_one():
    async def rest_then_ws():
        world = World(FakeHA(valid=()), gate_wait=0.3)
        await world.request("pod-a", "GET", "/api/states")
        with pytest.raises(ha_ws.HAWithheld):
            async with await world.session("pod-b"):
                pass
        total = world.total()
        await world.close()
        return total

    async def ws_then_rest():
        world = World(FakeHA(valid=()), gate_wait=0.3)
        with pytest.raises(ha_ws.AuthInvalid):
            async with await world.session("gateway-1"):
                pass
        response = await world.request("pod-a", "GET", "/api/states")
        total = world.total()
        await world.close()
        return total, response

    assert run(rest_then_ws()) == 1
    total, response = run(ws_then_rest())
    assert total == 1 and response.status_code == 401 and response.extensions["athena_ha_withheld"] == "latched"


# -- [c] the full clock ------------------------------------------------------------------------------


def _drive_minutes(world, minutes, *, instance="pod-a", path="/api/states", method="GET"):
    async def go():
        for _ in range(minutes):
            await world.request(instance, method, path)
            world.clock.advance(MINUTE)
    return go()


def test_c_a_token_that_never_worked_costs_three_over_thirty_days_then_nothing():
    async def scenario():
        world = World(FakeHA(valid=()), gate_wait=0.2)
        sends_after = []
        for day in range(30):
            await _drive_minutes(world, 24 * 60)
            sends_after.append((world.total(), len(world.fake.ws_connects), len(world.fake.rest_sent)))
        await world.close()
        return world, sends_after

    started = time.monotonic()
    world, history = run(scenario())
    assert world.total() == 3 and world.fake.banned == set()
    assert world.fake.rest_sent == [], "REST never sends for a rejected token"
    assert history[0][0] == 3, "all three happen inside the first day: +0, +2 min, +6 h"
    assert history[-1] == history[0], "and none after, across the 24 h latch expiry and the 7 d mark"
    assert time.monotonic() - started < 60


def test_c_prime_a_spent_budget_stays_spent_through_eight_idle_days():
    async def scenario():
        world = World(FakeHA(valid=()), gate_wait=0.2)
        for offset in (0, 121, 21601 - 121):
            await world.request("pod-a", "GET", "/api/states")
            world.clock.advance(121 if offset == 0 else 21601 - 121 if offset == 121 else 0)
        assert world.total() == 3
        world.clock.advance(8 * DAY)
        for _ in range(100):
            await world.request("pod-a", "GET", "/api/states")
            await world.request("gateway-1", "POST", ROUTE, json={"entity_id": "light.a"})
        await world.close()
        return world

    world = run(scenario())
    assert world.total() == 3
    assert len(world.fake.ws_connects) == 3 and world.fake.rest_sent == []


# -- [d] a second bad token saved after the budget was spent --------------------------------------------


def test_d_a_second_bad_token_after_the_budget_was_spent_costs_one_and_then_nothing():
    async def scenario():
        world = World(FakeHA(valid=()), gate_wait=0.2)
        await _drive_minutes(world, 7 * 60)
        assert world.total() == 3
        second = credentials("another-bad-token")
        stack = make_stack(fake=world.fake, token="another-bad-token", redis=world.redis, clock=world.clock, instance="pod-a", gate_wait=0.2)
        await stack.latch.clear_and_reopen(stack.creds)  # the operator saves the key
        for _ in range(50):
            await stack.channel.request("GET", "/api/states")
            world.clock.advance(MINUTE)
        await world.close()
        await stack.channel.aclose()
        return world

    world = run(scenario())
    assert world.total() == 4, "one more per save, never three"
    assert world.fake.banned == set()


# -- [e] a valid non-admin token and many admin-only services --------------------------------------------


def _admin_only_world(n=10, **kw):
    paths = [f"/api/services/hassio/s{i}" for i in range(n)]
    fake = FakeHA(non_admin=(TOKEN,), admin_only_paths=paths)
    return paths, World(fake, **kw)


def test_e_ten_admin_only_services_in_sequence_never_push_the_address_past_one():
    async def scenario():
        paths, world = _admin_only_world(gate_wait=0.5)
        statuses = [(await world.request("pod-a", "POST", p, json={"entity_id": "light.a"})).status_code for p in paths]
        await world.close()
        return world, statuses

    world, statuses = run(scenario())
    assert statuses == [401] * 10
    assert world.fake.max_count <= 1, f"the address reached {world.fake.max_count}"
    assert world.fake.banned == set()


def test_e_prime_the_same_ten_at_once_never_push_it_past_two_and_each_gets_its_answer():
    async def scenario():
        paths, world = _admin_only_world(gate_wait=3.0)
        responses = await asyncio.gather(*[
            world.request("pod-a", "POST", p, json={"entity_id": "light.a"}) for p in paths
        ])
        await world.close()
        return world, responses

    world, responses = run(scenario())
    assert world.fake.max_count <= ha_auth.REQUEST_ALLOWANCE, f"the address reached {world.fake.max_count}"
    assert [r.status_code for r in responses] == [401] * 10
    assert all("athena_ha_withheld" not in r.extensions for r in responses), "every service eventually got Home Assistant's own answer"


# -- [f] a proven token is revoked ---------------------------------------------------------------------------


async def _proven_world(*, delay=0.0, threshold=5, **kw):
    fake = FakeHA(delay=delay, threshold=threshold)
    world = World(fake, **kw)
    for entity in ("light.warmup", "light.warmup2"):
        response = await world.request("pod-a", "POST", ROUTE, json={"entity_id": entity})
        assert response.status_code == 200
    return world


def test_f_a_revoked_token_found_by_sequential_traffic_costs_three_in_all():
    async def scenario():
        world = await _proven_world(gate_wait=0.3)
        world.clock.advance(5)
        world.fake.revoke(TOKEN)
        seen = []
        for i in range(2 * 8):
            response = await world.request("pod-a", "POST", ROUTE, json={"entity_id": f"light.n{i}"})
            seen.append(response.status_code)
            world.clock.advance(0 if i < 3 else (121 if i == 3 else 21601 if i == 4 else 3600))
        await world.close()
        return world

    world = run(scenario())
    assert world.total() == 3 and world.fake.banned == set()


# -- [g] a ban is not a failed sign-in -------------------------------------------------------------------------


def test_g_a_403_and_a_banned_handshake_neither_trip_the_latch_nor_count():
    async def scenario():
        world = await _proven_world(gate_wait=0.3)
        world.fake.banned.add("A")
        response = await world.request("pod-a", "GET", "/api/states")
        with pytest.raises(ha_ws.HABanned):
            async with await world.session("gateway-1"):
                pass
        stack = world.stack("pod-a")
        await stack.latch.sync(stack.creds)
        return world, response, stack.latch.summary(stack.creds)

    world, response, summary = run(scenario())
    assert response.status_code == 403
    assert summary["latched"] is False and summary["failures"] == {} and summary["budget_spent"] is False
    assert summary["banned_at"] is not None
    assert world.total() == 0


# -- [g2] several egress addresses (the stated exception, measured) ---------------------------------------------


def test_g2_an_address_that_never_ends_an_episode_itself_carries_three_per_episode():
    async def scenario():
        fake = FakeHA(valid=("good-token",), threshold=None)
        clock = Clock()
        redis = ha_auth.MemoryRedis(clock=clock)
        orch = make_stack(fake=fake, token="bad-one", redis=redis, clock=clock, instance="pod-a", address="A", gate_wait=0.2)
        gateway = make_stack(fake=fake, token="good-token", redis=redis, clock=clock, instance="gw", address="B", caller="gateway_rest", gate_wait=0.2)

        async def idle(seconds):
            """Time passes; the gateway's registry keeps refreshing its proof from B."""
            while seconds > 0:
                step = min(seconds, 5 * MINUTE)
                clock.advance(step)
                seconds -= step
                await gateway.channel.request("GET", "/api/states")

        async def episode(stack):
            for step in (0, 121, 21601 - 121):
                await stack.channel.request("GET", "/api/states")
                await idle(step)
            await stack.channel.request("GET", "/api/states")

        await gateway.channel.request("GET", "/api/states")  # the gateway's token is proven before anything goes wrong
        await episode(orch)
        first = fake.count("A")
        async with ha_ws.ha_ws_session(  # its registry refresh signs in from B: the cluster's count resets, A's doesn't
            gateway.creds, gateway.latch, caller="gateway_registry", profile=ha_ws.REGISTRY_READ, connect=fake.ws_connect("B"),
        ):
            pass
        assert fake.count("A") == first
        orch2 = make_stack(fake=fake, token="bad-two", redis=redis, clock=clock, instance="pod-a", address="A", gate_wait=0.2)
        await orch2.latch.clear_and_reopen(orch2.creds)
        clock.advance(HOUR)
        await episode(orch2)
        for s in (orch, gateway, orch2):
            await s.channel.aclose()
        return fake, first

    fake, first = run(scenario())
    assert first == 3
    assert fake.count("A") == 3 + 3, "documented exception (g): 3 for each episode another address ended"
    assert fake.count("B") == 0


# -- [h] Redis down at cold start ----------------------------------------------------------------------------------


def test_h_redis_unreachable_at_cold_start_sends_nothing():
    async def scenario():
        clock = Clock()
        redis = FlakyRedis(ha_auth.MemoryRedis(clock=clock))
        redis.down = True
        fake = FakeHA()
        stack = make_stack(fake=fake, redis=redis, clock=clock, gate_wait=0.05)
        responses = [await stack.channel.request("GET", "/api/states") for _ in range(5)]
        responses.append(await stack.channel.request("POST", ROUTE, json={"entity_id": "light.a"}))
        with pytest.raises(ha_ws.HAWithheld):
            async with ha_ws.ha_ws_session(stack.creds, stack.latch, caller="orchestrator_registry", profile=ha_ws.REGISTRY_READ, connect=fake.ws_connect()):
                pass
        await stack.channel.aclose()
        return fake, responses

    fake, responses = run(scenario())
    assert fake.rest_sent == [] and fake.ws_connects == [] and fake.failed_total == 0
    assert {r.status_code for r in responses} == {503}


# -- [i] a measured concurrent fan-out after a revocation ----------------------------------------------------------


async def _fan_out(world, k, *, revoke=True):
    if revoke:
        world.fake.revoke(TOKEN)
    return await asyncio.gather(*[
        world.request("pod-a", "POST", ROUTE, json={"entity_id": ENTITIES[i]}) for i in range(k)
    ])


def test_i_a_revoked_proven_token_with_eighteen_sends_in_flight_costs_eighteen():
    async def scenario(k, threshold):
        world = await _proven_world(delay=0.05, threshold=threshold, gate_wait=0.5)
        world.clock.advance(1)
        responses = await _fan_out(world, k)
        after_burst = world.total()
        for i in range(20):  # later traffic, a day of it
            await world.request("pod-a", "POST", ROUTE, json={"entity_id": f"light.late{i}"})
            world.clock.advance(HOUR)
        await world.close()
        return world, responses, after_burst

    world, responses, burst = run(scenario(18, None))
    assert burst == 18 and world.total() == 18, "the stated exception (b), measured"
    world, _, burst = run(scenario(2, None))
    assert burst == 2 and world.total() == 3
    world, _, burst = run(scenario(25, None))
    assert burst == 18 and world.total() == 18, "the in-flight cap bounds K"
    world, _, _ = run(scenario(18, 5))
    assert "A" in world.fake.banned, "with a threshold of 5 the same burst bans the address (the exception, not hidden)"


def test_i2_after_ten_seconds_of_silence_a_revoked_token_costs_three_and_the_rest_send_nothing():
    async def scenario():
        world = await _proven_world(delay=0.05, gate_wait=1.0)
        world.clock.advance(11)
        responses = await _fan_out(world, 18)
        burst = world.total()
        world.clock.advance(121)
        await world.request("pod-a", "POST", ROUTE, json={"entity_id": "light.after"})
        world.clock.advance(7 * HOUR)
        await world.request("pod-a", "POST", ROUTE, json={"entity_id": "light.much_later"})
        await world.close()
        return world, responses, burst

    world, responses, burst = run(scenario())
    assert burst == 2, "the stale canary fails alone, then one gate proof"
    assert world.total() == 3 and world.fake.banned == set()
    rest_401s = [r for r in responses if "athena_ha_withheld" not in r.extensions]
    assert len(rest_401s) == 1, "only the canary reached Home Assistant over REST"


def test_i2_the_canary_costs_one_round_trip_and_a_fresh_proof_costs_none():
    async def timed(age):
        world = await _proven_world(delay=0.05, gate_wait=1.0)
        world.clock.advance(age)
        started = time.monotonic()
        responses = await _fan_out(world, 18, revoke=False)
        elapsed = time.monotonic() - started
        await world.close()
        return elapsed, {r.status_code for r in responses}

    fresh_elapsed, fresh_statuses = run(timed(1))
    stale_elapsed, stale_statuses = run(timed(11))
    assert fresh_statuses == stale_statuses == {200}
    assert fresh_elapsed < 0.05 + 0.1, "no canary within the window"
    assert 0.1 <= stale_elapsed < 0.1 + 3 * 0.05, "one canary round trip, then the rest in parallel"


# -- [j] two bad digests at once ----------------------------------------------------------------------------------


def test_j_two_bad_digests_racing_cost_three_in_all():
    async def scenario():
        clock = Clock()
        fake = FakeHA(valid=())
        redis = ha_auth.MemoryRedis(clock=clock)
        one = make_stack(fake=fake, token="digest-one", redis=redis, clock=clock, instance="pod-a", gate_wait=0.3)
        two = make_stack(fake=fake, token="digest-two", redis=redis, clock=clock, instance="pod-b", gate_wait=0.3)
        for step in (0, 121, 21601, 3 * DAY):
            await asyncio.gather(*[s.channel.request("GET", "/api/states") for s in (one, two) for _ in range(5)])
            clock.advance(step if step < DAY else step)
            clock.advance(121 if step == 0 else 0)
        await one.channel.aclose()
        await two.channel.aclose()
        return fake

    fake = run(scenario())
    assert fake.failed_total == 3 and fake.banned == set()


# -- [k] a gate holder that dies mid-proof ----------------------------------------------------------------------------


def test_k_a_holder_cancelled_after_sending_auth_leaves_its_attempt_counted():
    async def scenario():
        fake = FakeHA(valid=(), auth_reply_delay=5.0)
        clock = Clock()
        redis = ha_auth.MemoryRedis(clock=clock)
        stack = make_stack(fake=fake, redis=redis, clock=clock, instance="pod-a", gate_wait=0.3, gate_lease=2.0)
        task = asyncio.ensure_future(stack.channel.request("GET", "/api/states"))
        while fake.ws_auth_frames == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.failed_total == 1
        fake.auth_reply_delay = 0.0
        clock.advance(11)  # the lease would have expired too
        for step in (121, 21601, 3 * DAY):
            await stack.channel.request("GET", "/api/states")
            clock.advance(step)
        await stack.channel.request("GET", "/api/states")
        await stack.channel.aclose()
        return fake

    fake = run(scenario())
    assert fake.failed_total <= 3 and fake.banned == set()


# -- [l]/[m] a spent budget and a saved key --------------------------------------------------------------------------------


def test_l_a_spent_budget_with_a_good_unproven_token_sends_nothing_until_a_save():
    async def scenario():
        world = World(FakeHA(), gate_wait=0.3)
        await world.redis.hset(ha_auth.PREFIX + "budget:v2", mapping={"n": "3", "r": "0"})
        responses = await asyncio.gather(*[
            world.request("pod-a", "POST", f"/api/services/light/s{i}", json={"entity_id": "light.a"}) for i in range(10)
        ])
        await world.close()
        return world, responses

    world, responses = run(scenario())
    assert world.fake.rest_sent == [] and world.fake.ws_connects == [] and world.total() == 0
    assert {r.status_code for r in responses} == {401}
    assert all(r.extensions["athena_ha_withheld"] == "budget" for r in responses)


def test_m_a_save_after_a_spent_budget_lets_one_proof_through_and_service_resumes():
    async def scenario():
        world = World(FakeHA(), gate_wait=3.0)
        await world.redis.hset(ha_auth.PREFIX + "budget:v2", mapping={"n": "3", "r": "0"})
        stack = world.stack("pod-a")
        await stack.latch.clear_and_reopen(stack.creds)
        responses = await asyncio.gather(*[
            world.request("pod-a", "POST", f"/api/services/light/s{i}", json={"entity_id": "light.a"}) for i in range(10)
        ])
        budget = ha_auth._decode(await world.redis.hgetall(ha_auth.PREFIX + "budget:v2"))
        await world.close()
        return world, responses, budget

    world, responses, budget = run(scenario())
    assert {r.status_code for r in responses} == {200}
    assert world.fake.ws_auth_frames == 1 and world.total() == 0
    assert budget["n"] == "0", "the sign-in's auth_ok cleared the count"


# -- [n] the documented rotation order ----------------------------------------------------------------------------------------


def test_n_saving_the_new_token_first_and_revoking_the_old_one_later_costs_nothing():
    from shared.ha_credentials import HACredentialResolver, KeyLookup

    async def scenario():
        fake = FakeHA(valid=("old-token", "new-token"))
        clock = Clock()
        redis = ha_auth.MemoryRedis(clock=clock)
        current = {"token": "old-token"}

        async def source():
            return KeyLookup.found(URL, current["token"])

        from shared.ha_http import HAChannel

        async def resolves(host, port, **kw):
            return [(2, 1, 6, "", ("192.0.2.7", 0))]

        resolver = HACredentialResolver(source, service_api_key="service-key-for-tests", env={}, getaddrinfo=resolves)
        await resolver.start()
        latch = make_latch(redis, clock=clock, gate_wait=0.5)
        channel = HAChannel(resolver, latch, caller="orchestrator_rest", inner=fake.rest_transport(), ws_connect=fake.ws_connect())
        for i in range(3):
            assert (await channel.request("POST", ROUTE, json={"entity_id": f"light.o{i}"})).status_code == 200
        current["token"] = "new-token"  # save the new token in Athena
        await resolver.refresh(force=True)
        await latch.clear_and_reopen(resolver.current())
        for i in range(3):
            assert (await channel.request("POST", ROUTE, json={"entity_id": f"light.p{i}"})).status_code == 200
        clock.advance(2 * MINUTE)  # wait two minutes
        fake.revoke("old-token")  # then delete the old one in Home Assistant
        for i in range(5):
            assert (await channel.request("POST", ROUTE, json={"entity_id": f"light.q{i}"})).status_code == 200
        await channel.aclose()
        return fake

    fake = run(scenario())
    assert fake.failed_total == 0
