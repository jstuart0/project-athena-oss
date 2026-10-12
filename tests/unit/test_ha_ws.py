"""ha_ws: the one WebSocket handshake, its frame profiles, and the token proof."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from shared import ha_auth, ha_ws
from shared.ha_credentials import HACredentials

from .ha_auth_fakes import (
    Clock, FakeHA, HangingConnection, InvalidStatus, TOKEN, URL, credentials, make_latch,
)


def run(coro):
    return asyncio.run(coro)


def fresh(fake=None, **kw):
    clock = Clock()
    return fake or FakeHA(**kw), make_latch(ha_auth.MemoryRedis(clock=clock), clock=clock, gate_wait=0.2), credentials()


async def open_session(latch, creds, fake, profile=ha_ws.REGISTRY_READ, address="A"):
    return ha_ws.ha_ws_session(creds, latch, caller="orchestrator_registry", profile=profile, connect=fake.ws_connect(address))


# -- profiles ----------------------------------------------------------------------------------


def test_the_named_profiles_are_exactly_the_documented_frame_types():
    assert ha_ws.REGISTRY_READ.types == {
        "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list",
    }
    assert ha_ws.PIPELINES.types == {"assist_pipeline/pipeline/list", "assist_pipeline/pipeline/set_preferred"}
    assert ha_ws.PROOF.types == frozenset()


def test_a_session_requires_one_of_the_profile_objects():
    async def scenario():
        fake, latch, creds = fresh()
        with pytest.raises(TypeError):
            async with ha_ws.ha_ws_session(creds, latch, caller="admin_ws", connect=fake.ws_connect()):
                pass
        foreign = ha_ws.Profile("registry_read", frozenset(ha_ws.REGISTRY_READ.types))
        with pytest.raises(TypeError):
            async with ha_ws.ha_ws_session(creds, latch, caller="admin_ws", profile=foreign, connect=fake.ws_connect()):
                pass
        with pytest.raises(TypeError):
            async with ha_ws.ha_ws_session(creds, latch, caller="admin_ws", profile=set(ha_ws.REGISTRY_READ.types), connect=fake.ws_connect()):
                pass
        return fake

    assert run(scenario()).ws_connects == []


def test_a_session_sends_the_auth_frame_once_and_only_its_profile():
    async def scenario():
        fake, latch, creds = fresh()
        async with ha_ws.ha_ws_session(creds, latch, caller="orchestrator_registry", profile=ha_ws.REGISTRY_READ, connect=fake.ws_connect()) as ws:
            first = await ws.command({"type": "config/area_registry/list"})
            second = await ws.command({"type": "config/device_registry/list"})
            connection = ws._ws
        return first, second, connection

    first, second, connection = run(scenario())
    assert (first["id"], second["id"]) == (1, 2) and first["success"] is True
    frames = connection.sent
    assert [f["type"] for f in frames] == ["auth", "config/area_registry/list", "config/device_registry/list"]
    assert sum("access_token" in f for f in frames) == 1 and frames[0]["access_token"] == TOKEN


@pytest.mark.parametrize("payload", [
    "config/area_registry/list", ["config/area_registry/list"], None, 3,
    {"type": 1}, {"type": None}, {}, {"type": "auth"}, {"type": "auth", "access_token": TOKEN},
    {"type": "config/entity_registry/list2"}, {"type": "assist_pipeline/pipeline/list"},
    {"type": "call_service", "domain": "light", "service": "turn_on"},
    {"type": "config/area_registry/list", "x": {"access_token": "t"}},
    {"type": "config/area_registry/list", "x": [{"y": {"access_token": "t"}}]},
    {"type": "config/area_registry/list", "id": 7},
])
def test_command_refuses_before_any_send(payload):
    async def scenario():
        fake, latch, creds = fresh()
        async with ha_ws.ha_ws_session(creds, latch, caller="orchestrator_registry", profile=ha_ws.REGISTRY_READ, connect=fake.ws_connect()) as ws:
            with pytest.raises(ha_ws.ForbiddenFrameType):
                await ws.command(payload)
            return [f["type"] for f in ws._ws.sent]

    assert run(scenario()) == ["auth"]


def test_the_pipelines_profile_may_set_a_preferred_pipeline_and_the_proof_profile_nothing():
    async def scenario():
        fake, latch, creds = fresh()
        async with ha_ws.ha_ws_session(creds, latch, caller="admin_ws", profile=ha_ws.PIPELINES, connect=fake.ws_connect()) as ws:
            await ws.command({"type": "assist_pipeline/pipeline/set_preferred", "pipeline_id": "abc"})
        async with ha_ws.ha_ws_session(creds, latch, caller="admin_ws", profile=ha_ws.PROOF, connect=fake.ws_connect()) as ws:
            with pytest.raises(ha_ws.ForbiddenFrameType):
                await ws.command({"type": "config/area_registry/list"})
            return [f["type"] for f in ws._ws.sent]

    assert run(scenario()) == ["auth"]


# -- outcomes feed the latch ----------------------------------------------------------------------


def test_auth_ok_clears_the_count_and_proves_the_digest():
    async def scenario():
        fake, latch, creds = fresh()
        fake.counts["A"] = 2
        async with await open_session(latch, creds, fake):
            pass
        return fake, latch

    fake, latch = run(scenario())
    assert fake.count("A") == 0
    assert latch._local_proof


def test_auth_invalid_raises_trips_the_latch_and_counts_once():
    async def scenario():
        fake, latch, creds = fresh(valid=())
        with pytest.raises(ha_ws.AuthInvalid):
            async with await open_session(latch, creds, fake):
                pass
        with pytest.raises(ha_ws.HAWithheld) as withheld:
            async with await open_session(latch, creds, fake):
                pass
        return fake, latch, creds, withheld.value

    fake, latch, creds, withheld = run(scenario())
    assert fake.failed_total == 1 and len(fake.ws_connects) == 1, "the second session never connects"
    assert withheld.reason == "latched"
    run(latch.sync(creds))
    summary = latch.summary(creds)
    assert summary["latched"] is True and summary["failures"]["orchestrator_registry"]["count"] == 1


def test_a_403_upgrade_is_a_ban_not_a_failed_sign_in():
    async def scenario():
        fake, latch, creds = fresh()
        fake.banned.add("A")
        with pytest.raises(ha_ws.HABanned) as banned:
            async with await open_session(latch, creds, fake):
                pass
        await latch.sync(creds)
        return fake, latch, creds, banned.value

    fake, latch, creds, banned = run(scenario())
    summary = latch.summary(creds)
    assert summary["latched"] is False and summary["failures"] == {} and summary["banned_at"] is not None
    assert fake.failed_total == 0
    assert str(banned) == "Home Assistant has blocked this address"


def test_exception_messages_are_fixed_strings_with_no_url_or_token():
    class Leaky(Exception):
        pass

    def connect(url, **kwargs):
        raise InvalidStatus(502)

    async def scenario():
        _, latch, creds = fresh()
        with pytest.raises(InvalidStatus):
            async with ha_ws.ha_ws_session(creds, latch, caller="admin_ws", profile=ha_ws.PIPELINES, connect=connect):
                pass
        with pytest.raises(ha_ws.HomeAssistantNotConfiguredError) as unconfigured:
            async with ha_ws.ha_ws_session(HACredentials(), latch, caller="admin_ws", profile=ha_ws.PIPELINES, connect=connect):
                pass
        return str(unconfigured.value), str(ha_ws.HAWithheld("latched")), str(ha_ws.HABanned()), str(ha_ws.ForbiddenFrameType("frame type not allowed"))

    for text in run(scenario()):
        assert URL not in text and TOKEN not in text and "ha-fixture" not in text


# -- the token proof ---------------------------------------------------------------------------------


def test_the_proof_via_the_websocket_resets_the_count_and_closes():
    async def scenario():
        fake = FakeHA()
        fake.counts["A"] = 3
        return fake, await ha_ws.prove_token(credentials(), connect=fake.ws_connect())

    fake, result = run(scenario())
    assert (result.outcome, result.auth_sent, result.latch_result) == ("ok_ws", True, "proof_ok_ws")
    assert fake.count("A") == 0


def test_a_refused_websocket_falls_back_to_get_api():
    def refused(url, **kw):
        raise ConnectionRefusedError()

    async def scenario():
        seen = []

        async def rest_get():
            seen.append("GET /api")
            return 200

        return seen, await ha_ws.prove_token(credentials(), connect=refused, rest_get=rest_get)

    seen, result = run(scenario())
    assert seen == ["GET /api"] and (result.outcome, result.latch_result) == ("ok_rest", "proof_ok_rest")


def test_an_upgrade_that_a_proxy_refuses_also_falls_back():
    def proxy(url, **kw):
        raise InvalidStatus(502)

    async def scenario():
        async def rest_get():
            return 200

        return await ha_ws.prove_token(credentials(), connect=proxy, rest_get=rest_get)

    assert run(scenario()).outcome == "ok_rest"


@pytest.mark.parametrize("status,outcome,latch_result", [
    (401, "rejected", "auth_rejected"), (403, "banned", "banned"), (500, "unreachable", "unreachable_sent"),
])
def test_the_rest_fallback_classifies_the_status(status, outcome, latch_result):
    def refused(url, **kw):
        raise ConnectionRefusedError()

    async def scenario():
        async def rest_get():
            return status

        return await ha_ws.prove_token(credentials(), connect=refused, rest_get=rest_get)

    result = run(scenario())
    assert (result.outcome, result.latch_result) == (outcome, latch_result)


def test_an_auth_invalid_is_rejected_and_a_403_upgrade_is_banned():
    async def scenario():
        wrong = FakeHA(valid=())
        banned = FakeHA()
        banned.banned.add("A")
        return await ha_ws.prove_token(credentials(), connect=wrong.ws_connect()), await ha_ws.prove_token(credentials(), connect=banned.ws_connect())

    rejected, banned = run(scenario())
    assert (rejected.outcome, rejected.auth_sent) == ("rejected", True)
    assert banned.outcome == "banned"


def test_a_connect_that_never_completes_refunds_and_a_silent_server_after_auth_does_not(monkeypatch):
    monkeypatch.setattr(ha_auth, "PROOF_TIMEOUT", 0.8)
    monkeypatch.setattr(ha_ws, "WS_CONNECT_TIMEOUT", 0.2)
    monkeypatch.setattr(ha_ws, "WS_AUTH_TIMEOUT", 0.2)
    monkeypatch.setattr(ha_ws, "REST_FALLBACK_TIMEOUT", 0.2)

    class NeverConnects:
        def __call__(self, url, **kw):
            return self

        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *exc):
            return False

    async def never_connects():
        raise httpx.ConnectTimeout("connect timed out")

    async def never_answers():
        await asyncio.Event().wait()

    async def scenario():
        started = time.monotonic()
        silent = HangingConnection()
        sent = await ha_ws.prove_token(credentials(), connect=silent)
        unsent = await ha_ws.prove_token(credentials(), connect=NeverConnects(), rest_get=never_connects)
        maybe = await ha_ws.prove_token(credentials(), connect=NeverConnects(), rest_get=never_answers)
        return sent, unsent, maybe, silent, time.monotonic() - started

    sent, unsent, maybe, silent, elapsed = run(scenario())
    assert (sent.outcome, sent.auth_sent, sent.latch_result) == ("unreachable", True, "unreachable_sent")
    assert (unsent.outcome, unsent.auth_sent, unsent.latch_result) == ("unreachable", False, "unreachable_unsent")
    assert maybe.auth_sent is True, "a request that went out and never answered may have been counted"
    assert [f["type"] for f in silent.sent] == ["auth"]
    assert elapsed < 3 * (ha_auth.PROOF_TIMEOUT + 0.5)


def test_the_whole_proof_is_bounded_by_the_proof_timeout(monkeypatch):
    monkeypatch.setattr(ha_auth, "PROOF_TIMEOUT", 0.3)
    monkeypatch.setattr(ha_ws, "WS_CONNECT_TIMEOUT", 5)
    monkeypatch.setattr(ha_ws, "WS_AUTH_TIMEOUT", 5)

    async def scenario():
        started = time.monotonic()
        result = await ha_ws.prove_token(credentials(), connect=HangingConnection())
        return result, time.monotonic() - started

    result, elapsed = run(scenario())
    assert result.outcome == "unreachable" and result.auth_sent is True
    assert elapsed < ha_auth.PROOF_TIMEOUT + 0.5


def test_the_inner_bounds_fit_inside_the_proof_timeout():
    assert ha_ws.WS_CONNECT_TIMEOUT + ha_ws.WS_AUTH_TIMEOUT + ha_ws.REST_FALLBACK_TIMEOUT <= ha_auth.PROOF_TIMEOUT


def test_a_withheld_session_never_connects():
    async def scenario():
        fake, latch, creds = fresh(valid=())
        with pytest.raises(ha_ws.AuthInvalid):
            async with await open_session(latch, creds, fake):
                pass
        connects = len(fake.ws_connects)
        with pytest.raises(ha_ws.HAWithheld):
            async with await open_session(latch, creds, fake):
                pass
        return connects, len(fake.ws_connects)

    before, after = run(scenario())
    assert before == after == 1


def test_connect_kwargs_match_the_registry_adapter():
    kwargs = ha_ws.connect_kwargs("wss://ha.example.invalid/api/websocket")
    assert kwargs["ssl"] is not None and kwargs["ssl"].verify_mode.name == "CERT_NONE"
    assert (kwargs["open_timeout"], kwargs["close_timeout"], kwargs["ping_interval"]) == (3, 2, None)
    assert kwargs["max_size"] == 32 * 1024 * 1024 and kwargs["logger"].disabled is True
    assert ha_ws.connect_kwargs("ws://ha.example.invalid/api/websocket")["ssl"] is None
