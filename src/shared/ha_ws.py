"""The one Home Assistant WebSocket handshake, and the frames it may send.

Every WebSocket conversation with Home Assistant goes through
``ha_ws_session``. It takes the credentials snapshot and the process's
``HAAuthLatch``, asks the latch whether to connect, performs the ``auth``
exchange (the only place an ``access_token`` frame is built), settles the
outcome with the latch, and then lets the caller send only the frame types of
its named ``Profile``.

``prove_token`` is the dedicated token proof the latch's gate and probes use:
a handshake that sends ``auth`` and closes on ``auth_ok`` (the only success that
resets Home Assistant's failed sign-in count), with a ``GET /api`` fallback
when the WebSocket can't connect for a reason that isn't about the token.

Exception messages are fixed strings. A library exception is inspected for its
status code and never printed: it can carry the URL.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional

import httpx

from shared import ha_auth
from shared.ha_credentials import WS_PATH

WS_CONNECT_TIMEOUT = 3
WS_AUTH_TIMEOUT = 2
REST_FALLBACK_TIMEOUT = 2
WS_CLOSE_TIMEOUT = 2
WS_MAX_SIZE = 32 * 1024 * 1024
COMMAND_TIMEOUT = 5.0


class AuthInvalid(Exception):
    """Home Assistant answered ``auth_invalid`` to the token."""


class RegistryFetchError(Exception):
    """A protocol-level failure (unexpected frame, ``success: false``)."""


class ForbiddenFrameType(Exception):
    """An attempt to send a frame outside the session's profile."""


class HABanned(Exception):
    """Home Assistant answered 403 to the handshake: this address is blocked."""

    def __init__(self) -> None:
        super().__init__("Home Assistant has blocked this address")


class HAWithheld(Exception):
    """The latch decided not to send. ``reason`` is ``latched``, ``admin_only``,
    ``budget`` or ``gated``."""

    def __init__(self, reason: str) -> None:
        super().__init__("Home Assistant request withheld")
        self.reason = reason


class HomeAssistantNotConfiguredError(Exception):
    """Raised when Home Assistant is not configured but a method is called."""


@dataclass(frozen=True)
class Profile:
    """The frame types a session may send, besides ``auth`` (which is internal)."""

    name: str
    types: frozenset


REGISTRY_READ = Profile("registry_read", frozenset({
    "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list",
}))
PIPELINES = Profile("pipelines", frozenset({
    "assist_pipeline/pipeline/list", "assist_pipeline/pipeline/set_preferred",
}))
PROOF = Profile("proof", frozenset())
PROFILES = (REGISTRY_READ, PIPELINES, PROOF)


@dataclass(frozen=True)
class ProofResult:
    """``outcome``: ``ok_ws``, ``ok_rest``, ``rejected``, ``banned`` or
    ``unreachable``. ``auth_sent`` is whether Home Assistant may have counted an
    attempt, which decides whether the latch refunds the reservation."""

    outcome: str
    auth_sent: bool

    @property
    def latch_result(self) -> str:
        return {
            "ok_ws": "proof_ok_ws",
            "ok_rest": "proof_ok_rest",
            "rejected": "auth_rejected",
            "banned": "banned",
        }.get(self.outcome) or ("unreachable_sent" if self.auth_sent else "unreachable_unsent")


def _silent_logger() -> logging.Logger:
    quiet = logging.getLogger("athena.ha_ws")
    quiet.disabled = True
    quiet.propagate = False
    return quiet


def connect_kwargs(url: str) -> Dict[str, Any]:
    """TLS verification is off for ``wss://`` (self-signed certificates, as for
    every Home Assistant client here), and the library's frame logging is
    silenced: the auth frame carries the token."""
    ssl_context: Optional[ssl.SSLContext] = None
    if url.startswith("wss://"):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    return {
        "ssl": ssl_context,
        "open_timeout": WS_CONNECT_TIMEOUT,
        "close_timeout": WS_CLOSE_TIMEOUT,
        "max_size": WS_MAX_SIZE,
        "ping_interval": None,
        "logger": _silent_logger(),
    }


def _default_connect(url: str, **kwargs: Any):
    import websockets

    return websockets.connect(url, **kwargs)


