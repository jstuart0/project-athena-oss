"""Drift guard: every process-TZ clock read in src/ and apps/ is accounted for.

A naive ``datetime.now()``, ``datetime.today()``, ``date.today()``,
``datetime.utcnow()``, ``datetime.utcfromtimestamp(...)`` or a
``datetime.fromtimestamp(x)`` without a zone reads the process ``TZ`` (or
returns a naive UTC value that's easy to mistake for local). Wall-clock
decisions ("today", prompt clocks, date windows, "wait until") must use
``shared.local_time`` instead. The sites that stay are durations, cache ages,
record timestamps and log-file names, and each is on the counted ALLOWLIST
below with a reason. A new site anywhere, including inside an allowlisted
function, fails until it's switched or classified.

Stdlib only: this runs on the unit-min CI requirements.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "apps")
SKIP = {
    "src/jetson/athena_lite_llm.py": "PEP 701 f-string at :134; not a runtime image, parse fails on 3.11",
}

_RECEIVERS = {"datetime", "date"}

# (path, enclosing function) -> (count, reason)
DURATION = "duration / cache age / TTL: elapsed-time arithmetic between values from the same clock"
RECORD = "record timestamp: stored or reported as an instant, not a local wall-clock decision"
LOGFILES = "log files: date in a log-file name, or an mtime age filter with both sides in the process zone"
ONECALL = "onecall forecast time: the right zone is the forecast location's (ticketed follow-up), not DEFAULT_TIMEZONE"
ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("apps/jarvis-web/backend/main.py", "chat"): (2, DURATION),
    ("apps/jarvis-web/backend/main.py", "get_or_create_identity"): (1, DURATION),
    ("apps/jarvis-web/backend/main.py", "record_message"): (2, RECORD),
    ("src/control_agent/main.py", "_service_log_files"): (3, LOGFILES),
    ("src/control_agent/main.py", "health_check"): (1, RECORD),
    ("src/control_agent/main.py", "hf_import_to_ollama"): (1, RECORD),
    ("src/control_agent/main.py", "list_log_files"): (3, LOGFILES),
    ("src/control_agent/main.py", "ollama_health"): (2, RECORD),
    ("src/control_agent/main.py", "ollama_status"): (2, RECORD),
    ("src/control_agent/main.py", "restart_container"): (1, RECORD),
    ("src/control_agent/main.py", "restart_ollama"): (2, RECORD),
    ("src/control_agent/main.py", "restart_process"): (2, RECORD),
    ("src/control_agent/main.py", "search_logs"): (2, LOGFILES),
    ("src/control_agent/main.py", "start_container"): (1, RECORD),
    ("src/control_agent/main.py", "start_ollama"): (2, RECORD),
    ("src/control_agent/main.py", "start_process"): (1, RECORD),
    ("src/control_agent/main.py", "start_process_by_port"): (1, RECORD),
    ("src/control_agent/main.py", "stop_container"): (1, RECORD),
    ("src/control_agent/main.py", "stop_ollama"): (2, RECORD),
    ("src/control_agent/main.py", "stop_process"): (1, RECORD),
    ("src/control_agent/main.py", "watchdog_loop"): (3, RECORD),
    ("src/gateway/device_session_manager.py", "_cleanup_loop"): (1, DURATION),
    ("src/gateway/device_session_manager.py", "get_session_for_device"): (2, DURATION),
    ("src/gateway/device_session_manager.py", "update_session_for_device"): (3, DURATION),
    ("src/orchestrator/config_loader.py", "_get_from_cache"): (1, DURATION),
    ("src/orchestrator/config_loader.py", "_set_to_cache"): (1, DURATION),
    ("src/orchestrator/ha_entity_manager.py", "get_entities"): (1, DURATION),
    ("src/orchestrator/ha_entity_manager.py", "refresh_entities"): (1, DURATION),
    ("src/orchestrator/helpers.py", "prepare_openai_session"): (1, DURATION),
    ("src/orchestrator/music_handler.py", "get_account_for_room"): (1, RECORD),
    ("src/orchestrator/music_handler.py", "transfer_assignment"): (1, RECORD),
    ("src/orchestrator/self_building_tools.py", "get_stats"): (1, RECORD),
    ("src/orchestrator/self_building_tools.py", "to_dict"): (1, RECORD),
    ("src/orchestrator/session_manager.py", "__init__"): (2, DURATION),
    ("src/orchestrator/session_manager.py", "add_message"): (2, DURATION),
    ("src/orchestrator/session_manager.py", "get_or_create_session"): (1, DURATION),
    ("src/orchestrator/session_manager.py", "is_expired"): (1, DURATION),
    ("src/rag/base_rag_service.py", "load_configuration"): (1, DURATION),
    ("src/rag/community_events/main.py", "cache_events"): (2, DURATION),
    ("src/rag/community_events/main.py", "scrape_event_cards_source"): (1, RECORD),
    ("src/rag/community_events/main.py", "scrape_link_scan_source"): (1, RECORD),
    ("src/rag/community_events/main.py", "scrape_squarespace_eventlist_source"): (1, RECORD),
    ("src/rag/community_events/main.py", "scrape_tribe_events_api_source"): (1, RECORD),
    ("src/rag/media/main.py", "get_requests"): (1, RECORD),
    ("src/rag/media/main.py", "health"): (1, RECORD),
    ("src/rag/onecall/main.py", "format_daily_forecast"): (1, ONECALL),
    ("src/rag/onecall/main.py", "format_hourly_forecast"): (1, ONECALL),
    ("src/rag/transportation/main.py", "load_gtfs_data"): (1, RECORD),
    ("src/shared/errors.py", "athena_exception_handler"): (1, RECORD),
    ("src/shared/errors.py", "create_error_response"): (1, RECORD),
    ("src/shared/errors.py", "generic_exception_handler"): (1, RECORD),
    ("src/shared/events.py", "to_dict"): (1, RECORD),
    ("src/shared/logging_config.py", "configure_logging"): (1, LOGFILES),
}


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_marker(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or _receiver_name(func.value) not in _RECEIVERS:
        return False
    no_args = not call.args and not call.keywords
    if func.attr in ("now", "today"):
        return no_args
    if func.attr in ("utcnow", "utcfromtimestamp"):
        return True
    if func.attr == "fromtimestamp":
        return len(call.args) < 2 and not any(k.arg == "tz" for k in call.keywords)
    return False


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[tuple[str, int]] = []

    def _visit_function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if _is_marker(node):
            self.found.append((self.stack[-1] if self.stack else "<module>", node.lineno))
        self.generic_visit(node)


def find_clock_reads(source: str, filename: str = "<string>") -> list[tuple[str, int]]:
    finder = _Finder()
    finder.visit(ast.parse(source, filename=filename))
    return finder.found


def _scan_files() -> list[Path]:
    files = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = path.relative_to(REPO_ROOT).parts
            if "tests" in parts or "node_modules" in parts:
                continue
            files.append(path)
    return files


def scan() -> tuple[Counter, int, list[str]]:
    found: Counter = Counter()
    parsed = 0
    lines: list[str] = []
    for path in _scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SKIP:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            hits = find_clock_reads(source, rel)
        except SyntaxError as exc:
            raise AssertionError(f"{rel} doesn't parse ({exc}); fix it or add it to SKIP with a reason") from exc
        parsed += 1
        for func, lineno in hits:
            found[(rel, func)] += 1
            lines.append(f"{rel}:{lineno} ({func})")
    return found, parsed, lines


def test_clock_reads_match_the_allowlist():
    found, parsed, lines = scan()
    assert parsed >= 150, f"only {parsed} files parsed; the scan roots moved"
    assert sum(found.values()) >= 60, "the detector found implausibly few sites"
    expected = Counter({key: count for key, (count, _reason) in ALLOWLIST.items()})
    assert sum(expected.values()) >= 60
    new = found - expected
    gone = expected - found
    assert not new and not gone, (
        "process-TZ clock reads drifted from the allowlist.\n"
        f"New (switch to shared.local_time, or classify with a reason): {dict(new)}\n"
        f"Gone (drop from ALLOWLIST): {dict(gone)}\n" + "\n".join(lines)
    )
    assert ("src/shared/base_knowledge_utils.py", "resolve_dynamic_value") not in found


def test_every_allowlist_entry_has_a_reason():
    for key, (count, reason) in ALLOWLIST.items():
        assert count >= 1 and reason.strip(), key


def test_skip_entries_still_fail_to_parse():
    for rel in SKIP:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:
            continue
        raise AssertionError(f"{rel} parses now; remove it from SKIP so it's scanned")


def test_clock_detector_self_test():
    source = '''
import datetime as dt
from datetime import datetime, date, timezone

def flagged(t):
    datetime.now()
    datetime.utcnow()
    datetime.fromtimestamp(t)
    date.today()
    datetime.datetime.now()

def not_flagged(t, utc):
    datetime.now(timezone.utc)
    datetime.fromtimestamp(t, tz=utc)
    datetime.fromtimestamp(t, utc)
    dt.now()
'''
    assert find_clock_reads(source) == [
        ("flagged", 6), ("flagged", 7), ("flagged", 8), ("flagged", 9), ("flagged", 10),
    ]
