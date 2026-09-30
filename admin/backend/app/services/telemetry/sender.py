"""The telemetry loop: enablement, cadence, the cross-replica lease, identity,
and the POST.

One cycle, bounded by CYCLE_TIMEOUT_SECONDS:
  enablement and due check -> acquire the lease -> re-check both under it ->
  identity -> collect and build -> fence (renew) -> re-read the admin switch
  -> POST -> commit the outcome -> release.

State lives in ``system_settings`` rows under ``telemetry.*``. The local
install key is a secret: it's never sent, logged, stored in the payload or
returned by the API. The wire carries an HMAC of it bound to the endpoint's
origin, so a key presented to one collector is useless at another.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import os
import random
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

import httpx
import structlog
from sqlalchemy.exc import IntegrityError, OperationalError

import shared
from app.database import DATABASE_URL, SessionLocal
from app.models import IntentMetric, SystemSetting, User
from app.services import settings_lease
from app.services.telemetry import classify
from app.services.telemetry.collect import collect_facts
from app.services.telemetry.schema import SCHEMA_VERSION, SendState, build_payload, serialize
from shared.config import TELEMETRY_DEFAULT_ENDPOINT, read_telemetry_env

logger = structlog.get_logger()

LEASE_KEY = "telemetry.send.lock"
LEASE_TTL_SECONDS = 150
CYCLE_TIMEOUT_SECONDS = 90
POST_TIMEOUT_SECONDS = 10
RESPONSE_CAP_BYTES = 4096
SUCCESS_INTERVAL = timedelta(hours=23)
MAX_BACKOFF = timedelta(hours=24)
FIRST_CHECK_DELAY = (300, 360)
TICK_SECONDS = 3600
TICK_JITTER = 600
CATEGORY = "telemetry"

K_ID = "telemetry.installation_id"
K_KEY = "telemetry.install_key"
K_CREATED = "telemetry.installation_created_at"
K_PROVENANCE = "telemetry.provenance"
K_FIRST_BOOT = "telemetry.first_boot_sent_at"
K_ATTEMPT = "telemetry.last_attempt_at"
K_SUCCESS = "telemetry.last_success_at"
K_DUE = "telemetry.next_due_at"
K_FAILURES = "telemetry.consecutive_failures"
K_ERROR = "telemetry.last_error"
K_PAYLOAD = "telemetry.last_payload"
K_ADMIN_DISABLED = "telemetry.admin_disabled"
RESET_KEYS = (K_ID, K_KEY, K_CREATED, K_PROVENANCE, K_FIRST_BOOT, K_ATTEMPT, K_SUCCESS, K_DUE, K_FAILURES,
              K_ERROR, K_PAYLOAD)

# Module hooks (tests substitute these).
LEASE_SESSION_FACTORY = SessionLocal
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
EPHEMERAL_DB_CHECK: Callable[[], bool] = lambda: classify.db_is_ephemeral(DATABASE_URL)  # noqa: E731
CLOCK: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731
SLEEP = asyncio.sleep
RNG = random.Random()
LOADED_MODULES: Callable[[], Iterable[str]] = lambda: sys.modules  # noqa: E731
ENVIRON: Callable[[], Any] = lambda: os.environ  # noqa: E731
DOTENV_PATH = ".env"
DOTENV_READER = None
COLLECT = collect_facts
RESOLVER = None
_AFTER_DUE_CHECK: Optional[Callable[[], Any]] = None
_BEFORE_FENCE: Optional[Callable[[], Any]] = None
_BEFORE_ID_INSERT: Optional[Callable[[], Any]] = None

_loop_task: Optional[asyncio.Task] = None
_background: set = set()
_warned: set = set()

DOCS_LINK = "docs/CONFIGURATION.md#telemetry"


class _Abort(Exception):
    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


# ---------------------------------------------------------------------------
# system_settings helpers (every call site passes its own session)
# ---------------------------------------------------------------------------

def _get(session, key: str) -> Optional[str]:
    row = session.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row is not None else None


def _set(session, key: str, value: str) -> None:
    row = session.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        session.add(SystemSetting(key=key, value=value, category=CATEGORY))
    else:
        row.value = value


def _delete(session, keys: Iterable[str]) -> None:
    session.query(SystemSetting).filter(SystemSetting.key.in_(list(keys))).delete(synchronize_session=False)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _maybe_await(value) -> None:
    if inspect.isawaitable(value):
        await value


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------

def _warn_once(messages: Iterable[str]) -> None:
    for message in messages:
        if message not in _warned:
            _warned.add(message)
            logger.warning("telemetry_config_warning", message=message)


def _decide(session, *, warn: bool = False) -> Dict[str, Any]:
    reading = read_telemetry_env(environ=ENVIRON(), dotenv_path=DOTENV_PATH, reader=DOTENV_READER)
    decision = classify.parse_telemetry_env(reading, TELEMETRY_DEFAULT_ENDPOINT)
    if warn:
        _warn_once(decision.warnings)
    version, channel = classify.release_channel(shared.__version__)
    env = dict(ENVIRON())
    env["ATHENA_TELEMETRY_MODE"] = decision.mode or ""
    install_class = classify.install_class(env, channel, LOADED_MODULES())
    admin_disabled = _get(session, K_ADMIN_DISABLED) == "true"
    enabled, reason, env_locked = classify.enable_state(decision, install_class, EPHEMERAL_DB_CHECK(), admin_disabled)
    return {
        "enabled": enabled,
        "reason": reason,
        "env_locked": env_locked,
        "endpoint": decision.endpoint,
        "endpoint_display": decision.endpoint_display,
        "install_class": install_class,
        "version": version,
        "release_channel": channel,
    }


def _is_due(session, now: datetime) -> bool:
    due_at = _parse_time(_get(session, K_DUE))
    if due_at is not None and now < due_at:
        return False
    last_success = _parse_time(_get(session, K_SUCCESS))
    return last_success is None or now - last_success >= SUCCESS_INTERVAL


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _valid_key(value: Optional[str]) -> bool:
    if not value or len(value) != 43:
        return False
    try:
        return len(base64.urlsafe_b64decode(value + "=")) == 32
    except (ValueError, TypeError):
        return False


def _insert_if_absent(session, key: str, value: str) -> str:
    """Unique-key INSERT; on a lost race roll back (required on Postgres
    before the next statement) and return the stored value."""
    session.add(SystemSetting(key=key, value=value, category=CATEGORY))
    try:
        session.commit()
        return value
    except (IntegrityError, OperationalError):
        session.rollback()
    existing = _get(session, key)
    if existing is None:
        raise _Abort("error")
    return existing


def _provenance(session, now: datetime) -> str:
    cutoff = now - timedelta(hours=24)
    for column in (User.created_at, IntentMetric.created_at):
        row = session.query(column).filter(column.isnot(None)).order_by(column).first()
        if row is not None and _as_utc(row[0]) is not None and _as_utc(row[0]) < cutoff:
            return "upgraded"
    return "new"


def _ensure_identity(session, now: datetime) -> Tuple[str, str, str]:
    installation_id = _get(session, K_ID)
    install_key = _get(session, K_KEY)
    if installation_id and not _valid_key(install_key):
        logger.warning("telemetry_identity_regenerated",
                       message="The telemetry installation ID had no valid install key; a new identity was created")
        _delete(session, RESET_KEYS)
        session.commit()
        installation_id = None
    if not installation_id:
        if _BEFORE_ID_INSERT is not None:
            _BEFORE_ID_INSERT()
        installation_id = _insert_if_absent(session, K_ID, str(uuid.uuid4()))
        _insert_if_absent(session, K_CREATED, now.isoformat())
    install_key = _get(session, K_KEY)
    if not _valid_key(install_key):
        _delete(session, [K_KEY])
        session.commit()
        install_key = _insert_if_absent(
            session, K_KEY, base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode())
    provenance = _get(session, K_PROVENANCE) or _insert_if_absent(session, K_PROVENANCE, _provenance(session, now))
    return installation_id, install_key, provenance


def wire_key(install_key: str, endpoint: str) -> str:
    """The X-Athena-Install-Key header: base64url(HMAC-SHA256(key, origin))."""
    raw = base64.urlsafe_b64decode(install_key + "=" * (-len(install_key) % 4))
    digest = hmac.new(raw, classify.endpoint_origin(endpoint).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------

async def _post(endpoint: str, body: bytes, key_header: str) -> Tuple[int, bytes]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"athena-telemetry/{shared.__version__}",
        "X-Athena-Install-Key": key_header,
    }
    async with httpx.AsyncClient(transport=TRANSPORT, timeout=POST_TIMEOUT_SECONDS, follow_redirects=False) as client:
        async with client.stream("POST", endpoint, content=body, headers=headers) as response:
            read = bytearray()
            async for chunk in response.aiter_bytes():
                read.extend(chunk[: RESPONSE_CAP_BYTES - len(read)])
                if len(read) >= RESPONSE_CAP_BYTES:
                    break
            return response.status_code, bytes(read)


def _record_outcome(session, now: datetime, status: Optional[int], error_type: Optional[str], event: str,
                    body: bytes, response: bytes) -> str:
    _set(session, K_ATTEMPT, now.isoformat())
    if status is not None and 200 <= status < 300:
        _set(session, K_SUCCESS, now.isoformat())
        _set(session, K_DUE, (now + SUCCESS_INTERVAL).isoformat())
        _set(session, K_FAILURES, "0")
        _set(session, K_PAYLOAD, body.decode("utf-8"))
        _delete(session, [K_ERROR])
        if event == "first_boot":
            _set(session, K_FIRST_BOOT, now.isoformat())
        session.commit()
        return "sent"

    failures = int(_get(session, K_FAILURES) or "0") + 1
    _set(session, K_FAILURES, str(failures))
    _set(session, K_ERROR, json.dumps({"type": error_type or "http", "status": status}, sort_keys=True))
    if status is None or status >= 500:
        wait = min(MAX_BACKOFF, timedelta(hours=1) * (2 ** (failures - 1)))
    elif status == 429:
        wait = SUCCESS_INTERVAL
    else:
        wait = MAX_BACKOFF
        if status == 403 and b"install_key_mismatch" in response:
            logger.warning("telemetry_install_key_rejected",
                           message="The collector rejected this install's key (another install may be using this "
                                   "ID). Use 'Reset telemetry identity' in the Admin UI to start a new identity.")
    _set(session, K_DUE, (now + wait).isoformat())
    session.commit()
    logger.info("telemetry_send_failed", error_type=error_type or "http", status=status,
                next_attempt_in_hours=round(wait / timedelta(hours=1), 2))
    return "failed"


async def _cycle(force: bool) -> str:
    session = LEASE_SESSION_FACTORY()
    try:
        state = _decide(session, warn=True)
        if not state["enabled"]:
            return "disabled"
        if not force and not _is_due(session, CLOCK()):
            return "not_due"
    finally:
        session.close()

    if _AFTER_DUE_CHECK is not None:
        await _maybe_await(_AFTER_DUE_CHECK())

    try:
        lease = await asyncio.to_thread(
            settings_lease.acquire, LEASE_SESSION_FACTORY, LEASE_KEY, category=CATEGORY, ttl=LEASE_TTL_SECONDS,
            busy_message="telemetry send in progress", now=CLOCK)
    except settings_lease.LeaseBusy:
        return "busy"

    try:
        session = LEASE_SESSION_FACTORY()
        try:
            now = CLOCK()
            state = _decide(session)
            if not state["enabled"]:
                return "disabled"
            if not force and not _is_due(session, now):
                return "not_due"
            installation_id, install_key, provenance = _ensure_identity(session, now)
            event = "heartbeat" if _get(session, K_FIRST_BOOT) else "first_boot"
            facts = await COLLECT(session, now=now, resolver=RESOLVER)
            payload = build_payload(facts, SendState(
                installation_id=installation_id, event=event, provenance=provenance, version=state["version"],
                release_channel=state["release_channel"], install_class=state["install_class"]))
            body = serialize(payload)
            header = wire_key(install_key, state["endpoint"])

            if _BEFORE_FENCE is not None:
                await _maybe_await(_BEFORE_FENCE())
            if not await asyncio.to_thread(settings_lease.renew, LEASE_SESSION_FACTORY, lease,
                                           ttl=LEASE_TTL_SECONDS, now=CLOCK):
                return "lease_lost"
            session.expire_all()
            if _get(session, K_ADMIN_DISABLED) == "true":
                return "disabled"

            try:
                status, response = await _post(state["endpoint"], body, header)
                error_type = None
            except httpx.HTTPError as exc:
                status, response, error_type = None, b"", type(exc).__name__
            return _record_outcome(session, CLOCK(), status, error_type, event, body, response)
        finally:
            session.close()
    finally:
        try:
            settings_lease.release(LEASE_SESSION_FACTORY, lease)
        except Exception as exc:  # the lease expires on its own
            logger.warning("telemetry_lease_release_failed", error_type=type(exc).__name__)


async def run_cycle(force: bool = False) -> str:
    """One bounded cycle. Never raises; returns the outcome: sent, failed,
    not_due, busy, disabled, lease_lost, timeout or error. ``force`` skips
    the due check only, never the lease or enablement."""
    try:
        async with asyncio.timeout(CYCLE_TIMEOUT_SECONDS):
            return await _cycle(force)
    except TimeoutError:
        logger.warning("telemetry_cycle_timeout", seconds=CYCLE_TIMEOUT_SECONDS)
        return "timeout"
    except _Abort as abort:
        return abort.outcome
    except Exception as exc:
        logger.warning("telemetry_cycle_failed", error_type=type(exc).__name__)
        return "error"


# ---------------------------------------------------------------------------
# Loop, startup disclosure, API helpers
# ---------------------------------------------------------------------------

async def _loop() -> None:
    await SLEEP(RNG.uniform(*FIRST_CHECK_DELAY))
    while True:
        await run_cycle()
        await SLEEP(TICK_SECONDS + RNG.uniform(-TICK_JITTER, TICK_JITTER))


def _disclosure(endpoint_display: str) -> str:
    return (
        "Athena sends a pseudonymous install heartbeat about once a day: version, install class, deployment "
        "shape, per-component model family and local vs cloud, and coarse feature and usage buckets. No names, "
        f"hosts, URLs, keys, queries or guest data. Endpoint: {endpoint_display or '(unset)'}. Turn it off with "
        "ATHENA_TELEMETRY=off or DO_NOT_TRACK=1, or in the Admin UI (System Configuration). "
        f"See {DOCS_LINK}."
    )


def start_telemetry() -> None:
    """Log the disclosure and start the loop. Called last in admin-backend's
    startup. Never raises."""
    global _loop_task
    try:
        session = LEASE_SESSION_FACTORY()
        try:
            state = _decide(session, warn=True)
        finally:
            session.close()
        if state["enabled"]:
            logger.info("telemetry_enabled", endpoint=state["endpoint_display"],
                        install_class=state["install_class"], message=_disclosure(state["endpoint_display"]))
        else:
            logger.info("telemetry_disabled", reason=state["reason"], endpoint=state["endpoint_display"],
                        message=f"Install telemetry is off ({state['reason']}). "
                                + _disclosure(state["endpoint_display"]))
        reason = state["reason"]
        if reason.startswith("install_class_") or reason == "ephemeral_database":
            return
        if _loop_task is None or _loop_task.done():
            _loop_task = asyncio.get_running_loop().create_task(_loop())
    except Exception as exc:
        logger.warning("telemetry_start_failed", error_type=type(exc).__name__)


async def stop_telemetry() -> None:
    global _loop_task
    task, _loop_task = _loop_task, None
    for pending in [task, *list(_background)]:
        if pending is None or pending.done():
            continue
        pending.cancel()
        try:
            await pending
        except (asyncio.CancelledError, Exception):
            pass


def request_send() -> None:
    """Schedule a forced cycle in the background and return immediately."""
    task = asyncio.get_running_loop().create_task(run_cycle(force=True))
    _background.add(task)
    task.add_done_callback(_background.discard)


def get_status(db, user) -> Dict[str, Any]:
    """Read-only status for the Admin UI. Never creates an identity and never
    returns the install key."""
    state = _decide(db)
    payload = _get(db, K_PAYLOAD)
    try:
        last_payload = json.loads(payload) if payload else None
    except ValueError:
        last_payload = None
    error = _get(db, K_ERROR)
    try:
        last_error = json.loads(error) if error else None
    except ValueError:
        last_error = None
    return {
        "enabled": state["enabled"],
        "reason": state["reason"],
        "env_locked": state["env_locked"],
        "endpoint": state["endpoint_display"],
        "installation_id": _get(db, K_ID),
        "install_class": state["install_class"],
        "provenance": _get(db, K_PROVENANCE),
        "release_channel": state["release_channel"],
        "schema_version": SCHEMA_VERSION,
        "last_attempt_at": _get(db, K_ATTEMPT),
        "last_success_at": _get(db, K_SUCCESS),
        "last_error": last_error,
        "next_due_at": _get(db, K_DUE),
        "last_payload": last_payload,
        "can_manage": bool(user is not None and user.has_permission("manage_infrastructure")),
    }


def current_state(db) -> Dict[str, Any]:
    """Enablement as the routes need it: enabled, reason, env_locked."""
    state = _decide(db)
    return {k: state[k] for k in ("enabled", "reason", "env_locked")}


def last_attempt_at(db) -> Optional[datetime]:
    return _parse_time(_get(db, K_ATTEMPT))


def set_admin_disabled(db, disabled: bool) -> None:
    _set(db, K_ADMIN_DISABLED, "true" if disabled else "false")
    db.commit()


def reset_identity() -> Optional[str]:
    """Delete the identity and every piece of send state under the send lock
    (LeaseBusy when a cycle holds it), so the next cycle mints a new ID and
    sends first_boot. Returns the old ID."""
    lease = settings_lease.acquire(LEASE_SESSION_FACTORY, LEASE_KEY, category=CATEGORY, ttl=LEASE_TTL_SECONDS,
                                   busy_message="telemetry send in progress", now=CLOCK)
    try:
        session = LEASE_SESSION_FACTORY()
        try:
            old = _get(session, K_ID)
            _delete(session, RESET_KEYS)
            session.commit()
            return old
        finally:
            session.close()
    finally:
        settings_lease.release(LEASE_SESSION_FACTORY, lease)


def _loop_task_for_tests() -> Optional[asyncio.Task]:
    return _loop_task


def _reset_for_tests() -> None:
    """Forget in-process state (the loop task, deduplicated warnings)."""
    global _loop_task
    _loop_task = None
    _background.clear()
    _warned.clear()
