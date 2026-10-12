"""Neither the Home Assistant token nor its host leaves the credential path.

One run over the shared stack: the admin-key lookup, the resolver, the channel,
the latch and its Redis, a WebSocket session, the area registry and the
exceptions they raise. The fixture token and host must appear in no log record
(structlog or stdlib), no Redis key or value, no exception message and no
``repr`` of the credentials, the channel, the latch or an admission.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import structlog

from .ha_auth_fakes import Clock, FakeHA, HOST, TOKEN, URL, credentials, make_stack

SECRETS = (TOKEN, HOST)


def run(coro):
    return asyncio.run(coro)


def assert_clean(text: str, where: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {where}"


def test_the_admin_key_lookup_does_not_log_the_endpoint_url(caplog):
    from shared.admin_config import AdminConfigClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"api_key": TOKEN, "endpoint_url": URL, "rate_limit_per_minute": 60})

    async def scenario():
        client = AdminConfigClient(admin_url="http://admin.invalid", api_key="service-key-for-tests")
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        data = await client.get_external_api_key("home-assistant")
        await client.client.aclose()
        return data

    with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
        data = run(scenario())
    assert data["endpoint_url"] == URL
    assert_clean(json.dumps(logs, default=str) + app_log_text(caplog), "the admin key lookup")


def app_log_text(caplog) -> str:
    """The application's stdlib records. httpx's own INFO lines name the URL;
    production keeps that logger at WARNING (``configure_logging``)."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore")))


def test_a_full_stack_run_leaks_nothing(caplog):
    from shared import ha_auth, ha_ws
    from shared.ha_credentials import HACredentialResolver, KeyLookup

    async def scenario():
        clock = Clock()
        fake = FakeHA(valid=(), threshold=None)  # a rejected token exercises every failure path
        redis = ha_auth.MemoryRedis(clock=clock)
        stack = make_stack(fake=fake, redis=redis, clock=clock, gate_wait=0.1)
        texts = []

        async def source():
            return KeyLookup.found(URL, TOKEN)

        async def resolves(host, port, **kw):
            return [(2, 1, 6, "", ("192.0.2.7", 0))]

        resolver = HACredentialResolver(source, service_api_key="service-key-for-tests", env={}, getaddrinfo=resolves)
        await resolver.start()
        texts += [repr(resolver.current()), str(resolver.current()), repr(stack.channel), repr(stack.latch)]
        for _ in range(3):
            response = await stack.channel.request("GET", "/api/states")
            texts += [repr(response), repr(response.extensions)]
        try:
            async with ha_ws.ha_ws_session(stack.creds, stack.latch, caller="admin_ws", profile=ha_ws.PIPELINES, connect=fake.ws_connect()):
                pass
        except Exception as exc:
            texts.append(f"{type(exc).__name__}: {exc}")
        for bad in ("/api/../x", "/api/states?x"):
            try:
                await stack.channel.request("GET", bad)
            except ValueError as exc:
                texts.append(str(exc))
        admission = await stack.latch.admit(stack.creds, method="GET", path="/api/states", key="GET /api/states")
        texts.append(repr(admission))
        clock.advance(121)
        await stack.channel.request("GET", "/api/states")
        await stack.latch.sync(stack.creds)
        texts.append(json.dumps(stack.latch.summary(stack.creds)))
        stored = []
        for key in list(redis._data):
            stored.append(key)
            stored.append(json.dumps(redis._data[key], default=lambda v: sorted(v)))
        for key, deadline in redis._expiry.items():
            stored.append(key)
        await stack.channel.aclose()
        return texts, stored

    with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
        texts, stored = run(scenario())
    assert logs, "the run must have logged something for this check to mean anything"
    assert_clean("\n".join(texts), "an exception message or a repr")
    assert_clean("\n".join(stored), "a Redis key or value")
    assert_clean(json.dumps(logs, default=str) + app_log_text(caplog), "a log record")


def test_a_registry_session_failure_text_is_fixed(caplog):
    from shared import ha_ws
    from shared.ha_registry import HARegistry

    async def scenario():
        clock = Clock()
        fake = FakeHA(valid=())
        stack = make_stack(fake=fake, clock=clock, gate_wait=0.1)

        async def fetcher():
            async with ha_ws.ha_ws_session(stack.creds, stack.latch, caller="orchestrator_registry", profile=ha_ws.REGISTRY_READ, connect=fake.ws_connect()):
                return {}

        registry = HARegistry(fetcher, min_refresh_interval=0)
        await registry.ensure(1.0)
        return registry.link_summary()

    with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
        summary = run(scenario())
    assert_clean(json.dumps(summary) + json.dumps(logs, default=str) + app_log_text(caplog), "the registry failure")


MARKER = "private-message-body-4417"


def test_call_service_and_create_automation_log_no_payload_and_no_exception_text(caplog):
    """The client's logs carry key names, the error class and a status code, never
    the service data, an automation's config, or an exception message (which
    names the address)."""
    from shared.ha_client import HomeAssistantClient

    async def scenario(valid):
        stack = make_stack(fake=FakeHA(valid=valid, threshold=None), gate_wait=0.1)
        client = HomeAssistantClient(stack.channel)
        outcomes = []
        try:
            outcomes.append(await client.call_service("notify", "mobile_app", {"message": MARKER, "title": MARKER}))
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        outcomes.append(await client.create_automation("auto_1", {"alias": MARKER, "trigger": [], "action": [{"message": MARKER}]}))
        await stack.channel.aclose()
        return outcomes

    with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
        run(scenario((TOKEN,)))
        run(scenario(()))
    text = json.dumps(logs, default=str) + app_log_text(caplog)
    assert logs, "the calls must have logged"
    for secret in (MARKER, TOKEN, HOST):
        assert secret not in text, f"{secret!r} leaked in a log record"
    events = {entry["event"] for entry in logs}
    assert {"ha_call_service_request", "ha_call_service_error"} <= events
    request = next(entry for entry in logs if entry["event"] == "ha_call_service_request")
    assert request["data_keys"] == ["message", "title"]
    error = next(entry for entry in logs if entry["event"] == "ha_call_service_error")
    assert error["error_class"] and "error" not in error
