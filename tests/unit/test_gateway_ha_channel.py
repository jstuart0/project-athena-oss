"""The gateway reaches Home Assistant only through GatewayHA, over the shared channel.

Runs in its own process: it imports gateway.main behind a stubbed prometheus_client.
The lifespan's wiring is rebuilt here from the same parts (resolver over the admin
key adapter, one latch, one channel inside GatewayHA); the AST pins that the
lifespan builds exactly that are in test_ha_consolidation_drift.py.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

sys.modules.setdefault("prometheus_client", mock.MagicMock())

import gateway.main as gw  # noqa: E402
from shared import household  # noqa: E402

from .ha_auth_fakes import Clock, FakeHA, TOKEN, URL  # noqa: E402

# The new shared modules are imported where they are used, so this file collects
# (and its environment-free test runs) against the tree before they existed.

SATELLITE = "assist_satellite.study_voice"


def run(coro):
    return asyncio.run(coro)


class AdminFake:
    """Just the admin client's key lookup."""

    def __init__(self, status="found", url=URL, token=TOKEN):
        self.status, self.url, self.token = status, url, token

    async def lookup_external_api_key(self, service):
        assert service == "home-assistant"
        if self.status == "found":
            return "found", {"endpoint_url": self.url, "api_key": self.token}
        return self.status, None


async def build(fake, *, admin=None, env=None, redis=None, clock=None, gate_wait=0.2):
    from gateway.gateway_ha import GatewayHA
    from shared import ha_auth
    from shared.ha_credentials import HACredentialResolver, admin_http_source
    from shared.ha_http import HAChannel

    clock = clock or Clock()
    redis = redis if redis is not None else ha_auth.MemoryRedis(clock=clock)

    async def resolves(host, port, **kw):
        return [(2, 1, 6, "", ("192.0.2.7", 0))]

    resolver = HACredentialResolver(
        admin_http_source(admin or AdminFake()), service_api_key="service-key-for-tests", env=env or {}, getaddrinfo=resolves,
    )
    await resolver.start()
    latch = ha_auth.HAAuthLatch(redis, instance="gateway-1", service_api_key="service-key-for-tests", wall=clock, gate_wait=gate_wait)
    channel = HAChannel(resolver, latch, caller="gateway_rest", inner=fake.rest_transport("G"), ws_connect=fake.ws_connect("G"))
    return GatewayHA(channel), resolver, latch, redis


class StatesHA(FakeHA):
    def _rest(self, request, address):
        response = super()._rest(request, address)
        if response.status_code == 200 and request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": SATELLITE}, {"entity_id": "light.a"}], request=request)
        return response


def test_the_admin_key_wins_over_the_environment_for_the_gateway():
    async def go():
        fake = StatesHA(valid=("admin-token",))
        access, resolver, *_ = await build(
            fake, admin=AdminFake(token="admin-token"), env={"HA_URL": "https://env.example.invalid", "HA_TOKEN": "env-token"},
        )
        return await access.read_states(), resolver.current().source, fake.rest_sent

    result, source, sent = run(go())
    assert source == "admin" and [s["entity_id"] for s in result] == [SATELLITE, "light.a"]
    assert sent == [("G", "GET", "/api/states")]


def test_an_install_with_only_environment_credentials_still_works():
    async def go():
        fake = StatesHA(valid=("env-token",))
        access, resolver, *_ = await build(
            fake, admin=AdminFake("absent"), env={"HA_URL": URL, "HA_TOKEN": "env-token"},
        )
        return await access.read_states(), resolver.current().source

    result, source = run(go())
    assert source == "env" and result


def test_a_401_on_the_satellite_read_trips_the_latch_and_the_next_read_sends_nothing():
    async def go():
        fake = StatesHA(valid=())
        access, *_ = await build(fake)
        first = await access.read_states()
        second = await access.read_states()
        return first, second, fake

    first, second, fake = run(go())
    assert first == [] and second == []
    assert fake.rest_sent == [] and fake.failed_total == 1, "one failed sign-in in all: the gate's proof"


def test_the_first_request_waits_on_a_gate_another_process_holds_and_sends_nothing_when_that_proof_failed():
    async def go():
        from shared import ha_auth

        clock = Clock()
        redis = ha_auth.MemoryRedis(clock=clock)
        fake = StatesHA(valid=())
        other = ha_auth.HAAuthLatch(redis, instance="orchestrator-1", service_api_key="service-key-for-tests", wall=clock)
        access, resolver, latch, _ = await build(fake, redis=redis, clock=clock, gate_wait=2.0)
        snapshot = resolver.current()
        held = await other.admit(snapshot, method="GET", path="/api/states", key="GET /api/states")
        assert held.kind == ha_auth.PROVE
        waiting = asyncio.ensure_future(access.read_states())
        await asyncio.sleep(0.15)
        assert not waiting.done(), "the gateway waits for the proof another process is running"
        await other.observe(snapshot, held, caller="orchestrator_rest", key="GET /api/states", result="auth_rejected")
        return await waiting, fake

    result, fake = run(go())
    assert result == [] and fake.rest_sent == [] and fake.ws_connects == [], "the gateway adds no sign-in of its own"


