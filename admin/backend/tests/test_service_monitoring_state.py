"""Whether the background health checker is watching a registry row.

`monitoring_state` decides off | pending | stale | live from the row's
`enabled` flag and `last_health_check`, against a threshold derived from the
poll interval and the leader lease. Time is injected; nothing sleeps.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.utils import service_state as ss
from shared.config import get_config

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
APP = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _config_cache():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def _state(enabled, ago_seconds, *, threshold=130):
    last = None if ago_seconds is None else NOW - timedelta(seconds=ago_seconds)
    return ss.monitoring_state(enabled, last, now=NOW, stale_after=threshold)


# -- the threshold ----------------------------------------------------------

def test_the_threshold_is_two_intervals_plus_the_lease_plus_thirty_seconds(monkeypatch):
    from app.services.health_poller import LEASE_TTL_SECONDS

    monkeypatch.setenv("HEALTH_POLL_INTERVAL_SECONDS", "30")
    get_config.cache_clear()
    assert ss.stale_after_seconds() == 2 * 30 + LEASE_TTL_SECONDS + 30 == 130
    monkeypatch.setenv("HEALTH_POLL_INTERVAL_SECONDS", "40")
    get_config.cache_clear()
    assert ss.stale_after_seconds() == 150


def test_the_threshold_follows_a_config_override_and_is_not_cached(monkeypatch):
    monkeypatch.setenv("HEALTH_POLL_INTERVAL_SECONDS", "10")
    get_config.cache_clear()
    assert ss.stale_after_seconds() == 90


# -- the four states ----------------------------------------------------------

@pytest.mark.parametrize(
    "enabled,ago,expected",
    [
        (True, 0, "live"),
        (True, 129, "live"),
        (True, 130, "live"),      # equal to the threshold is live
        (True, 131, "stale"),     # one second past it is stale
        (True, 100000, "stale"),
        (True, None, "pending"),  # enabled and never checked
        (False, None, "off"),     # disabled and never checked
        (False, 5, "off"),        # disabled wins over a fresh check
        (False, 100000, "off"),
        (True, -60, "live"),      # a future timestamp (clock skew) is live
    ],
)
def test_monitoring_state_boundaries(enabled, ago, expected):
    assert _state(enabled, ago) == expected


def test_a_naive_and_an_aware_timestamp_for_the_same_instant_agree():
    aware = NOW - timedelta(seconds=200)
    naive = aware.replace(tzinfo=None)
    assert ss.monitoring_state(True, aware, now=NOW, stale_after=130) == "stale"
    assert ss.monitoring_state(True, naive, now=NOW, stale_after=130) == "stale"
    fresh_naive = (NOW - timedelta(seconds=10)).replace(tzinfo=None)
    assert ss.monitoring_state(True, fresh_naive, now=NOW, stale_after=130) == "live"
    assert ss.monitoring_state(True, fresh_naive, now=NOW.replace(tzinfo=None), stale_after=130) == "live"


# -- the display status -------------------------------------------------------

def _row(enabled=True, health="healthy", ago=10):
    return SimpleNamespace(
        enabled=enabled, health_status=health,
        last_health_check=None if ago is None else NOW - timedelta(seconds=ago),
    )


@pytest.mark.parametrize(
    "row,status,monitoring",
    [
        (_row(), "healthy", "live"),
        (_row(health="unhealthy"), "unhealthy", "live"),
        (_row(health="unconfigured"), "unconfigured", "live"),
        (_row(ago=100000), "stale", "stale"),
        (_row(health="unhealthy", ago=100000), "stale", "stale"),
        (_row(ago=None), "pending", "pending"),
        (_row(health=None, ago=None), "pending", "pending"),
        (_row(enabled=False), "disabled", "off"),
        (_row(enabled=False, health="unhealthy", ago=100000), "disabled", "off"),
    ],
)
def test_display_status_and_apply_monitoring(row, status, monitoring):
    assert ss.display_status(row, NOW) == (status, monitoring)
    d = ss.apply_monitoring({"health_status": row.health_status, "enabled": row.enabled}, row, NOW)
    assert (d["health_status"], d["monitoring"]) == (status, monitoring)
    assert d["last_observed_health"] == (row.health_status if row.enabled else None)


def test_run_state_still_reads_the_raw_column():
    assert ss.derive_run_state(True, "healthy") == "running"
    assert ss.derive_run_state(False, "healthy") == "disabled"


# -- the writers store aware UTC ---------------------------------------------

WRITERS = ["app/services/health_poller.py", "app/routes/service_registry.py", "app/routes/services.py"]


@pytest.mark.parametrize("relpath", WRITERS)
def test_the_last_health_check_writers_use_aware_utc(relpath):
    tree = ast.parse((APP / relpath).read_text(encoding="utf-8"))
    naive = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "utcnow"]
    assert naive == [], f"{relpath} still calls utcnow() at lines {naive}"
    now_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "now" and n.args
    ]
    assert now_calls, f"{relpath} has no datetime.now(<tz>) call"


# -- P2 drift guards ------------------------------------------------------------

def test_the_display_functions_are_called_only_from_service_state():
    offenders = []
    for root in (APP / "app" / "routes", APP / "app" / "services", APP / "app" / "utils"):
        for path in root.rglob("*.py"):
            if path.name == "service_state.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if name in {"normalized_health_status", "monitoring_state"}:
                        offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    main_tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in {
            "normalized_health_status", "monitoring_state"}:
            offenders.append(f"main.py:{node.lineno}")
    assert offenders == []


# Raw reads of RagService.health_status that are allowed, with the reason each exists.
RAW_HEALTH_STATUS_READS = {
    # audit old_value (:service.health_status), the envelope's run_state, the single-service response
    "app/routes/service_control.py": 3,
    # CRUD: the status filter, the check route's write key and response
    "app/routes/services.py": 3,
    # the per-row check route's write key
    "app/routes/service_registry.py": 1,
    # the poller's own write key
    "app/services/health_poller.py": 1,
}
# A different model with its own health_status column.
_OTHER_MODELS = {"app/routes/cloud_providers.py"}


def raw_health_status_reads(source: str) -> int:
    return sum(1 for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute) and n.attr == "health_status")


def _scan():
    found = {}
    files = [*(APP / "app" / "routes").rglob("*.py"), *(APP / "app" / "services").rglob("*.py"), APP / "main.py"]
    for path in files:
        rel = path.relative_to(APP).as_posix()
        if rel in _OTHER_MODELS:
            continue
        count = raw_health_status_reads(path.read_text(encoding="utf-8"))
        if count:
            found[rel] = count
    return found


def test_raw_health_status_reads_are_exactly_the_reasoned_allowlist():
    found = _scan()
    assert len(found) >= 4
    assert found == RAW_HEALTH_STATUS_READS, (
        "a status-serving read of RagService.health_status must go through "
        "service_state.apply_monitoring/display_status; add to the allowlist only with a reason"
    )


def test_a_new_raw_read_is_detected():
    assert raw_health_status_reads("def f(svc):\n    return svc.health_status == 'healthy'\n") == 1
    assert raw_health_status_reads("def f(svc):\n    return svc.name\n") == 0
