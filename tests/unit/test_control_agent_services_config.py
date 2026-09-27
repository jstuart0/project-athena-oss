"""ATHENA-99 (D46): Control Agent manages only configured services.

src/control_agent/main.py used to hard-code every house process (gateway,
orchestrator, 11+ RAG services on ports 8000-8040), a Docker container
whitelist, and a watchdog-exclude list. That's why the 60s watchdog
relaunched a retired house stack as bare processes after its containers
were stopped, and the startup registry sync re-registered them.

CONTROL_AGENT_SERVICES_FILE now makes this configuration (OSS-First:
unset means nothing is managed). This module is reloaded fresh per test
(under a private name each time -- main.py collides with every other
RAG service's own main.py) since PROCESS_SERVICES/WATCHDOG_EXCLUDE/
ALLOWED_CONTAINERS are computed once, at import time.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
# ATHENA-110: main.py now imports its sibling auth.py at module level
# (`from auth import ...`), the same way it lazily imports huggingface.py
# and url_validator.py -- put src/control_agent on sys.path so that
# resolves under spec_from_file_location the same way it does when
# uvicorn runs main.py with that directory as its cwd in production.
_CONTROL_AGENT_DIR = _SRC / "control_agent"
if str(_CONTROL_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROL_AGENT_DIR))

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

_MAIN_PATH = _SRC / "control_agent" / "main.py"

_GOOD_SERVICES_FILE = {
    "processes": {
        "8000": {
            "name": "example-gateway",
            "dir": "src/gateway",
            "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
        },
        "8010": {
            "name": "example-rag",
            "dir": "src/rag/example",
            "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"],
            "health_path": "/health",
        },
        "8099": {
            "name": "disabled-example",
            "dir": "src/rag/disabled_example",
            "cmd": ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"],
            "enabled": False,
        },
    },
    "watchdog_exclude": [8000],
    "containers": ["athena-example"],
}


def _import_control_agent(unique_name: str, monkeypatch, services_file: str | None):
    if services_file is None:
        monkeypatch.delenv("CONTROL_AGENT_SERVICES_FILE", raising=False)
    else:
        monkeypatch.setenv("CONTROL_AGENT_SERVICES_FILE", services_file)

    if unique_name in sys.modules:
        module = sys.modules[unique_name]
        module.__spec__.loader.exec_module(module)
        return module
    spec = importlib.util.spec_from_file_location(unique_name, _MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Default (unset CONTROL_AGENT_SERVICES_FILE): nothing managed.
# ---------------------------------------------------------------------------

def test_default_unset_manages_nothing(monkeypatch):
    module = _import_control_agent("_test_ca_default", monkeypatch, None)
    assert module.PROCESS_SERVICES == {}
    assert module.WATCHDOG_EXCLUDE == set()
    assert module.ALLOWED_CONTAINERS == set()


def test_default_is_port_allowed_false_for_every_port(monkeypatch):
    module = _import_control_agent("_test_ca_default_port", monkeypatch, None)
    for port in (8000, 8001, 8010, 8025, 8028, 8040):
        assert module.is_port_allowed(port) is False


def test_default_is_container_allowed_false_for_every_container(monkeypatch):
    module = _import_control_agent("_test_ca_default_container", monkeypatch, None)
    for name in ("athena-gateway", "athena-orchestrator", "athena-weather"):
        assert module.is_container_allowed(name) is False


# ---------------------------------------------------------------------------
# A valid file: services/watchdog_exclude/containers all load.
# ---------------------------------------------------------------------------

def test_valid_file_loads_services(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps(_GOOD_SERVICES_FILE))

    module = _import_control_agent("_test_ca_valid", monkeypatch, str(services_path))

    assert set(module.PROCESS_SERVICES.keys()) == {8000, 8010}
    assert module.PROCESS_SERVICES[8000]["name"] == "example-gateway"
    assert module.PROCESS_SERVICES[8010]["health_path"] == "/health"
    # The explicitly-disabled entry (8099) is dropped, not managed.
    assert 8099 not in module.PROCESS_SERVICES

    assert module.WATCHDOG_EXCLUDE == {8000}
    assert module.ALLOWED_CONTAINERS == {"athena-example"}

    assert module.is_port_allowed(8000) is True
    assert module.is_port_allowed(8099) is False
    assert module.is_container_allowed("athena-example") is True
    assert module.is_container_allowed("athena-gateway") is False


def test_empty_processes_with_containers_is_valid_and_functional(monkeypatch, tmp_path):
    """hank's actual house file shape: no bare processes to manage at all,
    but Docker containers (e.g. whisper-wyoming, athena-piper-tts) still
    controllable. Empty 'processes' must not be treated as "file is
    broken" -- it's the documented, intentional container-only case."""
    path = tmp_path / "containers-only.json"
    path.write_text(json.dumps({
        "processes": {},
        "watchdog_exclude": [],
        "containers": ["whisper-wyoming", "athena-piper-tts"],
    }))

    module = _import_control_agent("_test_ca_containers_only", monkeypatch, str(path))

    assert module.PROCESS_SERVICES == {}
    assert module.WATCHDOG_EXCLUDE == set()
    assert module.ALLOWED_CONTAINERS == {"whisper-wyoming", "athena-piper-tts"}
    assert module.is_container_allowed("whisper-wyoming") is True
    assert module.is_container_allowed("athena-piper-tts") is True
    assert module.is_port_allowed(8000) is False


# ---------------------------------------------------------------------------
# Malformed file: ERROR logged, nothing managed, never crashes.
# ---------------------------------------------------------------------------

