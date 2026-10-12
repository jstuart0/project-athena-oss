"""The one authenticated Home Assistant REST path.

``HAChannel`` is what a service calls; ``HATransport`` is the one httpx
transport under it. The transport is the only place the ``Authorization``
header is built, and every request passes the process's ``HAAuthLatch`` first:
a request the latch won't admit is answered with a synthetic response and
nothing is sent.

- ``401`` with ``extensions["athena_ha_withheld"]`` of ``latched``,
  ``admin_only`` or ``budget``: a rejected token (or a service the token may
  not call) is not sent again. Callers see what a real 401 looks like.
- ``503`` with ``athena_ha_withheld == "gated"``: the gate, a canary or a
  reservation wait timed out, or Redis couldn't be read for an unproven digest
  or an unseen route. Callers see what a real 5xx looks like.

The request carries only a credentials digest (``extensions["athena_ha_digest"]``);
the token is looked up from the resolver when the request is sent, so it never
sits in a request object that could be logged.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable, Optional, Set

import httpx
import structlog

from shared import ha_auth, ha_ws
from shared.ha_credentials import HACredentials
from shared.ha_link import HALinkMonitor
from shared.ha_ws import HomeAssistantNotConfiguredError

logger = structlog.get_logger(__name__)

__all__ = ["HAChannel", "HATransport", "HomeAssistantNotConfiguredError", "WITHHELD_EXTENSION"]

WITHHELD_EXTENSION = "athena_ha_withheld"
DIGEST_EXTENSION = "athena_ha_digest"
PROOF_EXTENSION = "athena_ha_proof"
DEFAULT_TIMEOUT = 30.0

_PATH = re.compile(r"/api(/[A-Za-z0-9_.\-]+)*")
_ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
_SLUG = re.compile(r"[a-z0-9_]+")


def validate_path(path: Any) -> str:
    """The path if it can only name a Home Assistant API resource, else
    ``ValueError`` before anything is sent: no traversal, no encoding tricks, no
    query, and an entity id or service name that is exactly that."""
    if not isinstance(path, str) or not _PATH.fullmatch(path):
        raise ValueError("invalid Home Assistant API path")
    segments = path.split("/")[1:]  # "api", then the resource
    if any(segment in (".", "..") for segment in segments):
        raise ValueError("invalid Home Assistant API path")
    if len(segments) > 1 and segments[1] == "states" and len(segments) > 2:
        if len(segments) != 3 or not _ENTITY.fullmatch(segments[2]):
            raise ValueError("invalid Home Assistant entity id")
    elif len(segments) > 1 and segments[1] == "services" and len(segments) > 2:
        if len(segments) != 4 or not _SLUG.fullmatch(segments[2]) or not _SLUG.fullmatch(segments[3]):
            raise ValueError("invalid Home Assistant service")
    return path


def _origin_of(url: httpx.URL) -> str:
    """scheme://host:port in the form ``HACredentials.origin`` uses."""
    host = f"[{url.host}]" if ":" in url.host else url.host
    port = url.port or (443 if url.scheme == "https" else 80)
    return f"{url.scheme}://{host}:{port}"


def _bearer(snapshot: HACredentials, request: httpx.Request) -> str:
    """The Authorization value, only for a request addressed to the snapshot's own
    origin: the token is never sent anywhere else, whoever built the request."""
    if _origin_of(request.url) != snapshot.origin:
        raise RuntimeError("request is not addressed to the Home Assistant these credentials belong to")
    return f"Bearer {snapshot.token.reveal()}"


def _synthetic(request: httpx.Request, status: int, reason: str) -> httpx.Response:
    return httpx.Response(status, request=request, extensions={WITHHELD_EXTENSION: reason})


