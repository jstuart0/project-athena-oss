"""ATHENA-110: Control Agent mutating routes require X-Service-Key;
exec-safe process launch; config-file dir/cmd containment.

Prior state (xander F2, 2026-09-27-operate-athena-dashboard-registry-
cleanup): zero incoming auth on any Control Agent route -- anyone on the
LAN could stop Ollama or any managed process. `cmd`/`dir` from the
services file reached `create_subprocess_shell` via a joined string with
no metacharacter/containment checks (`dir: "/etc"` or `dir: "../../etc"`
escaped PROJECT_ROOT).

This suite drives the real FastAPI app (`src/control_agent/main.py`)
through `TestClient`, mocking only the actual subprocess/docker/ollama/hf
side effects -- the auth dependency, the config-load validation, and the
exec-safe launch path are exercised for real.
"""
from __future__ import annotations

import importlib.util
import json
import stat as stat_module
import sys
import types
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_CONTROL_AGENT_DIR = _SRC / "control_agent"
for _p in (_SRC, _CONTROL_AGENT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

_MAIN_PATH = _CONTROL_AGENT_DIR / "main.py"

_GOOD_KEY = "test-service-key-athena-110"

_SERVICES_FILE = {
    "processes": {
        "8000": {
            "name": "example-gateway",
            "dir": "src/gateway",
            "cmd": ["python", "-m", "http.server", "8000"],
        },
    },
    "watchdog_exclude": [],
    "containers": ["athena-example"],
}


def _install_fake_huggingface_module():
    """Stand in for src/control_agent/huggingface.py -- main.py's hf_*
    routes `from huggingface import X` locally (call-time, not
    import-time), so pre-seeding sys.modules['huggingface'] intercepts
    every one of those without needing the real HF Hub client."""
    fake = types.ModuleType("huggingface")
    fake.search_models = AsyncMock(return_value=[])
    fake.get_repo_files = AsyncMock(return_value=[])
    fake.start_download = AsyncMock(return_value="job-123")
    fake.get_download_status = MagicMock(return_value=types.SimpleNamespace(
        job_id="job-123", status="pending", progress_percent=0.0,
        downloaded_bytes=0, total_bytes=0, error=None,
    ))
    fake.cancel_download = MagicMock(return_value=True)
    fake.import_to_ollama = AsyncMock(return_value=(True, "imported"))
    fake.list_downloaded_models = MagicMock(return_value=[])
    fake.delete_downloaded_model = MagicMock(return_value=(True, "deleted"))
    sys.modules["huggingface"] = fake
    return fake


def _import_control_agent(unique_name: str, monkeypatch, *, service_key, services_path):
    """Fresh-import main.py under a private module name (it's literally
    named main.py, the same collision every RAG service's own main.py
    hits)."""
    _install_fake_huggingface_module()

    if service_key is None:
        monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SERVICE_API_KEY", service_key)
    monkeypatch.setenv("CONTROL_AGENT_SERVICES_FILE", str(services_path))
    monkeypatch.delenv("ALLOWED_CALLBACK_HOSTS", raising=False)

    if unique_name in sys.modules:
        module = sys.modules[unique_name]
        module.__spec__.loader.exec_module(module)
    else:
        spec = importlib.util.spec_from_file_location(unique_name, _MAIN_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)

    # Reset the one-time startup-warning flag every fresh import so each
    # test observes its own warn-or-not-warn behaviour independently.
    import auth as auth_module
    auth_module.reset_for_test()

    return module


def _client_for(monkeypatch, *, service_key, tmp_path, services_file=None):
    payload = services_file if services_file is not None else _SERVICES_FILE
    services_path = tmp_path / f"services-{id(payload)}-{service_key}.json"
    services_path.write_text(json.dumps(payload))
    module = _import_control_agent(
        f"_test_ca_auth_{tmp_path.name}_{service_key}_{id(payload)}",
        monkeypatch,
        service_key=service_key,
        services_path=services_path,
    )

    # Neutralise real subprocess/docker/lsof calls -- this suite is about
    # auth and validation, not process management itself.
    monkeypatch.setattr(module, "get_pid_by_port", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "run_docker_command", AsyncMock(return_value=(True, "ok")))

    return module, TestClient(module.app)


# ---------------------------------------------------------------------------
# Every mutating route: 401 missing key / 401 wrong key / 503 unset key /
# succeeds with the correct key.
# ---------------------------------------------------------------------------

_MUTATING_ROUTES = [
    ("post", "/docker/start/athena-example"),
    ("post", "/docker/stop/athena-example"),
    ("post", "/docker/restart/athena-example"),
    ("post", "/ollama/restart"),
    ("post", "/ollama/start"),
    ("post", "/ollama/stop"),
    ("post", "/process/stop/8000"),
    ("post", "/process/start/8000"),
    ("post", "/process/restart/8000"),
    ("post", "/huggingface/download"),
    ("delete", "/huggingface/download/job-123"),
    ("post", "/huggingface/import-to-ollama"),
    ("delete", "/huggingface/downloaded?file_path=/tmp/x.gguf"),
    ("post", "/watchdog/enable"),
    ("post", "/watchdog/disable"),
    ("post", "/watchdog/exclude/8000"),
    ("post", "/watchdog/include/8000"),
]


def _request_kwargs(path: str) -> dict:
    if path == "/huggingface/download":
        return {"json": {"repo_id": "org/model", "filename": "model.gguf"}}
    if path == "/huggingface/import-to-ollama":
        return {"json": {"gguf_path": "/tmp/x.gguf", "model_name": "custom"}}
    return {}


@pytest.mark.parametrize("method,path", _MUTATING_ROUTES)
def test_mutating_route_401_without_key(monkeypatch, tmp_path, method, path):
    _module, client = _client_for(monkeypatch, service_key=_GOOD_KEY, tmp_path=tmp_path)
    resp = getattr(client, method)(path, **_request_kwargs(path))
    assert resp.status_code == 401, (path, resp.status_code, resp.text)


@pytest.mark.parametrize("method,path", _MUTATING_ROUTES)
def test_mutating_route_401_with_wrong_key(monkeypatch, tmp_path, method, path):
    _module, client = _client_for(monkeypatch, service_key=_GOOD_KEY, tmp_path=tmp_path)
    resp = getattr(client, method)(
        path, headers={"X-Service-Key": "totally-wrong-key"}, **_request_kwargs(path)
    )
    assert resp.status_code == 401, (path, resp.status_code, resp.text)


@pytest.mark.parametrize("method,path", _MUTATING_ROUTES)
def test_mutating_route_503_when_key_unset(monkeypatch, tmp_path, method, path):
    _module, client = _client_for(monkeypatch, service_key=None, tmp_path=tmp_path)
    resp = getattr(client, method)(
        path, headers={"X-Service-Key": "anything"}, **_request_kwargs(path)
    )
    assert resp.status_code == 503, (path, resp.status_code, resp.text)
    assert "service key not configured" in resp.json()["detail"]


@pytest.mark.parametrize("method,path", _MUTATING_ROUTES)
def test_mutating_route_succeeds_with_correct_key(monkeypatch, tmp_path, method, path):
    module, client = _client_for(monkeypatch, service_key=_GOOD_KEY, tmp_path=tmp_path)

    # Simplest stand-in for "the service actually started" (used by
    # /process/start and /process/restart's post-start PID check).
    async def _fake_get_pid(port):
        return 4242
    monkeypatch.setattr(module, "get_pid_by_port", AsyncMock(side_effect=_fake_get_pid))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(return_value=None))

    resp = getattr(client, method)(
        path, headers={"X-Service-Key": _GOOD_KEY}, **_request_kwargs(path)
    )
    assert resp.status_code in (200, 201), (path, resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# Read-only routes stay open -- no key required, no 401/503 regardless.
# ---------------------------------------------------------------------------

_READ_ONLY_ROUTES = [
    ("get", "/health"),
    ("get", "/docker/list"),
    ("get", "/docker/status/athena-example"),
    ("get", "/process/list"),
    ("get", "/process/status/8000"),
    ("get", "/watchdog/status"),
]


@pytest.mark.parametrize("method,path", _READ_ONLY_ROUTES)
def test_read_only_route_open_without_key(monkeypatch, tmp_path, method, path):
    _module, client = _client_for(monkeypatch, service_key=_GOOD_KEY, tmp_path=tmp_path)
    resp = getattr(client, method)(path)
    assert resp.status_code not in (401, 503), (path, resp.status_code, resp.text)


@pytest.mark.parametrize("method,path", _READ_ONLY_ROUTES)
def test_read_only_route_open_when_key_unset(monkeypatch, tmp_path, method, path):
    _module, client = _client_for(monkeypatch, service_key=None, tmp_path=tmp_path)
    resp = getattr(client, method)(path)
    assert resp.status_code not in (401, 503), (path, resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# One-time startup warning when SERVICE_API_KEY is unset.
# ---------------------------------------------------------------------------

def test_startup_warning_logged_once_when_key_unset(monkeypatch, tmp_path):
    module, _client = _client_for(monkeypatch, service_key=None, tmp_path=tmp_path)
    import auth as auth_module

    warnings = []
    monkeypatch.setattr(auth_module.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))

    auth_module.warn_if_service_key_unset()
    auth_module.warn_if_service_key_unset()
    auth_module.warn_if_service_key_unset()

    unset_warnings = [w for w in warnings if w[0] and w[0][0] == "control_agent_service_key_unset"]
    assert len(unset_warnings) == 1


def test_startup_warning_not_logged_when_key_set(monkeypatch, tmp_path):
    _module, _client = _client_for(monkeypatch, service_key=_GOOD_KEY, tmp_path=tmp_path)
    import auth as auth_module

    warnings = []
    monkeypatch.setattr(auth_module.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))

    auth_module.warn_if_service_key_unset()

    assert warnings == []