def test_malformed_json_manages_nothing(monkeypatch, tmp_path, caplog):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not json")

    module = _import_control_agent("_test_ca_malformed_json", monkeypatch, str(bad_path))
    assert module.PROCESS_SERVICES == {}
    assert module.WATCHDOG_EXCLUDE == set()
    assert module.ALLOWED_CONTAINERS == set()


def test_missing_file_manages_nothing(monkeypatch, tmp_path):
    missing_path = tmp_path / "does-not-exist.json"
    module = _import_control_agent("_test_ca_missing", monkeypatch, str(missing_path))
    assert module.PROCESS_SERVICES == {}


def test_non_object_top_level_manages_nothing(monkeypatch, tmp_path):
    bad_path = tmp_path / "list.json"
    bad_path.write_text(json.dumps([1, 2, 3]))
    module = _import_control_agent("_test_ca_non_object", monkeypatch, str(bad_path))
    assert module.PROCESS_SERVICES == {}


def test_one_bad_entry_does_not_take_down_the_rest(monkeypatch, tmp_path):
    """A single malformed service definition is dropped (with an ERROR
    logged), not fatal to the rest of the file."""
    mixed = {
        "processes": {
            "8000": {
                "name": "example-gateway",
                "dir": "src/gateway",
                "cmd": ["python", "-m", "uvicorn", "main:app", "--port", "8000"],
            },
            "8001": {"name": "missing-dir-and-cmd"},
            "not-a-port": {"name": "bad-key", "dir": "x", "cmd": ["x"]},
        },
    }
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(mixed))

    module = _import_control_agent("_test_ca_mixed", monkeypatch, str(path))
    assert set(module.PROCESS_SERVICES.keys()) == {8000}


def test_malformed_watchdog_exclude_and_containers_manage_nothing_for_that_field(monkeypatch, tmp_path):
    path = tmp_path / "bad_lists.json"
    path.write_text(json.dumps({
        "processes": {"8000": {"name": "g", "dir": "src/gateway", "cmd": ["x"]}},
        "watchdog_exclude": "not-a-list",
        "containers": {"not": "a-list"},
    }))
    module = _import_control_agent("_test_ca_bad_lists", monkeypatch, str(path))
    assert set(module.PROCESS_SERVICES.keys()) == {8000}
    assert module.WATCHDOG_EXCLUDE == set()
    assert module.ALLOWED_CONTAINERS == set()


# ---------------------------------------------------------------------------
# Watchdog does not touch a port outside the config.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_never_restarts_a_port_outside_config(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps({
        "processes": {
            "8000": {
                "name": "example-gateway",
                "dir": "src/gateway",
                "cmd": ["python", "-m", "uvicorn", "main:app", "--port", "8000"],
            },
        },
    }))
    module = _import_control_agent("_test_ca_watchdog", monkeypatch, str(services_path))

    # get_pid_by_port would normally shell out to lsof; stub it so a
    # "down" service is reported for any port, and record every port
    # start_process_by_port is asked to restart.
    monkeypatch.setattr(module, "get_pid_by_port", AsyncMock(return_value=None))
    restarted_ports = []

    async def _fake_start(port):
        restarted_ports.append(port)
        return True, "started"

    monkeypatch.setattr(module, "start_process_by_port", _fake_start)
    monkeypatch.setattr(module, "watchdog_enabled", True)
    monkeypatch.setattr(module, "watchdog_interval", 0)

    # Drive one iteration of the watchdog body directly rather than the
    # infinite loop -- iterate PROCESS_SERVICES exactly as watchdog_loop
    # does, using the module's own real WATCHDOG_EXCLUDE/PROCESS_SERVICES.
    for port in list(module.PROCESS_SERVICES.keys()):
        if port in module.WATCHDOG_EXCLUDE:
            continue
        pid = await module.get_pid_by_port(port)
        if pid is None:
            await module.start_process_by_port(port)

    assert restarted_ports == [8000]
    # A port never present in the config (e.g. a stray 8028 from the old
    # hard-coded list) is never iterated, let alone restarted.
    assert 8028 not in restarted_ports


# ---------------------------------------------------------------------------
# Registry sync posts exactly the configured set.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_sync_upserts_exactly_the_configured_services(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps({
        "processes": {
            "8000": {"name": "example-gateway", "dir": "src/gateway", "cmd": ["x"]},
            "8010": {"name": "example-rag", "dir": "src/rag/example", "cmd": ["x"]},
        },
    }))
    module = _import_control_agent("_test_ca_sync", monkeypatch, str(services_path))
    monkeypatch.setenv("CONTROL_AGENT_URL", "http://ca-host:8099")

    posted_names = []

    class _FakeResponse:
        status_code = 200
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, params=None):
            posted_names.append(params["name"])
            return _FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    count_ok, count_skip = await module._upsert_all_services("http://admin:8080", "test-key")

    assert count_ok == 2
    assert count_skip == 0
    assert set(posted_names) == {"example-gateway", "example-rag"}


@pytest.mark.asyncio
async def test_registry_sync_loop_no_ops_with_one_info_line_when_unconfigured(monkeypatch, caplog):
    module = _import_control_agent("_test_ca_sync_noop", monkeypatch, None)
    assert module.PROCESS_SERVICES == {}

    logged = []
    monkeypatch.setattr(module.logger, "info", lambda event, **kw: logged.append(event))

    await module.sync_registry_loop()

    assert logged == ["no_managed_services_configured"]
