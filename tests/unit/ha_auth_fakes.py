"""Helpers for the Home Assistant auth tests: a controllable clock, a Redis that
can fail, and a fake Home Assistant that implements the parts of ``ban.py`` and
``websocket_api/auth.py`` the failed sign-in budget depends on.

Not a test file. ``MemoryRedis`` (with the Python mirror of each script) lives
in ``shared.ha_auth``, because the no-Redis state is the same code; the
real-Redis tier runs the scenario table against both.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import httpx

# The shared modules are imported inside the helpers that need them, so this file
# imports (and FakeHA runs) in the base tree, where the new modules don't exist yet.

TOKEN = "sekret-token-123"
HOST = "ha-fixture.invalid"
URL = f"https://{HOST}"
SERVICE_KEY = "service-key-for-tests"


class Clock:
    """One controllable time for ``wall`` and ``monotonic``."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def credentials(token: str = TOKEN, url: str = URL, *, service_api_key: Optional[str] = SERVICE_KEY) -> "HACredentials":
    from shared.ha_credentials import KeyLookup, resolve

    return resolve(KeyLookup.found(url, token), {}, previous=None, service_api_key=service_api_key)


class RedisDown(Exception):
    pass


class FlakyRedis:
    """Wraps a MemoryRedis. ``down`` makes every call raise; ``hang`` makes every
    call never complete; ``calls`` counts reads and scripts by kind."""

    def __init__(self, inner):
        self._inner = inner
        self.down = False
        self.hang = False

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target) or name in ("register_script", "pipeline", "bytes_view"):
            return target

        async def call(*args, **kwargs):
            await self._gate()
            return await target(*args, **kwargs)

        return call

    async def _gate(self):
        if self.down:
            raise RedisDown()
        if self.hang:
            await asyncio.Event().wait()

    def pipeline(self, transaction: bool = True):
        return _FlakyPipeline(self, self._inner.pipeline(transaction=transaction))

    def register_script(self, source: str):
        script = self._inner.register_script(source)

        async def run(keys=(), args=()):
            await self._gate()
            return await script(keys=keys, args=args)

        return run


class _FlakyPipeline:
    def __init__(self, owner: FlakyRedis, inner):
        self._owner = owner
        self._inner = inner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        target = getattr(self._inner, name)

        def queue(*args, **kwargs):
            target(*args, **kwargs)
            return self

        return queue

    async def execute(self):
        await self._owner._gate()
        return await self._inner.execute()


def reads(redis) -> int:
    """How many pipeline reads a MemoryRedis (or a FlakyRedis over one) served."""
    inner = getattr(redis, "_inner", redis)
    return inner.calls.count("pipeline")


class InvalidStatus(Exception):
    """Named like the websockets exception so the handshake status is read from
    ``response.status_code`` and never from the message."""

    def __init__(self, status_code: int):
        super().__init__("upgrade refused")
        self.response = SimpleNamespace(status_code=status_code)