# ---------------------------------------------------------------------------
# Exec-safe launch: create_subprocess_exec, not create_subprocess_shell,
# receives an argv list and cwd -- never a joined shell string.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_process_by_port_uses_exec_not_shell(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps(_SERVICES_FILE))
    module = _import_control_agent(
        f"_test_ca_execsafe_{tmp_path.name}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )

    monkeypatch.setattr(module, "get_pid_by_port", AsyncMock(side_effect=[None, 4242]))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(return_value=None))

    captured = {}

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", _fake_exec)
    # If a regression reintroduces a shell string, this must never be called.
    monkeypatch.setattr(
        module.asyncio, "create_subprocess_shell",
        AsyncMock(side_effect=AssertionError("create_subprocess_shell must not be used")),
    )

    success, message = await module.start_process_by_port(8000)

    assert success is True, message
    # argv list, not a single shell string. `python` is rewritten to the
    # venv's explicit interpreter path by start_process_by_port itself.
    expected_python = str(module.PROJECT_ROOT / ".venv" / "bin" / "python")
    assert list(captured["args"]) == [expected_python, "-m", "http.server", "8000"]
    assert all(isinstance(a, str) for a in captured["args"])
    assert captured["kwargs"]["cwd"] == str(module.PROJECT_ROOT / "src" / "gateway")
    assert captured["kwargs"]["stderr"] == module.asyncio.subprocess.STDOUT


