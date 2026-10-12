"""admin-backend's access to Home Assistant: one credential resolver, one failed
sign-in gate, one REST channel and one WebSocket session door.

Every admin route that talks to Home Assistant goes through here. The address
and token come from the ``home-assistant`` External API Key (this process reads
its own table), with the deployment's environment as the fallback, re-read every minute
by ``shared.ha_credentials``, so a key saved in the admin panel applies to the
routes within a minute without a restart. The shared gate (``shared.ha_auth``)
keeps every request that could fail authentication within the cluster's budget.

A failure never carries Home Assistant's own text to the browser: ``http_error``
maps every failure to a closed phrase and ``error_text`` gives the same phrase for
a per-item result. Logs carry the exception class and a status code only.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Optional, Tuple

import httpx
import structlog
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models import ExternalAPIKey
from app.utils.encryption import decrypt_value
from shared import ha_auth, ha_ws
from shared.config import get_config
from shared.ha_credentials import (
    HA_KEY_SERVICE, AdminKeySource, HACredentialResolver, KeyLookup, resolve,
)
from shared.ha_http import HAChannel
from shared.ha_ws import (
    AuthInvalid, HABanned, HAWithheld, HomeAssistantNotConfiguredError,
)

logger = structlog.get_logger()

NOT_SET_UP = "Home Assistant isn't set up."
TOKEN_REJECTED = "Home Assistant rejected Athena's token."
REFUSED = "Home Assistant refused the request."
BLOCKED = "Home Assistant has blocked this address."
CANT_CONNECT = "Couldn't connect to Home Assistant."
TIMED_OUT = "Home Assistant didn't answer in time."
FAILED = "Home Assistant returned an error."
BUSY = "Home Assistant is busy. Try again in a moment."
BAD_REQUEST = "That isn't a valid Home Assistant request."

_resolver: Optional[HACredentialResolver] = None
_latch: Optional[ha_auth.HAAuthLatch] = None
_channel: Optional[HAChannel] = None
_ws_connect: Optional[Callable[..., Any]] = None


# -- the stored key -----------------------------------------------------------------------------------


def read_enabled_key(db: Session, service_name: str) -> Optional[Tuple[str, str]]:
    """``(api_key, endpoint_url)`` of an enabled External API Key, decrypted, or
    None. Unlike the public key routes it doesn't touch ``last_used``, so reading
    the credentials every minute doesn't rewrite the row."""
    key = db.query(ExternalAPIKey).filter_by(service_name=service_name, enabled=True).first()
    if key is None:
        return None
    return decrypt_value(key.api_key_encrypted), key.endpoint_url or ""


def lookup_from_db(db: Session) -> KeyLookup:
    """The ``home-assistant`` key as the resolver reads it. A row that can't be
    read or decrypted is ``unavailable`` (the last value stays), not ``absent``."""
    try:
        row = read_enabled_key(db, HA_KEY_SERVICE)
    except Exception as exc:
        logger.warning("ha_key_read_failed", error_class=type(exc).__name__)
        return KeyLookup.unavailable()
    return KeyLookup.absent() if row is None else KeyLookup.found(row[1], row[0])


def admin_db_source() -> AdminKeySource:
    """The resolver's source: this process's own database, off the event loop."""

    async def source() -> KeyLookup:
        def read() -> KeyLookup:
            with SessionLocal() as db:
                return lookup_from_db(db)

        try:
            return await run_in_threadpool(read)
        except Exception as exc:
            logger.warning("ha_key_read_failed", error_class=type(exc).__name__)
            return KeyLookup.unavailable()

    return source


# -- lifecycle ---------------------------------------------------------------------------------------------


async def init(
    redis: Any,
    *,
    inner: Optional[httpx.AsyncBaseTransport] = None,
    ws_connect: Optional[Callable[..., Any]] = None,
    getaddrinfo: Optional[Callable[..., Any]] = None,
) -> None:
    """Build this process's resolver, latch and channel (once; a second call
    replaces them). ``redis`` is the client the failed sign-in budget lives in,
    or None where there is none (``DEV_MODE``): the same rules then run in-process."""
    global _resolver, _latch, _channel, _ws_connect
    await close()
    service_key = get_config().service_api_key
    resolver = HACredentialResolver(admin_db_source(), service_api_key=service_key, getaddrinfo=getaddrinfo)
    await resolver.start()
    asyncio.create_task(resolver.run())  # long-lived; resolver.close() cancels it
    latch = ha_auth.HAAuthLatch(redis, instance=ha_auth.instance_id(), service_api_key=service_key)
    channel = HAChannel(resolver, latch, caller="admin_rest", timeout=10.0, inner=inner, ws_connect=ws_connect)
    _resolver, _latch, _channel, _ws_connect = resolver, latch, channel, ws_connect