class HATransport(httpx.AsyncBaseTransport):
    """Admit, authenticate, send, record and settle one request."""

    def __init__(
        self,
        resolver: Any,
        latch: ha_auth.HAAuthLatch,
        *,
        caller: str,
        monitor: Optional[HALinkMonitor],
        inner: httpx.AsyncBaseTransport,
        ws_connect: Optional[Callable[..., Any]] = None,
    ):
        self._resolver = resolver
        self._latch = latch
        self._caller = caller
        self._monitor = monitor
        self._inner = inner
        self._ws_connect = ws_connect
        self._sent_since_withheld = False
        self._tasks: Set["asyncio.Task[None]"] = set()

    # -- background refresh ----------------------------------------------

    def _refresh_in_background(self) -> None:
        """A withheld request may mean the token was just replaced: ask the
        resolver to look again, once, after a real send."""
        if not self._sent_since_withheld:
            return
        self._sent_since_withheld = False
        task = asyncio.ensure_future(self._resolver.refresh(force=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_tasks(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:
                pass
        self._tasks.clear()

    # -- the request ------------------------------------------------------

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        digest = request.extensions.get(DIGEST_EXTENSION)
        snapshot = self._resolver.snapshot_for(digest) if isinstance(digest, str) else None
        if snapshot is None:
            raise RuntimeError("unknown Home Assistant credentials")
        if request.extensions.get(PROOF_EXTENSION):
            request.headers["Authorization"] = _bearer(snapshot, request)
            return await self._inner_send(request)
        method, path = request.method.upper(), request.url.path
        key = ha_auth.request_key(method, path, self._json_body(request))
        async with self._latch.slot():
            admission = await self._admit(snapshot, request, method, path, key)
            if isinstance(admission, httpx.Response):
                return admission
            result = "other"
            try:
                request.headers["Authorization"] = _bearer(snapshot, request)
                self._sent_since_withheld = True
                # From here the request may have reached Home Assistant and been counted: an error
                # that isn't a failure to connect, and a cancellation, keep a reservation spent
                # (as in ha_ws). Only something before the header is attached gives it back.
                result = "unreachable_sent"
                try:
                    response = await self._inner_send(request)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    result = "other"  # it never got there: the reservation is given back
                    raise
                result = self._result_for(request, response.status_code)
                return response
            finally:
                await self._latch.observe(snapshot, admission, caller=self._caller, key=key, result=result)

    @staticmethod
    def _json_body(request: httpx.Request) -> Any:
        try:
            return json.loads(request.content) if request.content else None
        except (ValueError, TypeError, httpx.RequestNotRead):
            return None

    async def _admit(self, snapshot: HACredentials, request: httpx.Request, method: str, path: str, key: str):
        """An admission that may be sent, or the synthetic response to return.
        A proof is run (once) when the gate says so."""
        for attempt in range(2):
            admission = await self._latch.admit(snapshot, method=method, path=path, key=key)
            if admission.kind == ha_auth.PROVE:
                if attempt == 1:
                    await self._latch.observe(snapshot, admission, caller=self._caller, key=key, result="other")
                    break
                if not await self._prove(snapshot, admission, key):
                    self._refresh_in_background()
                    return _synthetic(request, 401, ha_auth.REASON_LATCHED)
                continue
            if admission.kind in (ha_auth.OPEN, ha_auth.CANARY):
                return admission
            self._refresh_in_background()
            if admission.kind == ha_auth.WITHHELD:
                return _synthetic(request, 401, admission.reason or ha_auth.REASON_LATCHED)
            return _synthetic(request, 503, "gated")
        return _synthetic(request, 503, "gated")

    async def _prove(self, snapshot: HACredentials, admission: ha_auth.Admission, key: str) -> bool:
        sent = {"auth": False}
        result = "other"
        ok = False
        try:
            proof = await ha_ws.prove_token(
                snapshot, connect=self._ws_connect, rest_get=lambda: self._proof_status(snapshot), sent=sent,
            )
            result, ok = proof.latch_result, proof.outcome in ("ok_ws", "ok_rest")
        except BaseException:
            result = "unreachable_sent" if sent["auth"] else "other"
            raise
        finally:
            await self._latch.observe(snapshot, admission, caller=self._caller, key=key, result=result)
        return ok

    async def _proof_status(self, snapshot: HACredentials) -> int:
        request = httpx.Request("GET", snapshot.url + "/api", extensions={
            DIGEST_EXTENSION: snapshot.digest, PROOF_EXTENSION: True,
        })
        response = await self.handle_async_request(request)
        await response.aclose()
        return response.status_code

    async def _inner_send(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._inner.handle_async_request(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._monitor is not None:
                self._monitor.record_exception(exc)
            raise
        if self._monitor is not None:
            self._monitor.record_response(request.method, request.url.path, response.status_code)
        return response

    @staticmethod
    def _result_for(request: httpx.Request, status: int) -> str:
        if status == 401:
            return "auth_rejected"
        if status == 403 and ha_auth.is_token_proof(request.method, request.url.path):
            return "banned"
        return "passed"

    async def aclose(self) -> None:
        await self.cancel_tasks()
        await self._inner.aclose()


class HAChannel:
    """What a service calls to reach Home Assistant over REST.

    ``request`` takes one credentials snapshot, validates the path, and returns
    the ``httpx.Response`` (a synthetic one when the request was withheld; see
    the module docstring). It raises ``HomeAssistantNotConfiguredError`` before
    any send when Home Assistant isn't configured, and ``ValueError`` for a
    path that can't name an API resource.
    """

    def __init__(
        self,
        resolver: Any,
        latch: ha_auth.HAAuthLatch,
        *,
        caller: str,
        monitor: Optional[HALinkMonitor] = None,
        timeout: float = DEFAULT_TIMEOUT,
        inner: Optional[httpx.AsyncBaseTransport] = None,
        ws_connect: Optional[Callable[..., Any]] = None,
    ):
        self._resolver = resolver
        self._transport = HATransport(
            resolver, latch, caller=caller, monitor=monitor,
            inner=inner or httpx.AsyncHTTPTransport(verify=False), ws_connect=ws_connect,
        )
        self._client = httpx.AsyncClient(transport=self._transport, timeout=timeout, follow_redirects=False)

    @property
    def configured(self) -> bool:
        return self._resolver.current().configured

    async def request(
        self, method: str, path: str, *, json: Any = None, timeout: Optional[float] = None,
    ) -> httpx.Response:
        snapshot = self._resolver.current()
        if not snapshot.configured:
            raise HomeAssistantNotConfiguredError("Home Assistant is not configured")
        validate_path(path)
        built = self._client.build_request(
            method, snapshot.url + path, json=json,
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            extensions={DIGEST_EXTENSION: snapshot.digest},
        )
        return await self._client.send(built)

    async def aclose(self) -> None:
        await self._transport.cancel_tasks()
        await self._client.aclose()
