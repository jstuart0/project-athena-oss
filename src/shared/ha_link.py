"""What Athena's own Home Assistant connection last did, merged across replicas.

The status is passive. Nothing here sends a request to Home Assistant: each
orchestrator replica records the outcome of the HA traffic it already makes
(``RecordingTransport`` under the REST clients) and publishes it, with its area
registry state, to one Redis hash. A background loop merges the hash into a
cluster view that handlers read from memory. A health or probe handler asks
``HALinkPublisher.view()`` and never touches Home Assistant or Redis.

Layout:

- ``evidence`` / ``exception_outcome``: which responses and exceptions count,
  as a closed set of outcome words. No exception text or object is kept.
- ``HALinkMonitor``: the REST side. Synchronous, no I/O.
- ``RecordingTransport``: an httpx transport wrapper feeding the monitor.
- ``merge``: pure. Combines the replicas' entries under clock-skew rules.
- ``link_state``: pure. The one truth table (``tests/fixtures/ha_link``).
- ``HALinkPublisher``: the Redis loop and the cached view, with a local
  fallback that says so (``coverage``/``coverage_reason``).

Logs carry enum words and exception class names only.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

import httpx
import structlog

logger = structlog.get_logger(__name__)

REDIS_KEY = "athena:ha_link:v1"
REDIS_KEY_TTL_SECONDS = 86400
PRUNE_AFTER_SECONDS = 3600
PUBLISH_INTERVAL_SECONDS = 15.0
PUBLISH_MIN_GAP_SECONDS = 1.0
PEER_WINDOW_SECONDS = 900.0
MAX_FIELDS = 64
MAX_VALUE_BYTES = 4096
SKEW_TOLERANCE_SECONDS = 5.0
STALE_MERGE_INTERVALS = 3
FAILING_AFTER_CONSECUTIVE = 3

OUTCOME_OK = "ok"
OUTCOMES = frozenset({"ok", "auth_rejected", "forbidden", "ha_error", "timeout", "unreachable", "tls_error", "error"})
REGISTRY_STATES = ("refused", "unavailable", "ok", "disabled")
FAILURE_CLASSES = frozenset({"auth", "timeout", "unreachable", "other"})

COVERAGE_CLUSTER = "cluster"
COVERAGE_REPLICA = "replica"
REASON_REDIS_ERROR = "redis_error"
REASON_REDIS_UNCONFIGURED = "redis_unconfigured"

STATE_CONNECTED = "connected"
STATE_ATTENTION = "attention"
STATE_FAILING = "failing"
STATE_UNKNOWN = "unknown"
STATE_NOT_CONFIGURED = "not_configured"
HEALTHY_STATES = frozenset({STATE_CONNECTED, STATE_ATTENTION})

# Responses on these paths are the evidence; Home Assistant's admin-only
# /api/config/* and everything else say nothing about the token or the link.
_STATES_PREFIX = "/api/states"
_SERVICES_PREFIX = "/api/services/"
_RELOAD_SUFFIX = "/reload"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_time(value: Any) -> Optional[datetime]:
    """An ISO timestamp as an aware UTC value, else None. A naive value is UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        # OverflowError: a year-0001 time with a positive offset has no UTC form.
        return None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _is_evidence_path(path: str) -> bool:
    if path in ("/api", "/api/"):
        return True
    if path == _STATES_PREFIX or path.startswith(_STATES_PREFIX + "/"):
        return True
    return path.startswith(_SERVICES_PREFIX) and not path.endswith(_RELOAD_SUFFIX)


def evidence(method: str, path: str, status: int) -> Optional[str]:
    """The outcome word for a response on a non-admin read/write path, or None
    when the response says nothing about the link (other paths, other 4xx, 3xx)."""
    if not _is_evidence_path(path):
        return None
    if 200 <= status < 300 or status == 404:
        return "ok"
    if status == 401:
        return "auth_rejected"
    if status == 403:
        return "forbidden"
    if status >= 500:
        return "ha_error"
    return None


def _ssl_in_chain(exc: BaseException) -> bool:
    seen = 0
    link: Optional[BaseException] = exc
    while link is not None and seen < 6:
        for cause in (link.__cause__, link.__context__):
            if isinstance(cause, ssl.SSLError):
                return True
        link = link.__cause__ or link.__context__
        seen += 1
    return False