# ---------------------------------------------------------------------------
# cmd metacharacter rejection at config-load time.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_char", [";", "&", "|", "$", "`", "<", ">", "\n"])
def test_cmd_metacharacter_rejected(monkeypatch, tmp_path, bad_char, caplog):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps({
        "processes": {
            "8000": {
                "name": "evil",
                "dir": "src/gateway",
                "cmd": ["python", f"-m{bad_char}rm -rf /"],
            },
        },
    }))
    module = _import_control_agent(
        f"_test_ca_metachar_{tmp_path.name}_{ord(bad_char)}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )
    assert module.PROCESS_SERVICES == {}
    assert module.is_port_allowed(8000) is False


def test_cmd_without_metacharacters_accepted(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps(_SERVICES_FILE))
    module = _import_control_agent(
        f"_test_ca_metachar_clean_{tmp_path.name}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )
    assert module.is_port_allowed(8000) is True


# ---------------------------------------------------------------------------
# `dir` containment: absolute paths, `..` traversal, and paths that
# resolve outside PROJECT_ROOT are all rejected at config-load time.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_dir", ["/etc", "../../etc", "src/../../etc", "..", "/"])
def test_dir_containment_rejected(monkeypatch, tmp_path, bad_dir):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps({
        "processes": {
            "8000": {
                "name": "escape-attempt",
                "dir": bad_dir,
                "cmd": ["python", "-m", "http.server", "8000"],
            },
        },
    }))
    module = _import_control_agent(
        f"_test_ca_dircontain_{tmp_path.name}_{abs(hash(bad_dir))}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )
    assert module.PROCESS_SERVICES == {}
    assert module.is_port_allowed(8000) is False


def test_dir_inside_project_root_accepted(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps(_SERVICES_FILE))
    module = _import_control_agent(
        f"_test_ca_dircontain_ok_{tmp_path.name}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )
    assert module.is_port_allowed(8000) is True
    assert module.PROCESS_SERVICES[8000]["dir"] == "src/gateway"


# ---------------------------------------------------------------------------
# Writable services-file warning: logged, but the file still loads.
# ---------------------------------------------------------------------------

def test_group_writable_services_file_logs_warning(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps(_SERVICES_FILE))
    services_path.chmod(0o664)  # group-writable

    module = _import_control_agent(
        f"_test_ca_writable_{tmp_path.name}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )

    warnings = []
    monkeypatch.setattr(module.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))
    module.load_control_agent_config()

    matching = [w for w in warnings if w[0] and w[0][0] == "control_agent_services_file_writable"]
    assert len(matching) == 1
    # Still loads -- warn, don't refuse.
    assert module.is_port_allowed(8000) is True


def test_owner_only_services_file_no_warning(monkeypatch, tmp_path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps(_SERVICES_FILE))
    services_path.chmod(0o600)

    module = _import_control_agent(
        f"_test_ca_notwritable_{tmp_path.name}", monkeypatch,
        service_key=_GOOD_KEY, services_path=services_path,
    )

    warnings = []
    monkeypatch.setattr(module.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))
    module.load_control_agent_config()

    matching = [w for w in warnings if w[0] and w[0][0] == "control_agent_services_file_writable"]
    assert matching == []
