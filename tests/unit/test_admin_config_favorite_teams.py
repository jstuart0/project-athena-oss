"""AdminConfigClient.get_favorite_teams: the service key goes on the wire, the
answer is cached by timestamp, one request serves concurrent turns, and an
outage returns the last good list without queueing anyone."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import shared.admin_config as admin_config
from shared.admin_config import AdminConfigClient
from shared.favorite_teams import FavoriteTeam

KEY = "test-service-key-favorite-teams"
OWLS = {"name": "Anytown Owls", "league": "nfl"}
HAWKS = {"name": "Hawks", "league": None}


class Wire:
    """A scripted admin-backend: pops one response per request, records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.delay = 0.0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(*teams):
    return httpx.Response(200, json={"teams": list(teams), "revision": 3, "leagues": [], "max": 8})


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(admin_config, "_clock", lambda: state["now"])
    return state


@pytest.fixture
def refusals(monkeypatch):
    seen = []
    monkeypatch.setattr(admin_config, "note_admin_refusal", lambda status, template: seen.append((status, template)))
    return seen


def make_client(wire: Wire) -> AdminConfigClient:
    client = AdminConfigClient(admin_url="http://admin.example:8080", api_key=KEY)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(wire.handler), timeout=3.0)
    return client


def get(client):
    return asyncio.run(client.get_favorite_teams())


def run_all(client, n):
    async def go():
        return await asyncio.gather(*[client.get_favorite_teams() for _ in range(n)])

    return asyncio.run(go())


def names(teams):
    return [t.name for t in teams]


def test_call_carries_the_service_key_and_the_route(clock):
    wire = Wire(ok(OWLS))
    teams = get(make_client(wire))
    (request,) = wire.requests
    assert request.headers["X-Service-Key"] == KEY
    assert request.url.path == "/api/settings/favorite-teams"
    assert teams == (FavoriteTeam(name="Anytown Owls", league="nfl"),)


def test_success_is_cached_within_the_ttl(clock):
    wire = Wire(ok(OWLS))
    client = make_client(wire)

    async def twice():
        first = await client.get_favorite_teams()
        clock["now"] += 59
        return first, await client.get_favorite_teams()

    first, second = asyncio.run(twice())
    assert first == second and len(wire.requests) == 1


def test_a_503_after_the_ttl_returns_the_last_good_value(clock, refusals):
    wire = Wire(ok(OWLS), httpx.Response(503))
    client = make_client(wire)

    async def go():
        await client.get_favorite_teams()
        clock["now"] += 61
        return await client.get_favorite_teams()

    assert names(asyncio.run(go())) == ["Anytown Owls"]
    assert len(wire.requests) == 2
    assert refusals == [(503, "/api/settings/favorite-teams")]


def test_a_503_with_no_prior_success_is_empty(clock, refusals):
    wire = Wire(httpx.Response(503))
    assert get(make_client(wire)) == ()
    assert refusals == [(503, "/api/settings/favorite-teams")]


def test_a_transport_error_is_empty_and_logs_the_class_only(clock, refusals, capsys):
    wire = Wire(httpx.ConnectError("boom: http://secret.example/path"))
    assert get(make_client(wire)) == ()
    out = capsys.readouterr()
    assert "secret.example" not in out.out + out.err
    assert refusals == []


def test_an_authoritative_empty_replaces_a_prior_list_and_is_cached(clock):
    wire = Wire(ok(OWLS, HAWKS), ok())
    client = make_client(wire)

    async def go():
        first = await client.get_favorite_teams()
        clock["now"] += 61
        second = await client.get_favorite_teams()
        clock["now"] += 30
        third = await client.get_favorite_teams()
        return first, second, third

    first, second, third = asyncio.run(go())
    assert names(first) == ["Anytown Owls", "Hawks"]
    assert second == () and third == ()
    assert len(wire.requests) == 2


def test_concurrent_cold_calls_make_one_request(clock):
    wire = Wire(ok(OWLS))
    wire.delay = 0.05
    results = run_all(make_client(wire), 10)
    assert len(wire.requests) == 1
    assert all(names(r) == ["Anytown Owls"] for r in results)


def test_outage_retries_once_per_ttl(clock):
    wire = Wire(ok(OWLS), httpx.Response(503), httpx.Response(503), httpx.Response(503))
    client = make_client(wire)

    async def go():
        await client.get_favorite_teams()
        clock["now"] += 61
        await client.get_favorite_teams()  # first failure
        assert len(wire.requests) == 2
        clock["now"] += 10
        during = await client.get_favorite_teams()  # backed off: no request
        assert len(wire.requests) == 2
        clock["now"] += 51
        after = await client.get_favorite_teams()  # 61 s after the failure
        return during, after

    during, after = asyncio.run(go())
    assert names(during) == names(after) == ["Anytown Owls"]
    assert len(wire.requests) == 3


def test_invalid_items_from_admin_are_dropped_not_fatal(clock):
    wire = Wire(ok(OWLS, {"name": "<b>x</b>", "league": None}, {"name": "Hawks", "league": "curling"}))
    assert names(get(make_client(wire))) == ["Anytown Owls"]


def test_a_non_object_200_body_is_empty_when_cold_and_never_wipes_the_last_good_list(clock):
    assert get(make_client(Wire(httpx.Response(200, json=[1, 2])))) == ()
    assert get(make_client(Wire(httpx.Response(200, json={"teams": "nope"})))) == ()
    wire = Wire(ok(OWLS), httpx.Response(200, json=[1, 2]))
    client = make_client(wire)

    async def go():
        await client.get_favorite_teams()
        clock["now"] += 61
        return await client.get_favorite_teams()

    assert names(asyncio.run(go())) == ["Anytown Owls"]
    assert len(wire.requests) == 2


def test_turns_do_not_wait_on_a_refresh_that_is_in_flight(clock):
    wire = Wire(ok(OWLS), ok(OWLS, HAWKS))
    client = make_client(wire)

    async def go():
        await client.get_favorite_teams()
        clock["now"] += 61
        wire.delay = 0.3
        refresh = asyncio.ensure_future(client.get_favorite_teams())
        await asyncio.sleep(0.05)  # the refresh holds the lock, waiting on admin
        started = asyncio.get_running_loop().time()
        during = [await client.get_favorite_teams() for _ in range(5)]
        waited = asyncio.get_running_loop().time() - started
        return during, waited, await refresh

    during, waited, refreshed = asyncio.run(go())
    assert waited < 0.1 and all(names(d) == ["Anytown Owls"] for d in during)
    assert names(refreshed) == ["Anytown Owls", "Hawks"]
    assert len(wire.requests) == 2


def test_the_request_has_its_own_short_timeout(clock):
    wire = Wire(ok(OWLS))
    client = make_client(wire)
    seen = {}
    real_get = client.client.get

    async def spy(url, **kwargs):
        seen.update(kwargs)
        return await real_get(url, **kwargs)

    client.client.get = spy
    asyncio.run(client.get_favorite_teams())
    assert seen["timeout"] == 2.0
