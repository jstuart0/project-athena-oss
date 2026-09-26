"""Unit tests for gateway LiveKit dependency declaration and visible import
failure (ATHENA-87, F84).

Two silent-failure sites existed before this fix:
1. `livekit_service.py` imports `numpy` unguarded; when it fails, the outer
   `gateway.main` import swallows it and logs at INFO.
2. Even when `numpy` succeeds but the `livekit` SDK itself fails to import,
   `gateway.main.LIVEKIT_ROUTES_AVAILABLE` is still True (the routes mount),
   and `/livekit/config` silently reports `enabled:false` with no log signal
   at all.

Requirements-file assertions (1, 2) are plain text/regex checks — real, no
mocking. Import-behaviour tests run in a subprocess so `sys.modules`
poisoning (`sys.modules["numpy"] = None`) can't leak into the rest of the
suite. Import-time code only *records* `LIVEKIT_IMPORT_ERROR` /
`LIVEKIT_SDK_IMPORT_ERROR` (logging isn't configured yet at that point,
since the routes import happens before `configure_logging("gateway")`).
The ERROR-level events are emitted by `_log_livekit_startup_status`, which
`_start_livekit_integration` calls first, after `configure_logging`. Tests
call it explicitly rather than relying on import order.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"
REQUIREMENTS_IN = SRC_ROOT / "gateway" / "requirements.in"
REQUIREMENTS_TXT = SRC_ROOT / "gateway" / "requirements.txt"

SUBPROCESS_TIMEOUT_S = 30


def _numpy_snippet(mode: str) -> str:
    if mode == "poison":
        return 'sys.modules["numpy"] = None\n'
    if mode == "stub":
        return 'sys.modules["numpy"] = types.ModuleType("numpy")\n'
    raise ValueError(mode)


def _livekit_snippet(mode: str) -> str:
    if mode == "poison":
        return 'sys.modules["livekit"] = None\n'
    if mode == "fake":
        # Every statement here is flat (no nested indentation) because
        # _flatten() strips each physical line's leading whitespace — that's
        # safe inside json.dumps({...}) parens but would break an indented
        # class body, so class definitions use the single-line form.
        lines = [
            '_lk = types.ModuleType("livekit")',
            '_lk_api = types.ModuleType("livekit.api")',
            '_lk_rtc = types.ModuleType("livekit.rtc")',
            'class _FakeAccessToken: pass',
            'class _FakeVideoGrants: pass',
            '_lk_api.AccessToken = _FakeAccessToken',
            '_lk_api.VideoGrants = _FakeVideoGrants',
            '_lk.api = _lk_api',
            '_lk.rtc = _lk_rtc',
            'sys.modules["livekit"] = _lk',
            'sys.modules["livekit.api"] = _lk_api',
            'sys.modules["livekit.rtc"] = _lk_rtc',
        ]
        return "\n".join(lines) + "\n"
    raise ValueError(mode)


def _flatten(text: str) -> str:
    """Strip each physical line's leading/trailing whitespace.

    Safe here because every generated statement is either a flat top-level
    statement or sits inside `json.dumps({...})` parens (where indentation
    is never significant). Lets snippets built at column 0 interpolate
    cleanly into f-strings written with the surrounding test method's
    indentation, without textwrap.dedent's all-lines-must-share-a-prefix
    requirement tripping over the mismatch.
    """
    return "\n".join(line.strip() for line in text.strip("\n").splitlines())


def _run(body: str) -> tuple[dict, list[dict]]:
    """Run `body` in a fresh subprocess after stubbing prometheus_client.

    Returns (result_dict_from_the_RESULT:_line, list_of_json_log_events).
    """
    preamble = 'import sys, json, types, unittest.mock as mock\nsys.modules["prometheus_client"] = mock.MagicMock()'
    code = preamble + "\n" + _flatten(body)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    env["LOG_FORMAT"] = "json"
    env.pop("ATHENA_DEBUG_MODE", None)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )

    result = None
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "event" in obj:
            events.append(obj)

    assert result is not None, (
        f"subprocess produced no RESULT: line (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return result, events


# ---------------------------------------------------------------------------
# 1-2: requirements declaration + lock
# ---------------------------------------------------------------------------

def _normalized_requirement_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("[", "<", ">", "=", ";"):
            idx = line.find(sep)
            if idx != -1:
                line = line[:idx]
        name = line.strip().lower()
        if name:
            names.add(name)
    return names


def test_requirements_in_declares_livekit_stack():
    names = _normalized_requirement_names(REQUIREMENTS_IN.read_text())
    assert {"numpy", "livekit", "livekit-api"} <= names


def test_lock_pins_livekit_stack_with_hashes():
    text = REQUIREMENTS_TXT.read_text()
    lines = text.splitlines()
    for prefix in ("numpy==", "livekit==", "livekit-api=="):
        idx = next((i for i, l in enumerate(lines) if l.startswith(prefix)), None)
        assert idx is not None, f"no line starting with {prefix!r}"
        # At least one --hash=sha256: line follows before the next unindented entry.
        found_hash = False
        for l in lines[idx + 1:]:
            if l.startswith("    --hash=sha256:") or "--hash=sha256:" in l:
                found_hash = True
                continue
            if not l.startswith((" ", "\t")):
                break
        assert found_hash, f"no --hash=sha256: line following {prefix!r}"


# ---------------------------------------------------------------------------
# 3-7: import-behaviour and startup-status logging (subprocess isolation)
# ---------------------------------------------------------------------------

def test_routes_import_failure_logged_at_error():
    body = f"""
        {_numpy_snippet("poison")}
        {_livekit_snippet("fake")}
        import gateway.main as m
        m._log_livekit_startup_status(livekit_enabled=False)
        print("RESULT:" + json.dumps({{
            "routes_available": m.LIVEKIT_ROUTES_AVAILABLE,
            "import_error": m.LIVEKIT_IMPORT_ERROR,
        }}))
    """
    result, events = _run(body)

    assert result["routes_available"] is False
    assert result["import_error"]
    assert "numpy" in result["import_error"]

    matching = [e for e in events if e.get("event") == "livekit_routes_import_failed"]
    assert len(matching) == 1
    assert matching[0].get("level") == "error"
    assert "numpy" in matching[0].get("error", "")


def test_sdk_import_failure_logged_at_error():
    body = f"""
        {_numpy_snippet("stub")}
        {_livekit_snippet("poison")}
        import gateway.main as m
        import gateway.livekit_service as lks
        m._log_livekit_startup_status(livekit_enabled=False)
        print("RESULT:" + json.dumps({{
            "sdk_available": lks.LIVEKIT_AVAILABLE,
            "sdk_import_error": lks.LIVEKIT_SDK_IMPORT_ERROR,
        }}))
    """
    result, events = _run(body)

    assert result["sdk_available"] is False
    assert result["sdk_import_error"]
    assert "livekit" in result["sdk_import_error"]

    matching = [e for e in events if e.get("event") == "livekit_sdk_import_failed"]
    assert len(matching) == 1
    assert matching[0].get("level") == "error"
    assert "livekit" in matching[0].get("error", "")


def test_numpy_present_livekit_missing_routes_mount_but_disabled():
    body = f"""
        {_numpy_snippet("stub")}
        {_livekit_snippet("poison")}
        import gateway.main as m
        import gateway.livekit_service as lks
        from fastapi.testclient import TestClient

        client = TestClient(m.app)
        resp = client.get("/livekit/config")

        m._log_livekit_startup_status(livekit_enabled=False)

        print("RESULT:" + json.dumps({{
            "routes_available": m.LIVEKIT_ROUTES_AVAILABLE,
            "sdk_available": lks.LIVEKIT_AVAILABLE,
            "service_is_available": lks.get_livekit_service().is_available,
            "config_status": resp.status_code,
            "config_enabled": resp.json().get("enabled"),
        }}))
    """
    result, events = _run(body)

    assert result["routes_available"] is True
    assert result["sdk_available"] is False
    assert result["service_is_available"] is False
    assert result["config_status"] == 200
    assert result["config_enabled"] is False

    matching = [e for e in events if e.get("event") == "livekit_sdk_import_failed"]
    assert len(matching) == 1
    assert matching[0].get("level") == "error"


def test_livekit_routes_available_when_both_present():
    body = f"""
        {_numpy_snippet("stub")}
        {_livekit_snippet("fake")}
        import gateway.main as m
        import gateway.livekit_service as lks
        m._log_livekit_startup_status(livekit_enabled=False)
        print("RESULT:" + json.dumps({{
            "routes_available": m.LIVEKIT_ROUTES_AVAILABLE,
            "sdk_available": lks.LIVEKIT_AVAILABLE,
            "import_error": m.LIVEKIT_IMPORT_ERROR,
            "sdk_import_error": lks.LIVEKIT_SDK_IMPORT_ERROR,
        }}))
    """
    result, events = _run(body)

    assert result["routes_available"] is True
    assert result["sdk_available"] is True
    assert result["import_error"] is None
    assert result["sdk_import_error"] is None

    error_events = [e for e in events if e.get("level") == "error"]
    assert error_events == []
    assert [e for e in events if e.get("event") == "livekit_routes_import_failed"] == []
    assert [e for e in events if e.get("event") == "livekit_sdk_import_failed"] == []


@pytest.mark.parametrize(
    "numpy_mode,livekit_mode,case",
    [
        ("stub", "poison", "sdk_missing"),
        ("stub", "fake", "both_present"),
    ],
    ids=["sdk_missing", "both_present"],
)
def test_flag_on_startup_logs_sdk_status_before_gated_init(numpy_mode, livekit_mode, case):
    body = f"""
        {_numpy_snippet(numpy_mode)}
        {_livekit_snippet(livekit_mode)}
        import asyncio
        import inspect
        import gateway.main as m

        spy = mock.AsyncMock()
        m.initialize_livekit_integration = spy

        asyncio.run(m._start_livekit_integration(livekit_enabled=True))

        print("RESULT:" + json.dumps({{
            "init_awaited": spy.await_count,
            "wiring_ok": "_start_livekit_integration(" in inspect.getsource(m.lifespan),
        }}))
    """
    result, events = _run(body)

    assert result["wiring_ok"] is True

    error_events = [e for e in events if e.get("level") == "error"]
    initialized_events = [e for e in events if "LiveKit WebRTC integration initialized" in str(e.get("event", ""))]

    if case == "sdk_missing":
        matching = [e for e in events if e.get("event") == "livekit_sdk_import_failed"]
        assert len(matching) == 1
        assert matching[0].get("level") == "error"
        assert result["init_awaited"] == 0
        assert initialized_events == []
    else:
        assert result["init_awaited"] == 1
        assert error_events == []
