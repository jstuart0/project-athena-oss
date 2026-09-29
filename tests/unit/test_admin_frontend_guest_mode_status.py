"""guest-mode.js status-panel helpers, executed in Node (vm.runInThisContext,
since the file is a plain browser <script> whose top-level declarations only
become globals outside a CommonJS wrapper). Node runs with TZ=UTC so the
formatted times depend only on the property timezone passed in."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "admin" / "frontend"
NODE_BIN = shutil.which("node")


def _eval(expression: str):
    # Fail, don't skip, without node: a skipped behaviour test is a silent green.
    assert NODE_BIN, "node is required on PATH for the guest-mode frontend tests"
    script = f"""
    'use strict';
    const vm = require('vm');
    const fs = require('fs');
    global.window = global;
    global.document = {{ getElementById: () => null, createElement: () => ({{}}) }};
    for (const f of ['escape-html.js', 'guest-mode.js']) {{
        vm.runInThisContext(fs.readFileSync({json.dumps(str(FRONTEND))} + '/' + f, 'utf8'), {{ filename: f }});
    }}
    process.stdout.write(JSON.stringify(vm.runInThisContext({json.dumps(expression)})));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15,
                          env={**os.environ, "TZ": "UTC"})
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


_REASON = "Active booking: lodgify #12 (until 2026-07-05T15:00:00+00:00)"


def test_blocked_status_has_a_neutral_style():
    assert _eval("getStatusClass('blocked')") == "bg-gray-800 text-gray-400"


@pytest.mark.parametrize("zone,valid,expected", [
    ("Asia/Tokyo", True, "(until Jul 6, 12:00 AM)"),
    ("America/New_York", True, "(until Jul 5, 11:00 AM)"),
    ("Mars/Olympus", False, "(until Jul 5, 3:00 PM)"),
])
def test_reason_checkout_is_shown_in_the_property_timezone(zone, valid, expected):
    status = {"reachable": True, "property_timezone": zone, "property_timezone_valid": valid}
    formatted = _eval(f"_gmFormatReason({json.dumps(_REASON)}, {json.dumps(status)})")
    assert formatted.endswith(expected), formatted


def test_invalid_mode_service_url_has_its_own_sentence():
    message = _eval("_gmUnreachableMessage({reachable: false, error: 'mode_service_url_invalid'})")
    assert "MODE_SERVICE_URL" in message
    assert "isn't a valid" in message


_SOURCES = {
    "admin": {"status": "fresh", "required": True},
    "ical": {"status": "never_loaded", "required": False},
}


def _warnings(sources, calendar_url_set):
    status = {"reachable": True, "mode": "owner", "bookings_status": "fresh", "bookings_sources": sources}
    options = {"guestModeEnabled": True, "hasCurrentGuests": False, "calendarUrlSet": calendar_url_set}
    return _eval(f"_modeStatusWarnings({json.dumps(status)}, {json.dumps(options)})")


@pytest.mark.parametrize("advisory_status", ["never_loaded", "expired"])
def test_unusable_advisory_calendar_url_is_warned_about(advisory_status):
    sources = {**_SOURCES, "ical": {"status": advisory_status, "required": False}}
    warnings = _warnings(sources, calendar_url_set=True)
    assert len(warnings) == 1
    assert "legacy iCal URL" in warnings[0]


def test_no_advisory_warning_without_a_calendar_url_or_when_fresh():
    assert _warnings(_SOURCES, calendar_url_set=False) == []
    fresh = {**_SOURCES, "ical": {"status": "stale", "required": False}}
    assert _warnings(fresh, calendar_url_set=True) == []