class FakeHA:
    """Home Assistant's failed sign-in rules, per source address.

    - every 401 (REST) and every ``auth_invalid`` (WebSocket) adds 1 to the
      address's count, with no decay;
    - only a WebSocket ``auth_ok`` resets the count (a REST 2xx does not);
    - at ``threshold`` the address is banned: REST answers 403 and the upgrade
      fails with 403 (``threshold=None`` never bans);
    - a ``valid`` token reads everything; an ``admin_only_paths`` entry answers
      401 to a token in ``non_admin``.
    """

    def __init__(
        self, *, valid=(TOKEN,), non_admin=(), admin_only_paths=(), threshold: Optional[int] = 5,
        delay: float = 0.0, auth_reply_delay: float = 0.0,
    ):
        self.valid = set(valid)
        self.non_admin = set(non_admin)
        self.admin_only_paths = set(admin_only_paths)
        self.threshold = threshold
        self.delay = delay
        self.auth_reply_delay = auth_reply_delay
        self.max_count = 0
        self.counts: Dict[str, int] = {}
        self.failed_total = 0
        self.banned: set = set()
        self.rest_sent: List[tuple] = []
        self.ws_connects: List[str] = []
        self.ws_auth_frames = 0
        self.inflight = 0
        self.max_inflight = 0

    def revoke(self, token: str) -> None:
        self.valid.discard(token)

    def _fail(self, address: str) -> None:
        self.failed_total += 1
        self.counts[address] = self.counts.get(address, 0) + 1
        self.max_count = max(self.max_count, self.counts[address])
        if self.threshold is not None and self.counts[address] >= self.threshold:
            self.banned.add(address)

    def count(self, address: str = "A") -> int:
        return self.counts.get(address, 0)

    # -- REST -------------------------------------------------------------

    def rest_transport(self, address: str = "A") -> httpx.AsyncBaseTransport:
        outer = self

        class _Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                outer.inflight += 1
                outer.max_inflight = max(outer.max_inflight, outer.inflight)
                try:
                    if outer.delay:
                        await asyncio.sleep(outer.delay)
                    return outer._rest(request, address)
                finally:
                    outer.inflight -= 1

        return _Transport()

    def _rest(self, request: httpx.Request, address: str) -> httpx.Response:
        self.rest_sent.append((address, request.method, request.url.path))
        if address in self.banned:
            return httpx.Response(403, request=request)
        auth = request.headers.get("authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if token not in self.valid or (token in self.non_admin and request.url.path in self.admin_only_paths):
            self._fail(address)
            return httpx.Response(401, request=request)
        return httpx.Response(200, json=[], request=request)

    # -- WebSocket ----------------------------------------------------------

    def ws_connect(self, address: str = "A") -> Callable[..., Any]:
        outer = self

        def connect(url: str, **kwargs: Any):
            return _FakeConnection(outer, address)

        return connect


class _FakeConnection:
    def __init__(self, ha: FakeHA, address: str):
        self._ha = ha
        self._address = address
        self._frames: asyncio.Queue = asyncio.Queue()
        self.sent: List[dict] = []

    async def __aenter__(self):
        ha = self._ha
        ha.ws_connects.append(self._address)
        if ha.delay:
            await asyncio.sleep(ha.delay)
        if self._address in ha.banned:
            raise InvalidStatus(403)
        self._frames.put_nowait({"type": "auth_required"})
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        ha = self._ha
        if frame.get("type") == "auth":
            ha.ws_auth_frames += 1
            if frame.get("access_token") in ha.valid:
                ha.counts.pop(self._address, None)
                reply = {"type": "auth_ok"}
            else:
                ha._fail(self._address)  # counted the moment the frame arrives
                reply = {"type": "auth_invalid"}
            if ha.auth_reply_delay:
                await asyncio.sleep(ha.auth_reply_delay)
            self._frames.put_nowait(reply)
            return
        self._frames.put_nowait({"type": "result", "id": frame.get("id"), "success": True, "result": []})

    async def recv(self) -> str:
        return json.dumps(await self._frames.get())


class HangingConnection:
    """A connection that greets with ``auth_required`` and then never answers
    (for the proof bound)."""

    def __init__(self):
        self.sent: List[dict] = []
        self._greeted = False

    def __call__(self, url: str, **kwargs: Any):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._greeted:
            self._greeted = True
            return json.dumps({"type": "auth_required"})
        await asyncio.Event().wait()


def resolver_for(creds):
    from shared.ha_credentials import StaticResolver

    return StaticResolver(creds)


def make_latch(redis, *, clock: Optional[Clock] = None, instance: str = "pod-a", **kw):
    from shared import ha_auth

    clock = clock or Clock()
    return ha_auth.HAAuthLatch(redis, instance=instance, service_api_key=SERVICE_KEY, wall=clock, **kw)


def make_stack(
    *,
    fake: Optional[FakeHA] = None,
    token: str = TOKEN,
    address: str = "A",
    redis=None,
    clock: Optional[Clock] = None,
    instance: str = "pod-a",
    caller: str = "orchestrator_rest",
    monitor=None,
    timeout: float = 5.0,
    **latch_kw,
) -> SimpleNamespace:
    """A real ``HAChannel`` over a real ``HAAuthLatch`` for one process, talking
    to ``fake``. Several stacks share a ``redis`` to model several processes."""
    from shared import ha_auth
    from shared.ha_credentials import StaticResolver
    from shared.ha_http import HAChannel

    clock = clock or Clock()
    fake = fake or FakeHA()
    redis = redis if redis is not None else ha_auth.MemoryRedis(clock=clock)
    creds = credentials(token)
    resolver = StaticResolver(creds)
    latch = make_latch(redis, clock=clock, instance=instance, **latch_kw)
    channel = HAChannel(
        resolver, latch, caller=caller, monitor=monitor, timeout=timeout,
        inner=fake.rest_transport(address), ws_connect=fake.ws_connect(address),
    )
    return SimpleNamespace(
        channel=channel, latch=latch, creds=creds, resolver=resolver, redis=redis, clock=clock, fake=fake,
        caller=caller, address=address,
    )