def test_gateway_ha_exposes_two_operations_and_refuses_everything_else():
    async def go():
        fake = StatesHA()
        access, *_ = await build(fake)
        for name in ("call_service", "request", "_channel", "_GatewayHA__channel_x", "get", "post"):
            with pytest.raises(AttributeError):
                getattr(access, name)
        with pytest.raises(AttributeError):
            access.anything = 1
        return fake

    fake = run(go())
    assert fake.rest_sent == []


@pytest.mark.parametrize("payload", [
    {"entity_id": "light.x", "message": "hello"},
    {"entity_id": "assist_satellite.a,light.b", "message": "hello"},
    {"entity_id": "all", "message": "hello"},
    {"entity_id": "none", "message": "hello"},
    {"entity_id": ["assist_satellite.a", 3], "message": "hello"},
    {"entity_id": [], "message": "hello"},
    {"entity_id": "assist_satellite.A", "message": "hello"},
    {"entity_id": SATELLITE, "message": "x" * 5001},
    {"entity_id": SATELLITE, "message": ""},
    {"entity_id": SATELLITE, "message": 5},
    {"entity_id": SATELLITE, "message": "hi", "preannounce": "yes"},
    {"entity_id": SATELLITE, "message": "hi", "extra": 1},
    {"entity_id": SATELLITE},
    {"message": "hi"},
    "not a dict",
    None,
])
def test_an_announcement_that_is_not_one_is_refused_before_any_send(payload):
    async def go():
        fake = StatesHA()
        access, *_ = await build(fake)
        with pytest.raises(ValueError):
            await access.announce(payload)
        return fake

    fake = run(go())
    assert fake.rest_sent == [] and fake.ws_connects == []


def test_a_valid_announcement_is_one_post_and_returns_whether_it_worked():
    async def go():
        fake = StatesHA()
        access, *_ = await build(fake)
        ok = await access.announce({"entity_id": SATELLITE, "message": "hello", "preannounce": False})
        many = await access.announce({"entity_id": [SATELLITE, "assist_satellite.kitchen_voice"], "message": "x" * 5000})
        return ok, many, fake.rest_sent

    ok, many, sent = run(go())
    assert ok is True and many is True
    assert sent.count(("G", "POST", "/api/services/assist_satellite/announce")) == 2


def test_not_configured_reads_nothing_and_announces_nothing():
    async def go():
        fake = StatesHA()
        access, *_ = await build(fake, admin=AdminFake("absent"))
        return access.configured, await access.read_states(), await access.announce({"entity_id": SATELLITE, "message": "hi"}), fake

    configured, read, announced, fake = run(go())
    assert (configured, read, announced) == (False, [], False)
    assert fake.rest_sent == [] and fake.ws_connects == []


def test_the_gateways_two_call_sites_go_through_gateway_ha(monkeypatch):
    async def go():
        fake = StatesHA()
        access, *_ = await build(fake)
        monkeypatch.setattr(gw, "gateway_ha", access)
        household.install_for_test(sources={"speaker_rooms": {"entries": [{"key": SATELLITE, "room": "study"}]}})
        satellites = await gw._fetch_assist_satellite_states()
        announced = await gw.send_satellite_announcement("study", "hello")
        return satellites, announced, fake.rest_sent

    satellites, announced, sent = run(go())
    assert [s["entity_id"] for s in satellites] == [SATELLITE] and announced is True
    assert ("G", "POST", "/api/services/assist_satellite/announce") in sent


def test_the_gateway_without_home_assistant_answers_empty_and_false(monkeypatch):
    monkeypatch.setattr(gw, "gateway_ha", None)
    assert run(gw._fetch_assist_satellite_states()) == []
    assert run(gw.send_satellite_announcement("study", "hello")) is False


def test_the_gateway_takes_nothing_from_the_environment_when_home_assistant_is_not_configured(monkeypatch):
    """No HA_URL fallback and no HA_TOKEN read in the gateway: with the variables
    set and no configured access, the two call sites send nothing anywhere."""
    sent = []

    class Recording:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            sent.append(("GET", str(url)))
            return httpx.Response(200, json=[{"entity_id": SATELLITE}])

        async def post(self, url, **kwargs):
            sent.append(("POST", str(url)))
            return httpx.Response(200, json=[])

    monkeypatch.setenv("HA_URL", "http://env-fixture.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "env-token")
    monkeypatch.setattr(gw.httpx, "AsyncClient", Recording)
    monkeypatch.setattr(gw, "gateway_ha", None, raising=False)
    monkeypatch.setattr(gw, "ha_client", None, raising=False)
    household.install_for_test(sources={"speaker_rooms": {"entries": [{"key": SATELLITE, "room": "study"}]}})
    assert run(gw._fetch_assist_satellite_states()) == []
    assert run(gw.send_satellite_announcement("study", "hello")) is False
    assert sent == []
