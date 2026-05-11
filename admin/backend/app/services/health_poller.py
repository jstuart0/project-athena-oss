"""Background health poller for the service registry.

Phase 4 (ATHENA-1): replaces the per-request inline ping loop with an
async background task that polls every enabled RagService on a configurable
interval and caches results into health_status / last_health_check /
last_response_time_ms / last_error / health_message columns.

Pattern source: admin/backend/app/services/calendar_sync.py:236-299
(librarian #2 from plan r1).

Column ownership (dexter D2 / D7):
- This module WRITES: health_status, last_health_check, last_response_time_ms,
  last_error, health_message.
- This module NEVER WRITES: is_running (owned by service_control.py),
  updated_at (config-change audit signal; synchronize_session=False bypasses
  ORM onupdate so it is not bumped during bulk writes).

Multi-replica note (otto H1): admin-backend runs with replicas: 2. Both pods
poll, doubling outbound requests and producing concurrent writes. Writes are
idempotent (last-write-wins on health-poll columns). Leader-election is
deferred to Campaign 5 follow-up ticket Q-NEW2; the prerequisite SSRF guard
(xander HIGH-1) lands in this file.

SSRF allowlist: add HEALTH_POLL_ALLOWED_PRIVATE_HOSTS (comma-separated CIDRs
or hostnames) to permit RFC1918/loopback targets. Default empty = fail-closed
for OSS deployers whose services are on the public internet. Jay's homelab:
set to "192.168.10.0/24" (or the specific pod CIDR) so the poller can reach
cluster services.
"""

import asyncio
import re
import time
from datetime import datetime
from typing import Optional

import httpx
import structlog

from app.database import get_db_context
from app.models import RagService
from shared.config import get_config

logger = structlog.get_logger()

# Module-level task handle (calendar_sync.py pattern).
_poll_task: Optional[asyncio.Task] = None

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

async def health_poll_loop(interval_override: Optional[int] = None) -> None:
    """Poll health for all enabled services on a schedule.

    Pattern source: admin/backend/app/services/calendar_sync.py:236-299.

    interval_override: test hook — pass 0 to run a single cycle without
    sleeping. Production code always uses None (reads cfg.health_poll_interval_seconds).
    """
    cfg = get_config()
    if cfg.dev_mode:
        logger.info('health_poll_disabled_dev_mode')
        return

    interval = cfg.health_poll_interval_seconds if interval_override is None else interval_override
    semaphore = asyncio.Semaphore(cfg.health_poll_concurrency)

    while True:
        try:
            await _poll_all_services(semaphore)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error('health_poll_iteration_failed', error_type=type(e).__name__)

        if interval == 0:
            # Test mode: run exactly one cycle then exit.
            break
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Lifespan management (calendar_sync.py pattern)
# ---------------------------------------------------------------------------

def start_health_polling() -> None:
    """Start the background health poll task.

    Called from main.py during application startup (next to start_background_sync).
    No-ops when DEV_MODE=true (health_poll_loop returns immediately on that path).
    Idempotent: returns without creating a second task if one is already running.
    """
    global _poll_task

    if _poll_task is not None and not _poll_task.done():
        logger.warning('health_poll_task_already_running')
        return

    _poll_task = asyncio.create_task(health_poll_loop())
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
