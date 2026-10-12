"""A Home Assistant stack for admin-backend tests: the real resolver, latch and
channel over the repository's fake Home Assistant (``tests/unit/ha_auth_fakes``),
installed into ``app.services.ha_access`` so the routes under test talk to it.

Not a test file. Nothing here opens a socket: the REST transport and the
WebSocket connector are the fake's, and "DNS" is a fixed answer.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
from typing import Any, Callable, Optional

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _load_fakes():
    name = "ha_auth_fakes_for_admin_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / "unit" / "ha_auth_fakes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fakes = _load_fakes()
Clock, FakeHA, TOKEN, URL, SERVICE_KEY = fakes.Clock, fakes.FakeHA, fakes.TOKEN, fakes.URL, fakes.SERVICE_KEY


async def _resolves(host, port, **kwargs):
    return [(2, 1, 6, "", ("192.0.2.7", 0))]


class Stack:
    def __init__(self, fake, resolver, latch, channel, redis, clock):
        self.fake, self.resolver, self.latch, self.channel, self.redis, self.clock = fake, resolver, latch, channel, redis, clock


def build(fake, source: Callable[[], Any], *, env: Optional[dict] = None, gate_wait: float = 0.2) -> Stack:
    """Build the stack (the resolver has done its first read) and install it."""
    from app.services import ha_access
    from shared import ha_auth
    from shared.ha_credentials import HACredentialResolver
    from shared.ha_http import HAChannel

    clock = Clock()
    redis = ha_auth.MemoryRedis(clock=clock)

    async def make():
        resolver = HACredentialResolver(source, service_api_key=SERVICE_KEY, env=env or {}, getaddrinfo=_resolves)
        await resolver.start()
        latch = ha_auth.HAAuthLatch(redis, instance="admin-1", service_api_key=SERVICE_KEY, wall=clock, gate_wait=gate_wait)
        channel = HAChannel(resolver, latch, caller="admin_rest", inner=fake.rest_transport("X"), ws_connect=fake.ws_connect("X"))
        return resolver, latch, channel

    resolver, latch, channel = asyncio.run(make())
    ha_access.install_for_test(resolver, latch, channel, fake.ws_connect("X"))
    return Stack(fake, resolver, latch, channel, redis, clock)


def constant_source(url: str = URL, token: str = TOKEN):
    from shared.ha_credentials import KeyLookup

    async def source():
        return KeyLookup.found(url, token)

    return source


def absent_source():
    from shared.ha_credentials import KeyLookup

    async def source():
        return KeyLookup.absent()

    return source