async def close() -> None:
    global _resolver, _latch, _channel, _ws_connect
    channel, resolver = _channel, _resolver
    _resolver = _latch = _channel = _ws_connect = None
    if channel is not None:
        try:
            await channel.aclose()
        except Exception as exc:
            logger.warning("ha_channel_close_failed", error_class=type(exc).__name__)
    if resolver is not None:
        await resolver.close()


def install_for_test(resolver: Any, latch: ha_auth.HAAuthLatch, channel: HAChannel, ws_connect: Optional[Callable[..., Any]] = None) -> None:
    """Install a ready-made stack (tests): no task is started and no database is read."""
    global _resolver, _latch, _channel, _ws_connect
    _resolver, _latch, _channel, _ws_connect = resolver, latch, channel, ws_connect


def reset_for_test() -> None:
    """Close the channel, cancel the resolver's task and drop the singletons."""
    global _resolver, _latch, _channel, _ws_connect
    if _channel is not None or _resolver is not None:
        try:
            asyncio.run(close())
        except Exception:
            pass
    _resolver = _latch = _channel = _ws_connect = None


# -- the doors -----------------------------------------------------------------------------------------------


def channel() -> HAChannel:
    """The REST channel. Raises ``HomeAssistantNotConfiguredError`` before the
    stack exists, like a request with nothing configured."""
    if _channel is None:
        raise HomeAssistantNotConfiguredError("Home Assistant is not configured")
    return _channel


def credentials() -> HACredentialResolver:
    if _resolver is None:
        raise HomeAssistantNotConfiguredError("Home Assistant is not configured")
    return _resolver


def ws_session(profile: ha_ws.Profile):
    """A WebSocket session for the voice-pipeline routes: ``profile`` must be
    ``ha_ws.PIPELINES``."""
    if profile is not ha_ws.PIPELINES:
        raise TypeError("admin-backend uses the pipelines profile only")
    resolver = credentials()
    if _latch is None:
        raise HomeAssistantNotConfiguredError("Home Assistant is not configured")
    return ha_ws.ha_ws_session(resolver.current(), _latch, caller="admin_ws", profile=profile, connect=_ws_connect)


async def key_saved(row: ExternalAPIKey) -> None:
    """The operator saved the ``home-assistant`` key (created or updated it).

    Forget the stop for the saved credentials and let exactly one gated attempt
    through (``HAAuthLatch.clear_and_reopen``), then re-read the key now rather
    than at the next minute. Saving the same key again is how a stopped token is
    retried after fixing things on Home Assistant's side. Never raises."""
    if _latch is None or _resolver is None:
        return
    try:
        saved = resolve(
            KeyLookup.found(row.endpoint_url or "", decrypt_value(row.api_key_encrypted)),
            os.environ, previous=None, service_api_key=get_config().service_api_key,
        )
        if saved.digest:
            await _latch.clear_and_reopen(saved)
        await _resolver.refresh(force=True)
    except Exception as exc:
        logger.warning("ha_key_saved_failed", error_class=type(exc).__name__)


# -- failures ------------------------------------------------------------------------------------------------------


def _from_response(response: httpx.Response) -> Tuple[int, str]:
    withheld = response.extensions.get("athena_ha_withheld")
    if withheld == "gated":
        return 503, BUSY
    if withheld == "admin_only":
        return 502, REFUSED
    if withheld in ("latched", "budget") or response.status_code == 401:
        return 502, TOKEN_REJECTED
    if response.status_code == 403:
        return 502, REFUSED
    return 502, FAILED


def _classify(failure: Any) -> Tuple[int, str]:
    if isinstance(failure, httpx.Response):
        return _from_response(failure)
    if isinstance(failure, HomeAssistantNotConfiguredError):
        return 409, NOT_SET_UP
    if isinstance(failure, HAWithheld):
        return {"gated": (503, BUSY), "admin_only": (502, REFUSED)}.get(failure.reason, (502, TOKEN_REJECTED))
    if isinstance(failure, AuthInvalid):
        return 502, TOKEN_REJECTED
    if isinstance(failure, HABanned):
        return 502, BLOCKED
    if isinstance(failure, httpx.HTTPStatusError):
        return _from_response(failure.response)
    if isinstance(failure, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return 504, TIMED_OUT
    if isinstance(failure, (httpx.ConnectError, ConnectionError, OSError)):
        return 502, CANT_CONNECT
    if isinstance(failure, ValueError):
        return 400, BAD_REQUEST
    return 502, FAILED


def error_text(failure: Any) -> str:
    """The closed phrase for an exception or a response (a per-item result)."""
    return _classify(failure)[1]


def http_error(failure: Any) -> HTTPException:
    """The one mapping from a Home Assistant failure to an HTTP error. The detail
    never comes from the exception; the log carries its class and a status code."""
    status, detail = _classify(failure)
    logger.warning(
        "ha_request_failed",
        error_class=type(failure).__name__,
        status_code=getattr(failure, "status_code", None) or getattr(getattr(failure, "response", None), "status_code", None),
    )
    return HTTPException(status_code=status, detail=detail)
