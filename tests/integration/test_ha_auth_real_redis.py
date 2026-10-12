"""The failed sign-in budget on a real Redis server.

The unit tier runs the scripts' Python mirror. This runs the Lua itself, so the
things only a server can get wrong are covered: a lease and a reservation taken
atomically, replies that arrive as bytes under ``decode_responses=False``, TTLs,
and two latches racing on one server. It needs ``HA_AUTH_TEST_REDIS_URL`` (a
database it may flush), and fails rather than skips when that is unset or does
not answer: a tier that can be skipped can pass without running.

The scenarios are the table in ``tests/unit/ha_auth_scenarios.py``; the
end-to-end cases here are [a], [j] and [k] of ``test_ha_auth_bound_end_to_end``.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from unit import ha_auth_scenarios as scenarios  # noqa: E402
from unit.ha_auth_fakes import Clock, FakeHA, make_stack  # noqa: E402

from shared import ha_auth  # noqa: E402

pytestmark = pytest.mark.integration

URL_VARIABLE = "HA_AUTH_TEST_REDIS_URL"


@pytest.fixture(scope="module")
def redis_url() -> str:
    url = os.environ.get(URL_VARIABLE)
    if not url:
        pytest.fail(f"{URL_VARIABLE} is not set: the real-Redis tier must run against a server, not skip")
    import redis

    try:
        redis.Redis.from_url(url, socket_connect_timeout=3).ping()
    except Exception as exc:
        pytest.fail(f"{URL_VARIABLE} does not answer PING ({type(exc).__name__}): the real-Redis tier cannot pass vacuously")
    return url


def run(coro):
    return asyncio.run(coro)


async def connect(url: str, decode: bool):
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, decode_responses=decode)
    await client.flushdb()
    return client


@pytest.mark.parametrize("decode", [True, False], ids=["str", "bytes"])
@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_the_scenario_table_on_a_real_server(redis_url, name, decode):
    async def scenario():
        client = await connect(redis_url, decode)
        clock = Clock()

        async def pass_time(seconds):
            await asyncio.sleep(seconds)  # a real TTL has to really pass
            clock.advance(seconds)

        try:
            await scenarios.SCENARIOS[name](scenarios.Env(redis=client, clock=clock, pass_time=pass_time))
        finally:
            await client.aclose()

    run(scenario())


@pytest.mark.parametrize("decode", [True, False], ids=["str", "bytes"])
def test_every_script_replies_in_types_the_decoder_reads(redis_url, decode):
    async def scenario():
        client = await connect(redis_url, decode)
        latch = make_stack(redis=client, clock=Clock()).latch
        snap = make_stack().creds
        keys = [scenarios.BUDGET_KEY, scenarios.key("gate:v1", snap), scenarios.key("latch:v1", snap), scenarios.key("proven:v2", snap)]
        proof = await latch._call("reserve_proof", keys, ["holder", 1000, ha_auth.BUDGET, "2026-10-12T00:00:00+00:00", "", ""])
        request = await latch._call("reserve_request", [scenarios.BUDGET_KEY], [ha_auth.REQUEST_ALLOWANCE])
        reopen = await latch._call("reopen", [scenarios.BUDGET_KEY], [ha_auth.BUDGET])
        epoch = await latch._call("clear_on_auth_ok", [
            scenarios.BUDGET_KEY, scenarios.CALLERS_KEY, scenarios.key("proven:v2", snap),
            scenarios.key("latch:v1", snap), scenarios.key("gate:v1", snap), latch._legacy_key(snap),
        ], ["{}", 60])
        ttl = await client.ttl(scenarios.BUDGET_KEY)
        await client.aclose()
        return proof, request, reopen, epoch, ttl

    proof, request, reopen, epoch, ttl = run(scenario())
    assert proof == [1, 0] and request[0] == 1 and isinstance(reopen, int) and epoch == 1
    assert ttl == -1, "no script gives the budget hash a TTL"


# -- end to end, with the fake Home Assistant and a real Redis ---------------------------------------


@pytest.mark.parametrize("decode", [True, False], ids=["str", "bytes"])
def test_a_a_burst_on_a_bad_token_across_callers_costs_one_failed_sign_in(redis_url, decode):
    async def scenario():
        client = await connect(redis_url, decode)
        clock, fake = Clock(), FakeHA(valid=())
        stacks = [
            make_stack(fake=fake, redis=client, clock=clock, instance=f"pod-{i}", gate_wait=1.5)
            for i in range(4)
        ]
        statuses = await asyncio.gather(*[stacks[i % 4].channel.request("GET", "/api/states") for i in range(20)])
        for stack in stacks:
            await stack.channel.aclose()
        ttl = await client.ttl(scenarios.BUDGET_KEY)
        await client.aclose()
        return fake, statuses, ttl

    fake, statuses, ttl = run(scenario())
    assert fake.failed_total == 1 and fake.banned == set()
    assert {s.status_code for s in statuses} <= {401, 503}
    assert ttl == -1


@pytest.mark.parametrize("decode", [True, False], ids=["str", "bytes"])
def test_j_two_bad_digests_racing_cost_three_in_all(redis_url, decode):
    async def scenario():
        client = await connect(redis_url, decode)
        clock, fake = Clock(), FakeHA(valid=())
        one = make_stack(fake=fake, token="digest-one", redis=client, clock=clock, instance="pod-a", gate_wait=0.5)
        two = make_stack(fake=fake, token="digest-two", redis=client, clock=clock, instance="pod-b", gate_wait=0.5)
        for step in (0, 121, 21601, 3 * 86400):
            await asyncio.gather(*[s.channel.request("GET", "/api/states") for s in (one, two) for _ in range(5)])
            clock.advance(121 if step == 0 else step)
        await one.channel.aclose()
        await two.channel.aclose()
        await client.aclose()
        return fake

    fake = run(scenario())
    assert fake.failed_total == 3 and fake.banned == set()


@pytest.mark.parametrize("decode", [True, False], ids=["str", "bytes"])
def test_k_a_holder_cancelled_after_sending_auth_leaves_its_attempt_counted(redis_url, decode):
    async def scenario():
        client = await connect(redis_url, decode)
        clock, fake = Clock(), FakeHA(valid=(), auth_reply_delay=5.0)
        stack = make_stack(fake=fake, redis=client, clock=clock, instance="pod-a", gate_wait=0.3, gate_lease=2.0)
        task = asyncio.ensure_future(stack.channel.request("GET", "/api/states"))
        while fake.ws_auth_frames == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        fake.auth_reply_delay = 0.0
        for step in (121, 21601, 3 * 86400):
            await stack.channel.request("GET", "/api/states")
            clock.advance(step)
        await stack.channel.request("GET", "/api/states")
        await stack.channel.aclose()
        await client.aclose()
        return fake

    fake = run(scenario())
    assert fake.failed_total <= 3 and fake.banned == set()
