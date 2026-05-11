"""Background health poller for the service registry with Redis leader election.

Phase 4 (ATHENA-1): replaces the per-request inline ping loop with an async
background task that polls every enabled RagService on a configurable interval
and caches results into health_status / last_health_check / last_response_time_ms
/ last_error / health_message columns. Phase 1 (ATHENA-18): adds Redis SETNX
leader election so only one pod polls and writes per cycle when admin-backend
runs with replicas: 2. Each iteration of health_poll_loop attempts to acquire
or renew a Redis lease via SET NX EX (acquire) or a Lua check-and-set (atomic
renewal) keyed at LEASE_KEY ("athena:health_poll_lease"). LEASE_TTL_SECONDS=40
must exceed health_poll_interval_seconds (default 30); start_health_polling
raises SystemExit at startup if this invariant is violated. HOLDER_ID is
seeded from the HOSTNAME env var (the pod name in Kubernetes) or a random
uuid4 — it identifies the current lease holder for logging but is NOT an auth
token. Lease integrity depends on Redis access control; any process with Redis
write access can overwrite the key. HEARTBEAT_INTERVAL_SECONDS=20 governs a
per-cycle background asyncio.Task that renews the lease mid-poll so a slow
cycle (many services + timeouts) cannot outlive the TTL. If the heartbeat
cannot renew the lease (strict-abort policy), it cancels the in-flight
_poll_all_services task before any further DB writes, ensuring no replica
writes to health columns after lease loss. On-demand poll endpoints
(/poll-now, /status/all, /refresh-status) call _poll_all_services directly
and are intentionally not gated — operators must be able to force a refresh
from either replica. redis_client=None (DEV_MODE or test paths) is treated as
"always leader, skip lease entirely" so the existing dev loop behavior is
unchanged.

Column ownership (dexter D2 / D7): this module WRITES health_status,
last_health_check, last_response_time_ms, last_error, health_message. It
NEVER WRITES is_running (owned by service_control.py) or updated_at
(config-change audit signal; synchronize_session=False bypasses ORM onupdate
so it is not bumped during bulk writes). SSRF allowlist: set
HEALTH_POLL_ALLOWED_PRIVATE_HOSTS (comma-separated CIDRs or hostnames) to
permit RFC1918/loopback targets; default empty = fail-closed.
"""

import asyncio
import contextlib
import os
import re
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
import redis.exceptions
import structlog

from app.database import get_db_context
from app.models import RagService
from shared.config import get_config

logger = structlog.get_logger()

# Module-level task handle (calendar_sync.py pattern).
_poll_task: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------------
# Leader-election constants (ATHENA-18)
# ---------------------------------------------------------------------------

LEASE_KEY = "athena:health_poller:leader"
LEASE_TTL_SECONDS = 40
# Must be > health_poll_interval_seconds (default 30s). Enforced at
# start_health_polling() via a startup assertion. Promote to AthenaConfig
# only if the interval is ever made tunable beyond this value.

