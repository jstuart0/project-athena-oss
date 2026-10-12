"""Service run-state derivation (ATHENA-118, D2).

Single source of truth for turning a registry row's raw column state
(``enabled``, ``health_status``, optionally a Kubernetes replica count) into
the run-state vocabulary the Service Control page and its API consumers
share: ``running`` | ``stopped`` | ``disabled``.

The poller's health vocabulary is ``healthy`` | ``unconfigured`` |
``unhealthy`` (verified against ``health_poller.py``); any other string
(``None``, a stale/unknown value) is treated as not-healthy so an
unrecognized status never fails open into ``running``.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from shared.config import get_config


def derive_run_state(
    enabled: bool,
    health_status: Optional[str],
    k8s_replicas: Optional[int] = None,
) -> str:
    """Derive the manager-agnostic run state for a registry row (D2).

    | enabled | health_status          | k8s_replicas | run_state  |
    |---------|-------------------------|--------------|------------|
    | False   | any                     | any          | disabled   |
    | True    | any                     | 0            | stopped    |
    | True    | healthy / unconfigured  | None / >=1   | running    |
    | True    | anything else (incl None)| None / >=1  | stopped    |
    """
    if not enabled:
        return 'disabled'
    if k8s_replicas == 0:
        return 'stopped'
    if health_status in ('healthy', 'unconfigured'):
        return 'running'
    return 'stopped'


# Statuses that are neither healthy nor a failure: switched off, never checked,
# not reached by the checker recently, or reachable but not set up ("Needs
# setup"). They are never "needs attention" and never count against the health
# percentage. Only a live unhealthy or degraded row (or a live probe that
# failed) needs attention.
NOT_CHECKED_STATUSES = frozenset({"disabled", "pending", "stale", "unconfigured"})

STALE_GRACE_SECONDS = 30


def stale_after_seconds() -> int:
    """How old a row's last check may be before the checker is presumed not to
    have reached it: two poll intervals, plus the leader-lease TTL (a failover
    pause), plus a cycle's worth of slack. 130 s at the default 30 s interval.
    The interval is read at call time so a config change is honoured."""
    from app.services.health_poller import LEASE_TTL_SECONDS

    return 2 * get_config().health_poll_interval_seconds + LEASE_TTL_SECONDS + STALE_GRACE_SECONDS


def _as_utc(value: datetime) -> datetime:
    """A naive timestamp is UTC (SQLite returns naive values; Postgres aware)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def monitoring_state(
    enabled: bool,
    last_health_check: Optional[datetime],
    *,
    now: datetime,
    stale_after: Optional[int] = None,
) -> str:
    """Whether the background checker is watching a row: ``off`` (disabled),
    ``pending`` (enabled, never checked), ``stale`` (last check older than the
    threshold) or ``live``. An age equal to the threshold is live; a timestamp
    in the future (clock skew) is live."""
    if not enabled:
        return 'off'
    if last_health_check is None:
        return 'pending'
    limit = stale_after_seconds() if stale_after is None else stale_after
    age = (_as_utc(now) - _as_utc(last_health_check)).total_seconds()
    return 'stale' if age > limit else 'live'


def normalized_health_status(
    enabled: bool,
    health_status: Optional[str],
    *,
    monitoring: Optional[str] = None,
) -> str:
    """Normalize a row's raw ``health_status`` for display (shared by
    service_registry.py and the service-control envelope, D3).

    A disabled row always reports ``'disabled'`` regardless of whatever
    value the poller cached before it stopped being polled (ATHENA-112).
    A never-polled enabled row reports ``'pending'`` rather than a raw
    ``None`` so the UI doesn't have to special-case null. With a
    ``monitoring`` state, a row the checker has not reached recently reads
    ``'stale'`` (and a never-checked one ``'pending'``) instead of whatever
    it last cached: that value is a past observation, not the present state.
    """
    if not enabled:
        return 'disabled'
    if health_status is None:
        return 'pending'
    if monitoring in ('stale', 'pending'):
        return monitoring
    return health_status


def display_status(svc: Any, now: datetime) -> Tuple[str, str]:
    """``(status, monitoring)`` for a registry row. The one place a row's
    displayed status is decided; every status-serving read goes through it."""
    monitoring = monitoring_state(bool(svc.enabled), svc.last_health_check, now=now)
    return normalized_health_status(bool(svc.enabled), svc.health_status, monitoring=monitoring), monitoring


def apply_monitoring(d: Dict[str, Any], svc: Any, now: datetime) -> Dict[str, Any]:
    """Set ``monitoring``, the normalized ``health_status`` and
    ``last_observed_health`` (the raw cached value; None when disabled, so an
    off row never shows an old status word) on a row dict."""
    status, monitoring = display_status(svc, now)
    d['monitoring'] = monitoring
    d['health_status'] = status
    d['last_observed_health'] = svc.health_status if svc.enabled else None
    return d