def exception_outcome(exc: BaseException) -> str:
    """The outcome word for a transport exception, by type only."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError) and _ssl_in_chain(exc):
        return "tls_error"
    if isinstance(exc, httpx.TransportError):
        return "unreachable"
    return "error"


# ---------------------------------------------------------------------------
# The REST side
# ---------------------------------------------------------------------------


class HALinkMonitor:
    """The last outcomes of this process's Home Assistant REST traffic.

    ``record_*`` are synchronous with no I/O. An event's time is never earlier
    than the previous one, so a wall clock that steps back can't make an older
    event look newer than the one recorded after it.
    """

    def __init__(self, wall: Callable[[], datetime] = utc_now):
        self._wall = wall
        self._last_outcome: Optional[str] = None
        self._last_outcome_at: Optional[datetime] = None
        self._last_success_at: Optional[datetime] = None
        self._consecutive_failures = 0
        self.changed = asyncio.Event()

    def _stamp(self) -> datetime:
        now = self._wall()
        floor = self._last_outcome_at
        if floor is not None and now <= floor:
            return floor + timedelta(microseconds=1)
        return now

    def _record(self, outcome: str, error_class: Optional[str] = None) -> None:
        at = self._stamp()
        previous = self._last_outcome
        self._last_outcome = outcome
        self._last_outcome_at = at
        if outcome == OUTCOME_OK:
            self._last_success_at = at
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
        if outcome != previous:
            fields: Dict[str, Any] = {"outcome": outcome}
            if error_class is not None:
                fields["error_class"] = error_class
            logger.info("ha_link_state_changed", **fields)
        self.changed.set()

    def record_response(self, method: str, path: str, status: int) -> None:
        outcome = evidence(method, path, status)
        if outcome is not None:
            self._record(outcome)

    def record_exception(self, exc: BaseException) -> None:
        if isinstance(exc, asyncio.CancelledError):
            return
        self._record(exception_outcome(exc), error_class=type(exc).__name__)

    def summary(self) -> Dict[str, Any]:
        return {
            "last_outcome": self._last_outcome,
            "last_outcome_at": _iso(self._last_outcome_at),
            "last_success_at": _iso(self._last_success_at),
            "consecutive_failures": self._consecutive_failures,
        }


class RecordingTransport(httpx.AsyncBaseTransport):
    """Hands every request to ``inner`` unchanged and records the outcome.

    The identical response object is returned and the identical exception is
    re-raised. The outcome comes from the status line; a body-read failure
    later on is not recorded again. Cancellation records nothing.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, monitor: HALinkMonitor):
        self._inner = inner
        self._monitor = monitor

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._inner.handle_async_request(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._monitor.record_exception(exc)
            raise
        self._monitor.record_response(request.method, request.url.path, response.status_code)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def recording_transport(monitor: HALinkMonitor) -> RecordingTransport:
    """The transport an HA REST client passes to ``httpx.AsyncClient``.

    ``verify=False`` is the same self-signed-certificate setting the clients
    have always used. Passing ``transport=`` also means httpx ignores the
    ``*_PROXY`` environment variables for these clients.
    """
    return RecordingTransport(httpx.AsyncHTTPTransport(verify=False), monitor)


# ---------------------------------------------------------------------------
# Merging replicas
# ---------------------------------------------------------------------------

_DISABLED_REGISTRY: Dict[str, Any] = {
    "state": "disabled",
    "areas": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_failure_class": None,
}


def disabled_registry_summary() -> Dict[str, Any]:
    return dict(_DISABLED_REGISTRY)


def _nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class _Entry:
    """One replica's validated entry with times parsed. ``None`` from
    ``parse`` means malformed."""

    __slots__ = (
        "configured", "published_at", "outcome", "outcome_at", "failures", "rest_success_at",
        "reg_state", "areas", "reg_success_at", "reg_failure_at", "reg_failure_class",
    )

    @classmethod
    def parse(cls, raw: Any) -> Optional["_Entry"]:
        if not isinstance(raw, Mapping):
            return None
        rest, registry = raw.get("rest"), raw.get("registry")
        if not isinstance(raw.get("configured"), bool) or not isinstance(rest, Mapping) or not isinstance(registry, Mapping):
            return None
        published = _parse_time(raw.get("published_at"))
        if published is None:
            return None
        entry = cls()
        entry.configured = raw["configured"]
        entry.published_at = published
        outcome = rest.get("last_outcome")
        if outcome is not None and not (isinstance(outcome, str) and outcome in OUTCOMES):
            return None
        entry.outcome = outcome
        failures = _nonneg_int(rest.get("consecutive_failures"))
        if failures is None:
            return None
        entry.failures = failures
        times = {}
        for name, source in (
            ("outcome_at", rest.get("last_outcome_at")),
            ("rest_success_at", rest.get("last_success_at")),
            ("reg_success_at", registry.get("last_success_at")),
            ("reg_failure_at", registry.get("last_failure_at")),
        ):
            parsed = _parse_time(source)
            if source is not None and parsed is None:
                return None
            times[name] = parsed
        entry.outcome_at = times["outcome_at"]
        entry.rest_success_at = times["rest_success_at"]
        entry.reg_success_at = times["reg_success_at"]
        entry.reg_failure_at = times["reg_failure_at"]
        state = registry.get("state")
        if not (isinstance(state, str) and state in REGISTRY_STATES):
            return None
        entry.reg_state = state
        areas = registry.get("areas")
        if areas is not None and _nonneg_int(areas) is None:
            return None
        entry.areas = areas
        failure_class = registry.get("last_failure_class")
        if failure_class is not None and not (isinstance(failure_class, str) and failure_class in FAILURE_CLASSES):
            return None
        entry.reg_failure_class = failure_class
        return entry

    def clamp(self, first_seen: datetime, skew: float) -> None:
        """No event time may be later than the entry's own ``published_at`` or
        the reader's first sight of it (plus the skew tolerance)."""
        ceiling = min(self.published_at, first_seen + timedelta(seconds=skew))
        for name in ("outcome_at", "rest_success_at", "reg_success_at", "reg_failure_at"):
            value = getattr(self, name)
            if value is not None and value > ceiling:
                setattr(self, name, ceiling)


def _parse_entry(raw: Any) -> Optional[_Entry]:
    """A validated entry, or None for anything malformed. Whatever a peer (or
    anything with write access to the hash) stored, this never raises."""
    try:
        return _Entry.parse(raw)
    except Exception:
        return None


def _newest(entries: Iterable[_Entry], attribute: str) -> Optional[_Entry]:
    best: Optional[_Entry] = None
    for entry in entries:
        value = getattr(entry, attribute)
        if value is None:
            continue
        if best is None or value > getattr(best, attribute):
            best = entry
    return best


def _latest(entries: Iterable[_Entry], attribute: str) -> Optional[datetime]:
    values = [getattr(e, attribute) for e in entries if getattr(e, attribute) is not None]
    return max(values) if values else None


def merge(
    entries: Mapping[str, Any],
    now: datetime,
    first_seen: Dict[Tuple[str, str], datetime],
    window: float = PEER_WINDOW_SECONDS,
    skew: float = SKEW_TOLERANCE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Combine the replicas' published entries into one view, or None when none
    survive.

    ``first_seen`` is the reader's own map of when it first saw each
    ``(replica, published_at)``; this function adds the pairs it keeps. An
    entry is dropped when malformed, when its ``published_at`` is more than
    ``skew`` seconds ahead of ``now``, or when the reader first saw it more
    than ``window`` seconds ago. A replica's clock can therefore make its own
    events look older, never newer.
    """
    kept = []
    for replica, raw in entries.items():
        entry = _parse_entry(raw)
        if entry is None:
            continue
        try:
            if entry.published_at - now > timedelta(seconds=skew):
                continue
            seen = first_seen.setdefault((replica, entry.published_at.isoformat()), now)
            if (now - seen).total_seconds() > window:
                continue
            entry.clamp(seen, skew)
        except Exception:
            continue
        kept.append(entry)
    if not kept:
        return None

    latest_outcome = _newest(kept, "outcome_at")
    reg_success = _newest(kept, "reg_success_at")
    reg_failure = _newest(kept, "reg_failure_at")
    state = next(s for s in REGISTRY_STATES if any(e.reg_state == s for e in kept))
    return {
        "configured": any(e.configured for e in kept),
        "rest": {
            "last_outcome": latest_outcome.outcome if latest_outcome else None,
            "last_outcome_at": _iso(latest_outcome.outcome_at) if latest_outcome else None,
            "last_success_at": _iso(_latest(kept, "rest_success_at")),
            "consecutive_failures": latest_outcome.failures if latest_outcome else 0,
        },
        "registry": {
            "state": state,
            "areas": reg_success.areas if reg_success else None,
            "last_success_at": _iso(_latest(kept, "reg_success_at")),
            "last_failure_at": _iso(_latest(kept, "reg_failure_at")),
            "last_failure_class": reg_failure.reg_failure_class if reg_failure else None,
        },
        "replicas_reporting": len(kept),
    }


# ---------------------------------------------------------------------------
# The truth table
# ---------------------------------------------------------------------------

_REST_FAILURE_REASON = {
    "forbidden": "forbidden",
    "ha_error": "ha_error",
    "timeout": "timeout",
    "unreachable": "unreachable",
    "tls_error": "tls_error",
    "error": "unreachable",
}


def link_times(view: Mapping[str, Any]) -> Tuple[Optional[datetime], Optional[datetime], bool]:
    """``(success_at, failure_at, failure_from_registry)``: the newest success
    (REST or registry) and the newest failure (REST outcome other than ok, or a
    registry fetch failure), as aware UTC times. The truth table's S and F."""
    rest = view.get("rest") or {}
    registry = view.get("registry") or _DISABLED_REGISTRY
    rest_failed = rest.get("last_outcome") not in (None, OUTCOME_OK)
    failure_at = _parse_time(rest.get("last_outcome_at")) if rest_failed else None
    registry_failure_at = _parse_time(registry.get("last_failure_at"))
    successes = [t for t in (_parse_time(rest.get("last_success_at")), _parse_time(registry.get("last_success_at"))) if t]
    from_registry = False
    if registry_failure_at is not None and (failure_at is None or registry_failure_at > failure_at):
        failure_at, from_registry = registry_failure_at, True
    return (max(successes) if successes else None), failure_at, from_registry


def link_state(view: Mapping[str, Any]) -> Tuple[str, str]:
    """``(state, reason)`` for a view, from the shared truth table. Rows 1-10 of
    the plan are commented in order; the first match wins."""
    if not view.get("configured"):
        return STATE_NOT_CONFIGURED, "not_configured"
    rest = view.get("rest") or {}
    registry = view.get("registry") or _DISABLED_REGISTRY
    success_at, failure_at, failure_from_registry = link_times(view)

    if success_at is None and failure_at is None:
        return STATE_UNKNOWN, "waiting"  # row 2

    failure_newer = failure_at is not None and (success_at is None or failure_at > success_at)
    if failure_newer:
        if failure_from_registry:
            failure_class = registry.get("last_failure_class")
            if failure_class == "auth":
                return STATE_FAILING, "token_rejected"  # row 3
            reason = "timeout" if failure_class == "timeout" else "unreachable"
            if success_at is None:
                return STATE_FAILING, reason  # row 7, no success
            return STATE_ATTENTION, "registry_unavailable"  # row 7
        outcome = rest["last_outcome"]
        if outcome == "auth_rejected":
            return STATE_FAILING, "token_rejected"  # row 3
        reason = _REST_FAILURE_REASON.get(outcome, "unreachable")
        if outcome in ("forbidden", "ha_error"):
            if rest.get("consecutive_failures", 0) < FAILING_AFTER_CONSECUTIVE:
                return STATE_ATTENTION, reason  # row 4
            return STATE_FAILING, reason  # row 5
        return STATE_FAILING, reason  # row 6

    registry_state = registry.get("state")
    if registry_state == "refused":
        return STATE_ATTENTION, "registry_refused"  # row 8
    if registry_state == "unavailable" or (registry_state != "disabled" and registry.get("areas") is None):
        return STATE_ATTENTION, "registry_unavailable"  # row 9
    return STATE_CONNECTED, "ok"  # row 10


def home_assistant_reachable(view: Mapping[str, Any]) -> bool:
    """The ``/health`` bool: the link works, possibly with something to look at."""
    return link_state(view)[0] in HEALTHY_STATES


# ---------------------------------------------------------------------------
# Publishing and the cached view
# ---------------------------------------------------------------------------


def _decode_entry(value: Any) -> Any:
    """The JSON object a stored value holds, or None when it is too large,
    not JSON, or nested too deeply to parse."""
    value = _decode(value)
    if not isinstance(value, str) or len(value.encode("utf-8", "ignore")) > MAX_VALUE_BYTES:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return None


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    return value


class HALinkPublisher:
    """Publishes this replica's entry, merges the cluster's, and serves the
    result from memory.

    ``view()`` is synchronous, does no I/O and never raises. Redis trouble
    degrades it to this replica's own view, tagged ``coverage="replica"``.
    ``configured`` is fixed at construction.
    """

    def __init__(
        self,
        monitor: HALinkMonitor,
        registry: Any,
        redis: Any,
        replica_id: str,
        *,
        configured: bool,
        interval: float = PUBLISH_INTERVAL_SECONDS,
        min_gap: float = PUBLISH_MIN_GAP_SECONDS,
        window: float = PEER_WINDOW_SECONDS,
        wall: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self._monitor = monitor
        self._registry = registry
        self._redis = redis
        self._replica_id = replica_id
        self._configured = configured
        self._interval = interval
        self._min_gap = min_gap
        self._window = window
        self._wall = wall
        self._monotonic = monotonic
        self._sleep = sleep
        self._first_seen: Dict[Tuple[str, str], datetime] = {}
        self._cluster: Optional[Dict[str, Any]] = None
        self._merged_at: Optional[float] = None
        self._redis_failed = False
        self._task: Optional["asyncio.Task[Any]"] = None
        self._closed = False

    # -- reads (no I/O) ---------------------------------------------------

    def _local_entry(self) -> Dict[str, Any]:
        registry = self._registry
        if registry is None:
            registry_summary = disabled_registry_summary()
        else:
            registry_summary = registry.link_summary()
        return {
            "configured": self._configured,
            "rest": self._monitor.summary(),
            "registry": registry_summary,
            "published_at": self._wall().isoformat(),
        }

    def _local_view(self, reason: str) -> Dict[str, Any]:
        entry = self._local_entry()
        return {
            "configured": entry["configured"],
            "rest": entry["rest"],
            "registry": entry["registry"],
            "coverage": COVERAGE_REPLICA,
            "coverage_reason": reason,
            "replicas_reporting": 1,
            "observed_at": entry["published_at"],
        }

    def view(self) -> Dict[str, Any]:
        try:
            if self._redis is None:
                return self._local_view(REASON_REDIS_UNCONFIGURED)
            fresh = (
                self._cluster is not None
                and self._merged_at is not None
                and self._monotonic() - self._merged_at <= STALE_MERGE_INTERVALS * self._interval
            )
            if not fresh:
                return self._local_view(REASON_REDIS_ERROR)
            return {
                **self._cluster,
                "coverage": COVERAGE_CLUSTER,
                "coverage_reason": None,
                "observed_at": self._wall().isoformat(),
            }
        except Exception as exc:
            logger.warning("ha_link_view_failed", error_class=type(exc).__name__)
            return {
                "configured": False,
                "rest": self._empty_rest(),
                "registry": disabled_registry_summary(),
                "coverage": COVERAGE_REPLICA,
                "coverage_reason": REASON_REDIS_ERROR,
                "replicas_reporting": 0,
                "observed_at": utc_now().isoformat(),
            }

    @staticmethod
    def _empty_rest() -> Dict[str, Any]:
        return {"last_outcome": None, "last_outcome_at": None, "last_success_at": None, "consecutive_failures": 0}

    # -- publishing -------------------------------------------------------

    async def publish_once(self) -> None:
        """One cycle: write this replica's entry, read all, merge. Never raises."""
        if self._redis is None:
            return
        local = self._local_entry()
        try:
            await self._redis.hset(REDIS_KEY, self._replica_id, json.dumps(local))
            await self._redis.expire(REDIS_KEY, REDIS_KEY_TTL_SECONDS)
            entries = {}
            for field_name, value in (await self._read_fields()).items():
                entries[str(_decode(field_name))] = _decode_entry(value)
            # This replica's own entry is always known, even if the hash holds
            # more fields than are read.
            entries[self._replica_id] = json.loads(json.dumps(local))
            now = self._wall()
            merged = merge(entries, now, self._first_seen, self._window)
            self._prune_first_seen(entries)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._cluster = None
            if not self._redis_failed:
                logger.warning("ha_link_cluster_view_unavailable", error_class=type(exc).__name__)
            self._redis_failed = True
            return
        if self._redis_failed:
            logger.info("ha_link_cluster_view_recovered")
        self._redis_failed = False
        self._cluster = merged
        self._merged_at = self._monotonic() if merged is not None else None
        await self._remove_old_entries(entries, now)

    async def _read_fields(self) -> Dict[Any, Any]:
        """At most ``MAX_FIELDS`` fields of the hash, by HSCAN, so a hash grown by
        anything with write access can't make every cycle read it all."""
        fields: Dict[Any, Any] = {}
        cursor: Any = 0
        while True:
            cursor, chunk = await self._redis.hscan(REDIS_KEY, cursor, count=MAX_FIELDS)
            for field_name, value in (chunk or {}).items():
                if len(fields) >= MAX_FIELDS:
                    break
                fields[field_name] = value
            if len(fields) >= MAX_FIELDS or str(_decode(cursor)) == "0":
                return fields

    def _prune_first_seen(self, entries: Mapping[str, Any]) -> None:
        live = set()
        for replica, raw in entries.items():
            published = _parse_time(raw.get("published_at")) if isinstance(raw, Mapping) else None
            if published is not None:
                live.add((replica, published.isoformat()))
        for key in [k for k in self._first_seen if k not in live]:
            del self._first_seen[key]

    async def _remove_old_entries(self, entries: Mapping[str, Any], now: datetime) -> None:
        """Delete other replicas' fields that are malformed, unparseable or more
        than an hour old (this replica's own field is rewritten every cycle)."""
        old = []
        for replica, raw in entries.items():
            if replica == self._replica_id:
                continue
            entry = _parse_entry(raw)
            if entry is None or (now - entry.published_at).total_seconds() > PRUNE_AFTER_SECONDS:
                old.append(replica)
        if not old:
            return
        try:
            await self._redis.hdel(REDIS_KEY, *old)
        except Exception as exc:
            logger.warning("ha_link_prune_failed", error_class=type(exc).__name__)

    async def run(self) -> None:
        """Publish on every recorded outcome (at most once per ``min_gap``) and
        at least every ``interval``. Returns at once without Redis."""
        if self._redis is None or self._closed:
            return
        self._task = asyncio.current_task()
        while not self._closed:
            self._monitor.changed.clear()
            await self.publish_once()
            await self._sleep(self._min_gap)
            try:
                await asyncio.wait_for(self._monitor.changed.wait(), self._interval)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._closed = True
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._task = None


def create_publisher(
    monitor: HALinkMonitor,
    registry: Any,
    redis: Any,
    ha_client: Any,
    *,
    replica_id: Optional[str] = None,
) -> HALinkPublisher:
    """The publisher a service builds in its lifespan. ``configured`` is the
    HA client's own answer (a URL and a token), so an install that has no
    Home Assistant reads ``not_configured`` rather than a failure."""
    configured = bool(ha_client is not None and getattr(ha_client, "is_configured", False))
    return HALinkPublisher(
        monitor,
        registry,
        redis,
        replica_id or os.getenv("HOSTNAME") or uuid.uuid4().hex,
        configured=configured,
    )


def empty_view() -> Dict[str, Any]:
    """What a service with no publisher reports."""
    return {
        "configured": False,
        "rest": HALinkPublisher._empty_rest(),
        "registry": disabled_registry_summary(),
        "coverage": COVERAGE_REPLICA,
        "coverage_reason": REASON_REDIS_UNCONFIGURED,
        "replicas_reporting": 0,
        "observed_at": utc_now().isoformat(),
    }