def _handshake_status(exc: BaseException) -> Optional[int]:
    """The HTTP status of a failed upgrade, from the exception's response and
    never its text (which can carry the URL)."""
    if type(exc).__name__ not in ("InvalidStatus", "InvalidStatusCode"):
        return None
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _contains_access_token(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "access_token" in value or any(_contains_access_token(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_access_token(v) for v in value)
    return False


class HAWebSocket:
    """The authenticated connection a session yields. ``command`` is the only
    way to send, and it sends only the session's profile."""

    def __init__(self, ws: Any, profile: Profile):
        self._ws = ws
        self._profile = profile
        self._next_id = 0

    def _check(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ForbiddenFrameType("frame must be an object")
        frame_type = payload.get("type")
        if not isinstance(frame_type, str) or frame_type == "auth" or frame_type not in self._profile.types:
            raise ForbiddenFrameType("frame type not allowed")
        if _contains_access_token(payload):
            raise ForbiddenFrameType("frame carries a token")
        if "id" in payload:
            raise ForbiddenFrameType("frame ids are assigned by the session")

    async def commands(self, payloads: List[Dict[str, Any]], *, timeout: float = COMMAND_TIMEOUT) -> List[Dict[str, Any]]:
        """Send several commands back to back and return their ``result``
        frames in order (one round trip, not one each). Every payload is checked
        before any is sent."""
        for payload in payloads:
            self._check(payload)
        ids = []
        for payload in payloads:
            self._next_id += 1
            ids.append(self._next_id)
            await self._ws.send(json.dumps({**payload, "id": self._next_id}))
        results: Dict[int, Dict[str, Any]] = {}
        async with asyncio.timeout(timeout):
            while len(results) < len(ids):
                frame = json.loads(await self._ws.recv())
                if frame.get("type") == "result" and frame.get("id") in ids:
                    results[frame["id"]] = frame
        return [results[i] for i in ids]

    async def command(self, payload: Dict[str, Any], *, timeout: float = COMMAND_TIMEOUT) -> Dict[str, Any]:
        """Send one command and return its ``result`` frame. Raises
        ``ForbiddenFrameType`` before any send for a frame outside the profile."""
        return (await self.commands([payload], timeout=timeout))[0]


async def _recv(ws: Any, timeout: float) -> Dict[str, Any]:
    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    return frame if isinstance(frame, dict) else {}


async def _handshake(ws: Any, token: str, sent: Dict[str, bool]) -> str:
    """The ``auth`` exchange. Returns ``auth_ok``; raises ``AuthInvalid`` for
    ``auth_invalid`` and ``RegistryFetchError`` for anything unexpected."""
    first = await _recv(ws, WS_AUTH_TIMEOUT)
    if first.get("type") != "auth_required":
        raise RegistryFetchError("unexpected first frame")
    sent["auth"] = True
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    verdict = (await _recv(ws, WS_AUTH_TIMEOUT)).get("type")
    if verdict == "auth_invalid":
        raise AuthInvalid()
    if verdict != "auth_ok":
        raise RegistryFetchError("unexpected auth reply")
    return "auth_ok"


def _classify_handshake_failure(exc: BaseException, sent: Dict[str, bool]) -> str:
    if isinstance(exc, AuthInvalid):
        return "auth_rejected"
    if _handshake_status(exc) == 403:
        return "banned"
    if isinstance(exc, asyncio.CancelledError) and not sent["auth"]:
        return "other"
    return "unreachable_sent" if sent["auth"] else "unreachable_unsent"


@contextlib.asynccontextmanager
async def ha_ws_session(
    snapshot: Any,
    latch: ha_auth.HAAuthLatch,
    *,
    caller: str,
    profile: Profile,
    connect: Optional[Callable[..., Any]] = None,
) -> AsyncIterator[HAWebSocket]:
    """Connect, authenticate and yield an ``HAWebSocket`` limited to
    ``profile``.

    The handshake is itself the proof: ``auth_ok`` clears Home Assistant's
    failed sign-in count and the latch's budget, ``auth_invalid`` trips the
    latch. Raises ``HomeAssistantNotConfiguredError``, ``HAWithheld`` (nothing
    was sent), ``AuthInvalid``, ``HABanned`` (a 403 upgrade, not a failed
    sign-in), ``RegistryFetchError``, or the connection's own ``OSError`` /
    ``TimeoutError``. Holds one in-flight slot for the session's lifetime.
    """
    if not any(profile is known for known in PROFILES):
        raise TypeError("profile must be one of the ha_ws profiles")
    if not snapshot.configured:
        raise HomeAssistantNotConfiguredError("Home Assistant is not configured")
    async with latch.slot():
        admission = await latch.admit(snapshot, method="WS", path=WS_PATH, key="ws")
        if admission.kind in (ha_auth.WITHHELD, ha_auth.GATED):
            raise HAWithheld(admission.reason or "gated")
        sent = {"auth": False}
        settled = False

        async def settle(result: str) -> None:
            nonlocal settled
            if not settled:
                settled = True
                await latch.observe(snapshot, admission, caller=caller, key="ws", result=result)

        connector = connect or _default_connect
        async with contextlib.AsyncExitStack() as stack:
            try:
                ws = await stack.enter_async_context(connector(snapshot.ws_url, **connect_kwargs(snapshot.ws_url)))
                await _handshake(ws, snapshot.token.reveal(), sent)
            except BaseException as exc:
                await asyncio.shield(settle(_classify_handshake_failure(exc, sent)))
                if isinstance(exc, (AuthInvalid, RegistryFetchError, asyncio.CancelledError)):
                    raise
                if _handshake_status(exc) == 403:
                    raise HABanned() from None
                raise
            await settle("proof_ok_ws")
            yield HAWebSocket(ws, profile)


RestGet = Callable[[], Awaitable[int]]


async def prove_token(
    snapshot: Any,
    *,
    connect: Optional[Callable[..., Any]] = None,
    rest_get: Optional[RestGet] = None,
    sent: Optional[Dict[str, bool]] = None,
) -> ProofResult:
    """Prove the snapshot's token with one failed-sign-in-safe exchange.

    The caller holds the latch's gate lease and reservation, and settles the
    result (``ProofResult.latch_result``). A WebSocket ``auth`` that gets
    ``auth_ok`` is the proof. When the WebSocket can't connect for a reason that
    isn't about the token (no upgrade through a proxy, a refused port), ``rest_get``
    does ``GET /api`` instead: it returns the HTTP status or raises. The whole
    proof is bounded by ``ha_auth.PROOF_TIMEOUT``. ``sent`` (``{"auth": False}``) is
    set to true once an ``auth`` frame or request has gone out, so a caller that
    is cancelled mid-proof still knows whether Home Assistant may have counted it.
    """
    sent = sent if sent is not None else {"auth": False}
    try:
        async with asyncio.timeout(ha_auth.PROOF_TIMEOUT):
            via_ws = await _prove_ws(snapshot, connect or _default_connect, sent)
            if via_ws is not None:
                return via_ws
            if rest_get is None:
                return ProofResult("unreachable", sent["auth"])
            return await _prove_rest(rest_get, sent)
    except TimeoutError:
        return ProofResult("unreachable", sent["auth"])


async def _prove_ws(snapshot: Any, connector: Callable[..., Any], sent: Dict[str, bool]) -> Optional[ProofResult]:
    """The WebSocket proof, or None when it never reached the point of sending
    ``auth`` (the caller then falls back to REST)."""
    try:
        async with contextlib.AsyncExitStack() as stack:
            ws = await asyncio.wait_for(
                stack.enter_async_context(connector(snapshot.ws_url, **connect_kwargs(snapshot.ws_url))),
                WS_CONNECT_TIMEOUT,
            )
            await _handshake(ws, snapshot.token.reveal(), sent)
            return ProofResult("ok_ws", True)
    except AuthInvalid:
        return ProofResult("rejected", True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _handshake_status(exc) == 403:
            return ProofResult("banned", False)
        if sent["auth"]:
            return ProofResult("unreachable", True)
        return None


async def _prove_rest(rest_get: RestGet, sent: Dict[str, bool]) -> ProofResult:
    before = sent["auth"]
    sent["auth"] = True  # the request may reach Home Assistant from here on, even if we're cancelled
    try:
        status = await asyncio.wait_for(rest_get(), REST_FALLBACK_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)):
            sent["auth"] = before  # it never connected
        return ProofResult("unreachable", sent["auth"])
    if 200 <= status < 300:
        return ProofResult("ok_rest", True)
    if status == 401:
        return ProofResult("rejected", True)
    if status == 403:
        return ProofResult("banned", False)
    return ProofResult("unreachable", True)
