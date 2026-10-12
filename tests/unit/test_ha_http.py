"""HAChannel and HATransport: the one authenticated REST path.

The strict-reduction test runs through a factory, so the same assertion runs
against the base ``HomeAssistantClient`` (11 REST 401s reach Home Assistant)
and against the channel (one failed sign-in, the gate's proof).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import httpx
import pytest

from .ha_auth_fakes import Clock, FakeHA, FlakyRedis, TOKEN, URL, make_stack

try:
    from shared import ha_auth, ha_http
except ImportError:  # the base tree: only the old client exists
    ha_auth = ha_http = None

ROOT = pathlib.Path(__file__).resolve().parents[2]


def run(coro):
    return asyncio.run(coro)


class _OldClient:
    """The base ``HomeAssistantClient`` over the fake Home Assistant."""

    def __init__(self, fake: FakeHA, token: str = TOKEN):
        from shared.ha_client import HomeAssistantClient

        self._client = HomeAssistantClient(URL, token)
        self._client.client = httpx.AsyncClient(
            base_url=URL, headers=self._client.headers, transport=fake.rest_transport(),
        )

    async def get_states_status(self) -> int:
        try:
            await self._client.get_states()
            return 200
        except httpx.HTTPStatusError as exc:
            return exc.response.status_code

    async def close(self):
        await self._client.close()


class _Channel:
    def __init__(self, fake: FakeHA, token: str = TOKEN):
        self.stack = make_stack(fake=fake, token=token)
        self.channel = self.stack.channel
        self.responses = []

    async def get_states_status(self) -> int:
        response = await self.channel.request("GET", "/api/states")
        self.responses.append(response)
        return response.status_code

    async def close(self):
        await self.channel.aclose()


@pytest.fixture
def make_client():
    """The factory: the channel when it exists, the base client otherwise."""
    def build(fake: FakeHA, token: str = TOKEN):
        return _Channel(fake, token) if ha_http is not None else _OldClient(fake, token)
    return build


# -- the strict reduction -------------------------------------------------------------


def test_a_rejected_token_is_sent_once_then_withheld(make_client):
    async def scenario():
        fake = FakeHA(valid=(), threshold=None)  # rejects the token on REST and on the WebSocket
        client = make_client(fake)
        statuses = [await client.get_states_status() for _ in range(11)]
        await client.close()
        return fake, client, statuses

    fake, client, statuses = run(scenario())
    ha_failed_auths = fake.failed_total
    assert ha_failed_auths == 1, f"{ha_failed_auths} failed sign-ins reached Home Assistant"
    assert fake.rest_sent == [], "no REST request is sent for a rejected token"
    assert statuses == [401] * 11
    assert all(r.extensions.get(ha_http.WITHHELD_EXTENSION) == "latched" for r in client.responses)


# -- path validation ------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/../config", "/api/states/%2e%2e", "/api/states/light.a\\b", "/api/states?x=1", "/apix",
    "/api/services/light/turn_on/extra", "/api/services/light", "/api/states/light.a\n", "/api/states/Light.A",
    "/api/states/light", "/api/./states", "/api/states/light.a#x", "/api/st ates", "/api/states/\x00", "/", "",
    "http://elsewhere/api/states", "//elsewhere/api", "/api/services/light/turn%5Fon", "/api/services/li ght/turn_on",
    "/api/services/light/Turn_On", "/api/config/automation/config/..", "/api//states",
])
def test_a_path_that_cannot_name_an_api_resource_is_refused_before_any_send(path):
    async def scenario():
        fake = FakeHA()
        stack = make_stack(fake=fake)
        with pytest.raises(ValueError):
            await stack.channel.request("GET", path)
        await stack.channel.aclose()
        return fake

    fake = run(scenario())
    assert fake.rest_sent == [] and fake.ws_connects == []


@pytest.mark.parametrize("path", [
    "/api", "/api/states", "/api/states/light.living_room_1", "/api/services/light/turn_on", "/api/services",
    "/api/config/automation/config/abc_123", "/api/config/automation/config/a-b.c",
])
def test_a_clean_api_path_is_accepted(path):
    assert ha_http.validate_path(path) == path


# -- what the request carries ------------------------------------------------------------------


def test_the_request_carries_a_digest_and_never_the_token():
    async def scenario():
        fake = FakeHA()
        stack = make_stack(fake=fake)
        built = []
        original = stack.channel._client.send

        async def spy(request, **kw):
            built.append((repr(request), repr(request.extensions), dict(request.headers), dict(request.extensions)))
            return await original(request, **kw)

        stack.channel._client.send = spy
        response = await stack.channel.request("GET", "/api/states")
        await stack.channel.aclose()
        return stack, built, response

    stack, built, response = run(scenario())
    assert response.status_code == 200
    shown, extensions_shown, headers, extensions = built[0]
    assert extensions["athena_ha_digest"] == stack.creds.digest
    assert TOKEN not in shown and TOKEN not in extensions_shown and TOKEN not in repr(headers)
    assert "authorization" not in headers, "the header is set by the transport at send time"


def test_an_unknown_digest_is_refused_before_any_send():
    async def scenario():
        fake = FakeHA()
        stack = make_stack(fake=fake)
        request = stack.channel._client.build_request(
            "GET", URL + "/api/states", extensions={"athena_ha_digest": "not-a-digest"},
        )
        with pytest.raises(RuntimeError):
            await stack.channel._client.send(request)
        request = stack.channel._client.build_request("GET", URL + "/api/states")
        with pytest.raises(RuntimeError):
            await stack.channel._client.send(request)
        await stack.channel.aclose()
        return fake

    fake = run(scenario())
    assert fake.rest_sent == [] and fake.ws_connects == []


def test_not_configured_raises_before_any_send():
    from shared.ha_credentials import HACredentials, StaticResolver
    from shared.ha_http import HAChannel, HomeAssistantNotConfiguredError

    async def scenario():
        fake = FakeHA()
        stack = make_stack(fake=fake)
        channel = HAChannel(StaticResolver(HACredentials()), stack.latch, caller="orchestrator_rest", inner=fake.rest_transport())
        assert channel.configured is False
        with pytest.raises(HomeAssistantNotConfiguredError):
            await channel.request("GET", "/api/states")
        await channel.aclose()
        return fake

    assert run(scenario()).rest_sent == []


def test_a_token_change_applies_on_the_next_request():
    from shared.ha_credentials import StaticResolver

    async def scenario():
        fake = FakeHA(valid=("new-token",))
        stack = make_stack(fake=fake, token="new-token")
        status = (await stack.channel.request("GET", "/api/states")).status_code
        await stack.channel.aclose()
        return status

    assert run(scenario()) == 200


def test_the_proxy_environment_is_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")

    async def scenario():
        from shared.ha_credentials import StaticResolver
        from shared.ha_http import HAChannel

        stack = make_stack()
        channel = HAChannel(stack.resolver, stack.latch, caller="orchestrator_rest")
        proxied = [m for m in channel._client._mounts]
        await channel.aclose()
        return proxied

    assert run(scenario()) == [], "an explicit transport means httpx reads no *_PROXY variable"


# -- the gate in front of a proven token -----------------------------------------------------


def test_a_proven_token_goes_through_and_a_first_use_goes_out_as_a_reserved_canary():
    async def scenario():
        fake = FakeHA()
        stack = make_stack(fake=fake)
        kinds = []
        original = stack.latch.admit

        async def spy(*a, **kw):
            admission = await original(*a, **kw)
            kinds.append(admission.kind)
            return admission

        stack.latch.admit = spy
        first = await stack.channel.request("GET", "/api/states")
        call = await stack.channel.request("POST", "/api/services/light/turn_on", json={"entity_id": "light.a"})
        await stack.channel.aclose()
        return fake, kinds, first, call

    fake, kinds, first, call = run(scenario())
    assert (first.status_code, call.status_code) == (200, 200)
    assert kinds == [ha_auth.PROVE, ha_auth.OPEN, ha_auth.CANARY], kinds
    assert fake.failed_total == 0 and fake.ws_auth_frames == 1, "one proof, then plain requests"


def test_a_cancelled_send_is_observed_once_as_other_and_gives_its_slot_back():
    async def scenario():
        fake = FakeHA(delay=0.2)
        stack = make_stack(fake=fake)
        observed = []
        original = stack.latch.observe

        async def spy(snapshot, admission, **kw):
            observed.append(kw["result"])
            return await original(snapshot, admission, **kw)

        stack.latch.observe = spy
        await stack.channel.request("GET", "/api/states")  # prove first
        observed.clear()
        task = asyncio.ensure_future(stack.channel.request("GET", "/api/states"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stack.channel.aclose()
        return observed, stack.latch._slots._value

    observed, slots = run(scenario())
    assert observed == ["other"]
    assert slots == ha_auth.INFLIGHT_CAP


def test_a_gated_response_is_not_recorded_or_observed():
    from shared import ha_link

    async def scenario():
        clock = Clock()
        redis = FlakyRedis(ha_auth.MemoryRedis(clock=clock))
        redis.down = True
        monitor = ha_link.HALinkMonitor()
        stack = make_stack(redis=redis, clock=clock, monitor=monitor, gate_wait=0.05)
        observed = []
        original = stack.latch.observe

        async def spy(*a, **kw):
            observed.append(kw["result"])
            return await original(*a, **kw)

        stack.latch.observe = spy
        response = await stack.channel.request("GET", "/api/states")
        await stack.channel.aclose()
        return response, observed, monitor, stack.fake

    response, observed, monitor, fake = run(scenario())
    assert response.status_code == 503 and response.extensions["athena_ha_withheld"] == "gated"
    assert observed == [] and monitor.summary()["last_outcome"] is None
    assert fake.rest_sent == [] and fake.ws_connects == []


def test_a_withheld_response_reads_like_a_real_401_and_a_gated_one_like_a_real_5xx():
    from orchestrator.model_safe_errors import PHRASES, model_safe_error

    async def scenario():
        fake = FakeHA(valid=())
        stack = make_stack(fake=fake)
        rejected = await stack.channel.request("GET", "/api/states")
        clock = Clock()
        down = FlakyRedis(ha_auth.MemoryRedis(clock=clock))
        down.down = True
        gated = await make_stack(redis=down, clock=clock, gate_wait=0.05).channel.request("GET", "/api/states")
        return rejected, gated

    rejected, gated = run(scenario())
    for response in (rejected, gated):
        with pytest.raises(httpx.HTTPStatusError) as caught:
            response.raise_for_status()
        phrase = model_safe_error(caught.value)
        assert phrase == (PHRASES["not_configured"] if response is rejected else PHRASES["unavailable"])


def test_a_withheld_request_asks_the_resolver_to_look_again_once_after_a_send():
    class Resolver:
        def __init__(self, inner):
            self._inner, self.refreshes = inner, 0

        def current(self):
            return self._inner.current()

        def snapshot_for(self, digest):
            return self._inner.snapshot_for(digest)

        async def refresh(self, force=False):
            assert force is True
            self.refreshes += 1

    async def scenario():
        from shared.ha_http import HAChannel

        fake = FakeHA(valid=())
        stack = make_stack(fake=fake)
        resolver = Resolver(stack.resolver)
        channel = HAChannel(resolver, stack.latch, caller="orchestrator_rest", inner=fake.rest_transport(), ws_connect=fake.ws_connect())
        channel._transport._sent_since_withheld = True  # a request went out earlier
        for _ in range(5):
            await channel.request("GET", "/api/states")
            await asyncio.sleep(0)
        await channel.aclose()
        return resolver.refreshes

    assert run(scenario()) == 1, "one refresh after a send, not one per withheld request"


def test_aclose_cancels_background_refresh_tasks_and_no_run_resolution_exists():
    class SlowResolver:
        def __init__(self, inner):
            self._inner, self.cancelled = inner, False

        def current(self):
            return self._inner.current()

        def snapshot_for(self, digest):
            return self._inner.snapshot_for(digest)

        async def refresh(self, force=False):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario():
        from shared.ha_http import HAChannel

        fake = FakeHA()
        stack = make_stack(fake=fake)
        resolver = SlowResolver(stack.resolver)
        channel = HAChannel(resolver, stack.latch, caller="orchestrator_rest", inner=fake.rest_transport(), ws_connect=fake.ws_connect())
        await channel.request("GET", "/api/states")
        channel._transport._sent_since_withheld = True
        channel._transport._refresh_in_background()
        await asyncio.sleep(0)
        await channel.aclose()
        return resolver.cancelled, channel._transport._tasks

    cancelled, tasks = run(scenario())
    assert cancelled and not tasks
    source = (ROOT / "src" / "shared" / "ha_http.py").read_text()
    assert "_run_resolution" not in source


def test_the_authorization_header_is_built_in_one_function():
    tree = ast.parse((ROOT / "src" / "shared" / "ha_http.py").read_text())
    holders = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if any(isinstance(n, ast.Constant) and n.value == "Authorization" for n in ast.walk(fn)):
                holders.append(f"{cls.name}.{fn.name}")
    assert holders == ["HATransport.handle_async_request"]
    assert "/api/services" not in (ROOT / "src" / "shared" / "ha_http.py").read_text(), "ha_http names no service endpoint"