# While the leader is inside _poll_all_services, a background heartbeat task
# renews the lease every HEARTBEAT_INTERVAL_SECONDS so a slow poll cycle
# cannot let the lease expire (D4b). Floor at 1s for tests that monkeypatch
# LEASE_TTL_SECONDS small (e.g., 2s).
HEARTBEAT_INTERVAL_SECONDS = max(1, LEASE_TTL_SECONDS // 2)  # = 20s

# Stable per-process identity — K8s sets HOSTNAME to the pod name before
# Python imports, so this resolves correctly in production.  Tests that need
# deterministic IDs should pass holder_id directly to start_health_polling /
# health_poll_loop rather than monkeypatching this constant.
HOLDER_ID = os.getenv("HOSTNAME") or str(uuid.uuid4())

# Atomic check-and-renew Lua script. Avoids the GET→SET XX TOCTOU race where
# the key can expire between GET and XX, letting a different replica take it
# via NX in the gap; the original holder's XX would then incorrectly succeed
# against the new owner's value because XX only checks key existence.
# KEYS[1] = LEASE_KEY, ARGV[1] = holder_id, ARGV[2] = ttl_seconds
_ATOMIC_RENEW_LUA = """\
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("SET", KEYS[1], ARGV[1], "XX", "EX", ARGV[2])
else
  return nil
end
"""

# ---------------------------------------------------------------------------
# Sanitization helpers (xander MED-1)
# ---------------------------------------------------------------------------

_IP_RE = re.compile(r'\b\d{1,3}(\.\d{1,3}){3}\b')
_URL_RE = re.compile(r'https?://[^\s]+')


def _classify_and_sanitize(
    exc: Optional[Exception],
    status_code: Optional[int],
) -> tuple[str, str]:
    """Return (category, sanitized_detail) for last_error.

    Categories: connection_refused, timeout, http_5xx, http_4xx, ssrf_blocked,
    unknown.

    detail: raw exception message after URL/IP scrub, truncated to 100 chars.
    (xander MED-1 / plan D13)
    """
    if status_code is not None:
        if 500 <= status_code < 600:
            return ('http_5xx', f'HTTP {status_code}')
        if 400 <= status_code < 500:
            return ('http_4xx', f'HTTP {status_code}')
    if isinstance(exc, httpx.TimeoutException):
        return ('timeout', 'timeout')
    if isinstance(exc, httpx.ConnectError):
        raw = str(exc)
        scrubbed = _URL_RE.sub('[redacted-url]', _IP_RE.sub('[redacted-ip]', raw))
        return ('connection_refused', scrubbed[:100])
    raw = str(exc) if exc else 'unknown'
    scrubbed = _URL_RE.sub('[redacted-url]', _IP_RE.sub('[redacted-ip]', raw))
    return ('unknown', scrubbed[:100])


# ---------------------------------------------------------------------------
# SSRF guard (xander HIGH-1 / round-2 H1)
# ---------------------------------------------------------------------------

def _validate_service_url(host: str, port: int, path: str) -> tuple[bool, str]:
    """SSRF allowlist guard for the health poller.

    Mirrors ``_PRIVATE_NETS`` in src/control_agent/url_validator.py:21-30
    verbatim (8 ranges). Both lists MUST stay byte-for-byte aligned until the
    consolidation follow-up ticket (see Phase 5 deliverables).

    Allowlist override: HEALTH_POLL_ALLOWED_PRIVATE_HOSTS env var
    (comma-separated hostnames or CIDRs) lets deployers running services on
    RFC1918 ranges opt-in. Default empty — fail-closed for OSS deployers.

    Path validation (round-2 H1): rejects paths containing CRLF, NUL, or
    traversal segments to prevent header injection when the path is
    concatenated into a URL.

    Returns (allowed: bool, reason: str). reason is non-empty on block.
    """
    import socket
    from ipaddress import ip_address, ip_network

    # MUST mirror src/control_agent/url_validator.py:21-30 verbatim.
    _PRIVATE_NETS = [
        ip_network('10.0.0.0/8'),
        ip_network('172.16.0.0/12'),
        ip_network('192.168.0.0/16'),
        ip_network('169.254.0.0/16'),   # link-local (AWS metadata etc.)
        ip_network('127.0.0.0/8'),      # loopback
        ip_network('::1/128'),           # IPv6 loopback
        ip_network('fc00::/7'),          # IPv6 unique-local (RFC-4193)
        ip_network('fe80::/10'),         # IPv6 link-local
    ]

    if not host:
        return False, 'empty host'

    # Path sanitization (round-2 H1).
    if path is None:
        path = ''
    if '\r' in path or '\n' in path:
        return False, 'path contains CRLF'
    if '\x00' in path:
        return False, 'path contains NUL byte'
    if '..' in path:
        return False, 'path contains traversal segments'

    # Block k8s control-plane hostnames explicitly. kubernetes.default.svc
    # resolves to a ClusterIP that may not be in any _PRIVATE_NETS range, so
    # the IP-based check below would miss it.
    if (
        host == 'kubernetes.default.svc'
        or host.endswith('.cluster.local')
        or host.endswith('.svc')
    ):
        return False, 'k8s control-plane hostname blocked'

    cfg = get_config()
    raw_allow = (cfg.health_poll_allowed_private_hosts or '').strip()
    allow_hosts: set[str] = set()
    allow_nets: list = []
    if raw_allow:
        for entry in (e.strip() for e in raw_allow.split(',') if e.strip()):
            if '/' in entry:
                try:
                    allow_nets.append(ip_network(entry, strict=False))
                except ValueError:
                    continue
            else:
                allow_hosts.add(entry.lower())

    if host.lower() in allow_hosts:
        return True, ''

    try:
        resolved = ip_address(socket.gethostbyname(host))
    except Exception as e:
        return False, f'cannot resolve: {type(e).__name__}'

    for net in allow_nets:
        if resolved in net:
            return True, ''

    for net in _PRIVATE_NETS:
        if resolved in net:
            return False, (
                f'address {resolved} in blocked range {net}; '
                'set HEALTH_POLL_ALLOWED_PRIVATE_HOSTS to opt-in'
            )

    return True, ''


# ---------------------------------------------------------------------------
# Leader-election helpers (ATHENA-18)
# ---------------------------------------------------------------------------

async def _acquire_or_renew_lease(
    redis_client,
    holder_id: str,
) -> tuple[bool, str]:
    """Try to acquire or renew the health-poll leader lease.

    Returns ``(is_leader, reason)`` where reason is one of:
    ``"acquired"``, ``"renewed"``, ``"not_leader"``, or
    ``"redis_error:<ExcType>"``.  Never raises.

    Two-step protocol — both steps are individually atomic on Redis ≥ 2.6.12:

    1. ``SET NX EX`` — wins the lease on first call or after expiry.
    2. Lua eval — atomically GET + SET XX EX if the stored value matches
       ``holder_id``.  Closes the TOCTOU race where the key could expire
       between a failed NX and a subsequent SET XX, letting another replica
       claim it while the original holder's XX would succeed on the wrong
       owner (XX checks key existence only, not value).

    Decode-normalization note: with ``decode_responses=False`` (redis-py
    default) response values are ``bytes``; command arguments are encoded as
    UTF-8 bulk strings before transport and the Lua comparison is server-side
    binary-string equality.  ARGV[1] and the NX-stored value therefore compare
    equal whenever the same Python ``holder_id`` string is used for both calls
    within one process.
    """
    try:
        # Acquire path — wins the lease when the key does not exist.
        acquired = await redis_client.set(
            LEASE_KEY, holder_id, nx=True, ex=LEASE_TTL_SECONDS
        )
        if acquired:
            return (True, "acquired")

        # Renew path — key exists (held by us or another replica).
        result = await redis_client.eval(
            _ATOMIC_RENEW_LUA, 1, LEASE_KEY, holder_id, LEASE_TTL_SECONDS
        )
        # redis-py may return bytes b"OK" or str "OK" depending on
        # decode_responses setting; both are truthy.
        if result in (b"OK", "OK"):
            return (True, "renewed")

        return (False, "not_leader")

    except (redis.exceptions.RedisError, asyncio.TimeoutError) as exc:
        return (False, f"redis_error:{type(exc).__name__}")
    except Exception as exc:
        return (False, f"unexpected:{type(exc).__name__}")


async def _heartbeat_lease(
    redis_client,
    holder_id: str,
    lease_lost: asyncio.Event,
    *,
    interval_seconds: int,
) -> None:
    """Renew the leader lease in the background while a poll cycle runs.

    Designed to be started as an asyncio.Task at the top of each leader
    cycle and cancelled in a ``finally`` block after the poll completes.

    On each tick:
    - Calls ``_acquire_or_renew_lease``; logs ``health_poll_lease_renewed``
      (debug) on success.
    - On any ``(False, reason)`` result: logs ``health_poll_lease_heartbeat_lost``
      (warning), sets ``lease_lost`` so the parent loop can abort the
      in-flight ``_poll_all_services`` task (strict-abort policy — D4b /
      codex r3 HIGH), then returns cleanly.

    ``asyncio.CancelledError`` is re-raised so that ``await hb_task`` in the
    parent's ``finally`` block returns promptly when the poll finishes
    normally and the parent cancels this task.

    Redis errors are caught inside ``_acquire_or_renew_lease`` and converted
    to ``(False, "redis_error:<type>")``, so this function normally does not
    raise.  As a belt-and-suspenders guard, any unexpected exception that
    escapes the helper is caught here and treated as the same lease-loss
    semantics rather than crashing the loop.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            ok, reason = await _acquire_or_renew_lease(redis_client, holder_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ok, reason = False, f"redis_error:{type(exc).__name__}"

        if ok:
            logger.debug(
                "health_poll_lease_renewed",
                holder_id=holder_id,
            )
        else:
            logger.warning(
                "health_poll_lease_heartbeat_lost",
                holder_id=holder_id,
                reason=reason,
            )
            lease_lost.set()
            return


# ---------------------------------------------------------------------------
# Per-service poll (inner loop)
# ---------------------------------------------------------------------------

async def _poll_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    svc_id: int,
    name: str,
    host: str,
    port: int,
    path: str,
    protocol: str = 'http',
) -> tuple[int, str, Optional[int], str, str, Optional[str]]:
    """Poll one service.

    Returns (svc_id, status, response_time_ms, error_category, error_detail,
    health_message).

    protocol: 'http' or 'https' — read from RagService.protocol so that HTTPS
    services are polled at the correct scheme.  Defaults to 'http' for callers
    that pre-date the protocol column (codex r2 M-4).
    """
    # SSRF guard — must precede every outbound HTTP request. (xander HIGH-1)
    ok, reason = _validate_service_url(host, port, path)
    if not ok:
        logger.warning('poll_blocked_by_ssrf_guard', service=name, reason=reason)
        # last_error stores categorical value per MED-1 spec.
        return (svc_id, 'unhealthy', None, 'ssrf_blocked', f'ssrf-blocked:{reason[:80]}', None)

    # Normalise protocol — only http/https are valid; fall back to http.
    scheme = protocol if protocol in ('http', 'https') else 'http'

    async with semaphore:
        url = f'{scheme}://{host}:{port}{path}'
        start = time.monotonic()
        try:
            resp = await client.get(url)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    health_message = (
                        data.get('message') or data.get('status') or ''
                    )[:500]
                except Exception:
                    health_message = ''
                return (svc_id, 'healthy', elapsed_ms, 'ok', '', health_message)
            cat, detail = _classify_and_sanitize(None, resp.status_code)
            return (svc_id, 'unhealthy', elapsed_ms, cat, detail, None)
        except Exception as e:
            cat, detail = _classify_and_sanitize(e, None)
            status = 'unhealthy' if cat == 'timeout' else 'unhealthy'
            return (svc_id, status, None, cat, detail, None)


# ---------------------------------------------------------------------------
# Full-poll cycle (called by the loop and by the /poll-now endpoint)
# ---------------------------------------------------------------------------

async def _poll_all_services(semaphore: asyncio.Semaphore) -> dict:
    """Poll all enabled services once and write results.

    Returns a summary dict {services_polled, healthy, unhealthy, unknown}.

    synchronize_session=False is REQUIRED so the bulk UPDATE doesn't trigger
    ORM session refresh on rows that may have been mutated concurrently (e.g.,
    service_control.py setting is_running). dexter D2 / D7.
    """
    cfg = get_config()
    with get_db_context() as db:
        services = db.query(RagService).filter(RagService.enabled.is_(True)).all()
        # Extract fields before closing the read context.
        # Tuple: (svc_id, name, host, port, path, protocol) — protocol added by
        # codex r2 M-4 so _poll_one uses the stored scheme instead of hardcoding http.
        targets = [
            (s.id, s.name, s.host or '', s.port or 0, s.health_endpoint or '/health', s.protocol or 'http')
            for s in services
        ]

    async with httpx.AsyncClient(timeout=float(cfg.health_poll_timeout_seconds)) as client:
        tasks = [_poll_one(client, semaphore, *t) for t in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    now = datetime.utcnow()
    summary = {'services_polled': len(targets), 'healthy': 0, 'unhealthy': 0, 'unknown': 0}

    with get_db_context() as db:
        for result in results:
            # bob C1 / dexter D7 / otto M3 / ian I-M1 — guard before destructure.
            if isinstance(result, Exception):
                logger.error('poll_task_failed', error_type=type(result).__name__)
                summary['unknown'] += 1
                continue
            svc_id, status, response_time_ms, error_category, error_detail, health_message = result
            last_error = f'{error_category}:{error_detail}' if error_category != 'ok' else None

            # POLLER WRITES (dexter D2): disjoint from service_control writes.
            # Do NOT include 'is_running' (owned by service_control.py).
            # Do NOT include 'updated_at' (config-change audit; dexter D7).
            # synchronize_session=False issues a raw UPDATE, bypassing ORM
            # event hooks including the onupdate=func.now() on updated_at.
            db.query(RagService).filter(RagService.id == svc_id).update(
                {
                    RagService.health_status: status,
                    RagService.last_health_check: now,
                    RagService.last_response_time_ms: response_time_ms,
                    RagService.last_error: last_error,
                    RagService.health_message: health_message,
                },
                synchronize_session=False,
            )

            if status == 'healthy':
                summary['healthy'] += 1
            elif status in ('unhealthy', 'offline'):
                summary['unhealthy'] += 1
            else:
                summary['unknown'] += 1

        db.commit()

    logger.info(
        'health_poll_complete',
        services_polled=summary['services_polled'],
        healthy=summary['healthy'],
        unhealthy=summary['unhealthy'],
    )
    return summary


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def health_poll_loop(
    interval_override: Optional[int] = None,
    *,
    redis_client=None,
    holder_id: Optional[str] = None,
) -> None:
    """Poll health for all enabled services on a schedule.

    Pattern source: admin/backend/app/services/calendar_sync.py:236-299.

    interval_override: test hook — pass 0 to run a single cycle without
    sleeping. Production code always uses None (reads cfg.health_poll_interval_seconds).

    redis_client: when provided, leader election is active.  Only the replica
    that holds the Redis lease issues outbound pings and writes to the DB.
    Non-leader replicas log ``health_poll_skipped_not_leader`` and sleep.
    On lease loss mid-cycle (heartbeat cannot renew), the in-flight
    ``_poll_all_services`` task is cancelled before any further DB writes
    (strict-abort policy — ATHENA-18 D4b / codex r3 HIGH).

    When ``redis_client`` is None (DEV_MODE or no-Redis paths), the pre-ATHENA-18
    "always poll" behavior is preserved — this keeps existing tests green and
    gives a safe fallback if wiring is missing.

    holder_id: stable per-process identity for the lease value.  Defaults to
    the module-level HOLDER_ID (resolved from HOSTNAME env var at import).
    Pass an explicit value in tests for deterministic lease attribution.
    """
    cfg = get_config()
    if cfg.dev_mode:
        logger.info('health_poll_disabled_dev_mode')
        return

    interval = cfg.health_poll_interval_seconds if interval_override is None else interval_override
    semaphore = asyncio.Semaphore(cfg.health_poll_concurrency)
    _holder_id = holder_id if holder_id is not None else HOLDER_ID

    if redis_client is None:
        logger.info('health_poll_lease_disabled_no_redis')

    was_leader: bool = False

    while True:
        # ------------------------------------------------------------------
        # Leader-election path (redis_client provided)
        # ------------------------------------------------------------------
        if redis_client is not None:
            try:
                acquired, reason = await _acquire_or_renew_lease(redis_client, _holder_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Belt-and-suspenders: _acquire_or_renew_lease never raises,
                # but guard against any future change.
                logger.warning(
                    'health_poll_lease_redis_error',
                    holder_id=_holder_id,
                    error_type=type(exc).__name__,
                )
                was_leader = False
                if interval_override == 0:
                    break
                await asyncio.sleep(interval)
                continue

            if not acquired:
                logger.debug(
                    'health_poll_skipped_not_leader',
                    holder_id=_holder_id,
                    reason=reason,
                )
                was_leader = False
                # Single-cycle test hook: break instead of spinning.
                if interval_override == 0:
                    break
                await asyncio.sleep(interval)
                continue

            # We hold the lease.
            if not was_leader:
                logger.info(
                    'health_poll_lease_acquired',
                    holder_id=_holder_id,
                    ttl_seconds=LEASE_TTL_SECONDS,
                )
                was_leader = True
            else:
                logger.debug('health_poll_lease_renewed', holder_id=_holder_id)

            # D4b: race the poll against lease loss.  The heartbeat task
            # renews the lease every HEARTBEAT_INTERVAL_SECONDS.  If it
            # cannot renew, it sets lease_lost; we cancel the in-flight poll.
            lease_lost: asyncio.Event = asyncio.Event()
            hb_task = asyncio.create_task(
                _heartbeat_lease(
                    redis_client,
                    _holder_id,
                    lease_lost,
                    interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                )
            )
            poll_task = asyncio.create_task(_poll_all_services(semaphore))
            lost_waiter = asyncio.create_task(lease_lost.wait())
            try:
                done, _pending = await asyncio.wait(
                    {poll_task, lost_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lease_lost.is_set() and not poll_task.done():
                    # Strict abort: cancel the in-flight poll before any
                    # further DB writes.
                    poll_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await poll_task
                    logger.warning(
                        'health_poll_aborted_lease_lost',
                        holder_id=_holder_id,
                    )
                    was_leader = False
                elif poll_task.done():
                    # Propagate any exception from the poll (matches
                    # pre-ATHENA-18 behavior).
                    poll_task.result()
            finally:
                # Cancel both auxiliary tasks.  The heartbeat catches its own
                # Redis errors (D8 / S6) so awaiting it after cancel never
                # raises non-CancelledError.
                hb_task.cancel()
                lost_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task
                with contextlib.suppress(asyncio.CancelledError):
                    await lost_waiter

            # Single-cycle test hook: break after the leader path too.
            if interval_override == 0:
                break
            await asyncio.sleep(interval)
            continue

        # ------------------------------------------------------------------
        # Legacy fallback path (redis_client is None — pre-ATHENA-18 behavior)
        # ------------------------------------------------------------------
        try:
            await _poll_all_services(semaphore)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error('health_poll_iteration_failed', error_type=type(e).__name__)

        if interval_override == 0:
            # Test mode: run exactly one cycle then exit.
            break
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Lifespan management (calendar_sync.py pattern)
# ---------------------------------------------------------------------------

def start_health_polling(
    interval_override: Optional[int] = None,
    *,
    redis_client=None,
    holder_id: Optional[str] = None,
) -> None:
    """Start the background health poll task.

    Called from main.py during application startup (next to start_background_sync).
    No-ops when DEV_MODE=true (health_poll_loop returns immediately on that path).
    Idempotent: returns without creating a second task if one is already running.

    redis_client: when provided, leader election is enabled.  The invariant
    ``LEASE_TTL_SECONDS > health_poll_interval_seconds`` is enforced here via
    ``SystemExit`` so a misconfigured deployment fails fast at startup rather
    than silently electing spurious leaders.  DEV_MODE / no-Redis paths
    (``redis_client is None``) bypass this gate because leader election is
    inactive there.

    holder_id: stable per-process identity for lease attribution.  Defaults to
    ``HOSTNAME`` env var or a random UUID suffix.  Pass an explicit value in
    tests for deterministic attribution.
    """
    global _poll_task

    cfg = get_config()

    # D4a invariant gate (ATHENA-18): TTL must exceed the poll interval so the
    # lease does not expire between cycles.  Matches the SystemExit("FATAL: …")
    # idiom used by the rest of admin-backend startup gates (main.py:427-475).
    if redis_client is not None and LEASE_TTL_SECONDS <= cfg.health_poll_interval_seconds:
        raise SystemExit(
            f"FATAL: LEASE_TTL_SECONDS ({LEASE_TTL_SECONDS}) must be > "
            f"health_poll_interval_seconds ({cfg.health_poll_interval_seconds}); "
            f"leader election cannot guarantee single-poller invariant. "
            f"Raise LEASE_TTL_SECONDS or lower the poll interval."
        )

    # Resolve holder identity once per process lifetime.
    resolved_holder_id = (
        holder_id
        or os.getenv("HOSTNAME")
        or f"unknown-{uuid.uuid4().hex[:8]}"
    )

    if _poll_task is not None and not _poll_task.done():
        logger.warning('health_poll_task_already_running')
        return

    _poll_task = asyncio.create_task(
        health_poll_loop(
            interval_override=interval_override,
            redis_client=redis_client,
            holder_id=resolved_holder_id,
        )
    )
    logger.info('health_poll_background_task_created')


async def stop_health_polling() -> None:
    """Stop the background health poll task gracefully.

    Called from main.py during application shutdown (next to stop_background_sync).
    """
    global _poll_task

    if _poll_task is None or _poll_task.done():
        return

    logger.info('health_poll_background_task_stopping')
    _poll_task.cancel()

    try:
        await _poll_task
    except asyncio.CancelledError:
        pass

    logger.info('health_poll_background_task_stopped')
