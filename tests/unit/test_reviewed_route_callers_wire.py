"""The service key on the wire, for every file that calls one of the
reviewed admin-backend routes.

``test_admin_guest_route_callers_send_service_key.py`` proves the source
text. This proves the request: real httpx request building, faked only at
the socket with ``httpx.MockTransport``, and the recorded ``httpx.Request``
inspected. It also pins what each caller does when admin-backend refuses it
(401/403/503): one request, its documented default, no exception, and one
ERROR ``admin_backend_refused`` per route and status per minute, carrying
the route's template and never the concrete URL or the key.

``src/shared/admin_config.py`` is covered against the real app in
``admin/backend/tests/test_admin_config_real_seam.py``.

Two environments run this file. jarvis-web's ``main.py`` imports only in
the jarvis-web environment (its own requirements, no ``shared.config``);
every other caller imports only where ``shared.config`` does. A case whose
environment isn't the current one is skipped with that reason, so each CI
job runs its own cases and a laptop with both runs them all.

Every test that drives a caller carries that caller's id, which starts with
``area_shared__``, ``area_orchestrator__`` or ``area_edge__``; ``-k`` on one
of those selects everything for that area.
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import importlib.util
import json
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
JARVIS_BACKEND = REPO_ROOT / "apps" / "jarvis-web" / "backend"

# SERVICE_API_KEY is deliberately not set here: each test sets its own, so a
# module that captured the key when it was imported sends something else and
# W2 fails on it.
os.environ.setdefault("ADMIN_API_URL", "http://admin-backend:8080")  # same value as ADMIN_URL below

from shared import service_key  # noqa: E402

WIRE_KEY = "wire-test-key"
# What the key is changed to once a caller has been imported and set up: a
# caller that kept the value it saw at import (or at construction) sends
# WIRE_KEY and fails W2.
ROTATED_KEY = "wire-test-key-rotated"
ADMIN_URL = "http://admin-backend:8080"
# Module constants that hold the admin URL as it was when the module was
# first imported (possibly by another test file, with no URL configured).
_ADMIN_URL_CONSTANTS = ("ADMIN_API_URL", "ADMIN_BACKEND_URL", "ADMIN_INTERNAL_URL")
_REAL_ASYNC_CLIENT = httpx.AsyncClient
_HAS_SHARED_CONFIG = importlib.util.find_spec("pydantic_settings") is not None
_HAS_JARVIS_DEPS = importlib.util.find_spec("sqlalchemy") is not None

SCANNER = Path(__file__).with_name("test_admin_guest_route_callers_send_service_key.py")
MATRIX_TEST = REPO_ROOT / "admin" / "backend" / "tests" / "test_reviewed_routes_auth.py"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Transport:
    """Answers the case's own paths from a script (the last entry repeats)
    and everything else with an empty 200. Records every request."""

    def __init__(self, paths):
        self.paths = set(paths)
        self.requests: list[httpx.Request] = []
        self.script: list[tuple[int, Any]] = [(200, {})]
        self.error: Callable[[httpx.Request], Exception] | None = None
        self._served = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path not in self.paths:
            return httpx.Response(200, json={})
        if self.error is not None:
            raise self.error(request)
        status, body = self.script[min(self._served, len(self.script) - 1)]
        self._served += 1
        return httpx.Response(status, json=body)

    @property
    def targeted(self):
        return [r for r in self.requests if r.url.path in self.paths]

    def client(self, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("verify", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self.handler), **kwargs)


class Harness:
    def __init__(self, monkeypatch, transport):
        self.monkeypatch = monkeypatch
        self.transport = transport

    def load(self, name):
        # orchestrator.helpers can't be the first orchestrator import (it is
        # imported through main in production too).
        if name.startswith("orchestrator.") and name != "orchestrator.main":
            importlib.import_module("orchestrator.main")
        return self.with_admin_url(importlib.import_module(name))

    def with_admin_url(self, module):
        for constant in _ADMIN_URL_CONSTANTS:
            if hasattr(module, constant):
                self.monkeypatch.setattr(module, constant, ADMIN_URL)
        return module


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@dataclasses.dataclass(frozen=True)
class WireCase:
    file: str
    function: str
    requests: tuple            # ((METHOD, path), ...) the call makes, in order
    routes: tuple              # the static route template of each request
    ok: tuple                  # (status, json) a healthy admin-backend answers
    setup: Callable            # (Harness) -> (async call, check_default)
    label: str = ""
    env: str = "shared_config"
    # 503 is left out for a route that answers 503 for its own reasons.
    refusal_statuses: tuple = (401, 403, 503)

    @property
    def area(self):
        if self.file.startswith("src/shared/"):
            return "shared"
        if self.file.startswith("src/orchestrator/"):
            return "orchestrator"
        return "edge"

    @property
    def id(self):
        suffix = f".{self.label}" if self.label else ""
        return f"area_{self.area}__{self.file}::{self.function}{suffix}"


WIRE_CASES: list[WireCase] = []


def wire_case(file, function, *, requests, routes, ok=(200, {}), **kwargs):
    def register(setup):
        WIRE_CASES.append(WireCase(
            file=file, function=function, requests=tuple(requests), routes=tuple(routes),
            ok=ok, setup=setup, **kwargs,
        ))
        return setup
    return register


def _case(file, function, label=""):
    (found,) = [c for c in WIRE_CASES if (c.file, c.function, c.label) == (file, function, label)]
    return found


# ---------------------------------------------------------------------------
# The cases: one or more per caller file
# ---------------------------------------------------------------------------

def _ollama_fallback(config):
    from shared.config import get_config

    assert config["backend_type"] == "ollama"
    assert config["endpoint_url"] == get_config().ollama_url
    assert config["max_tokens"] == 2048
    assert config["timeout_seconds"] == 60


BACKEND_ROWS = [{"model_name": "m1", "backend_type": "mlx", "endpoint_url": "http://mlx.example:8080"}]


@wire_case("src/shared/llm_router.py", "_get_backend_config",
           requests=[("GET", "/api/llm-backends/public")], routes=["/api/llm-backends/public"],
           ok=(200, BACKEND_ROWS))
def _llm_backend_config(h):
    router = h.load("shared.llm_router").LLMRouter(admin_url="http://admin", persist_metrics=False)

    async def call():
        return await router._get_backend_config("m1")

    return call, _ollama_fallback


@wire_case("src/shared/llm_router.py", "_track_cloud_usage",
           requests=[("POST", "/api/cloud-llm-usage")], routes=["/api/cloud-llm-usage"])
def _llm_cloud_usage(h):
    router = h.load("shared.llm_router").LLMRouter(admin_url="http://admin", persist_metrics=False)

    async def call():
        return await router._track_cloud_usage(
            provider="openai", model="gpt-x", input_tokens=1, output_tokens=2, cost_usd=0.01, latency_ms=5)

    def default(result):
        assert result is None

    return call, default


@wire_case("src/shared/service_registry.py", "get_service_url",
           requests=[("GET", "/api/service-registry/services/zz-service/url")],
           routes=["/api/service-registry/services/{service_name}/url"],
           ok=(200, {"url": "http://zz.example:8010"}), refusal_statuses=(401, 403))
def _service_url(h):
    module = h.load("shared.service_registry")

    async def call():
        module._url_cache.clear()
        module._cache_time.clear()
        return await module.get_service_url("zz-service")

    def default(result):
        assert result is None

    return call, default


@wire_case("src/shared/tool_registry.py", "_get_mcp_security",
           requests=[("GET", "/api/mcp-security/public")], routes=["/api/mcp-security/public"],
           ok=(200, {"allowed_domains": ["example.org"], "blocked_domains": []}))
def _mcp_security(h):
    registry = object.__new__(h.load("shared.tool_registry").UnifiedToolRegistry)

    async def call():
        return await registry._get_mcp_security()

    def default(result):
        assert result == {
            "allowed_domains": ["localhost", "127.0.0.1"], "blocked_domains": [],
            "max_execution_time_ms": 30000, "max_concurrent_tools": 5,
        }

    return call, default


ENGINE_ROW = [{"engine_id": "e1", "host": "voice.example", "port": 10300}]


@wire_case("src/shared/voice_config.py", "_load_engines",
           requests=[("GET", "/api/voice-interfaces/engines/public/stt"),
                     ("GET", "/api/voice-interfaces/engines/public/tts")],
           routes=["/api/voice-interfaces/engines/public/stt", "/api/voice-interfaces/engines/public/tts"],
           ok=(200, ENGINE_ROW))
def _voice_engines(h):
    manager = object.__new__(h.load("shared.voice_config").VoiceConfigManager)

    async def call():
        manager._stt_engines, manager._tts_engines = {}, {}
        await manager._load_engines()
        return manager

    def default(result):
        assert result._stt_engines == {} and result._tts_engines == {}

    return call, default


@wire_case("src/orchestrator/helpers.py", "get_feature_config",
           requests=[("GET", "/api/features/public")], routes=["/api/features/public"],
           ok=(200, [{"name": "zz_flag", "enabled": True, "config": {"a": 1}}]))
def _feature_config(h):
    helpers = h.load("orchestrator.helpers")
    runtime = h.load("orchestrator.nodes._runtime")

    async def call():
        runtime.get_orch_feature_flag_cache().clear()
        return await helpers.get_feature_config("zz_flag")

    def default(result):
        assert result == {"enabled": False, "config": {}}

    return call, default


@wire_case("src/orchestrator/main.py", "get_feature_flag",
           requests=[("GET", "/api/features/public")], routes=["/api/features/public"],
           ok=(200, [{"name": "zz_flag", "enabled": False}]))
def _orchestrator_feature_flag(h):
    main = h.load("orchestrator.main")

    async def call():
        main._orch_feature_flag_cache.clear()
        return await main.get_feature_flag("zz_flag", default=True)

    def default(result):
        assert result is True  # its `default` argument, not the flag's value

    return call, default


@wire_case("src/orchestrator/self_building_tools.py", "_save_proposal",
           requests=[("POST", "/api/tool-proposals")], routes=["/api/tool-proposals"],
           ok=(201, {"id": 1}))
def _save_proposal(h):
    module = h.load("orchestrator.self_building_tools")
    manager = object.__new__(module.SelfBuildingToolsManager)
    manager.admin_url = "http://admin"
    manager._service_api_key = "stale-constructor-key"
    proposal = module.ToolProposal(
        id="p1", name="zz_tool", description="d", trigger_phrases=["t"], workflow_definition={"nodes": []})

    async def call():
        return await manager._save_proposal(proposal)

    def default(result):
        assert result is None

    return call, default


@wire_case("src/orchestrator/smart_home_controller.py", "_create_stuck_sensor_alert",
           requests=[("POST", "/api/alerts/public/create")], routes=["/api/alerts/public/create"],
           ok=(200, {"id": 1}))
def _stuck_sensor_alert(h):
    controller = object.__new__(h.load("orchestrator.smart_home_controller").SmartHomeController)
    sensor = {"room": "office", "friendly_name": "Office motion", "state": "on", "hours_unchanged": 30.0,
              "last_changed": "2026-01-01T00:00:00+00:00", "entity_id": "binary_sensor.office_motion"}

    async def call():
        return await controller._create_stuck_sensor_alert(sensor)

    def default(result):
        assert result is None

    return call, default


@wire_case("src/orchestrator/tv_handler.py", "get_tv_configs",
           requests=[("GET", "/api/room-tv/internal")], routes=["/api/room-tv/internal"],
           ok=(200, [{"room_name": "Den", "media_player_entity_id": "media_player.den"}]))
def _tv_configs(h):
    module = h.load("orchestrator.tv_handler")

    async def call():
        module._tv_config_cache, module._tv_config_cache_time = {}, 0
        return await module.get_tv_configs()

    def default(result):
        assert result == {
            name: {"room_name": name, "media_player_entity_id": entities[0], "remote_entity_id": entities[1]}
            for name, entities in module._get_fallback_room_to_tv().items()
        }
        assert "den" not in result

    return call, default


@wire_case("src/orchestrator/music_handler.py", "get_room_configs",
           requests=[("GET", "/api/room-audio/internal")], routes=["/api/room-audio/internal"],
           ok=(200, [{"room_name": "Den", "primary_entity_id": "media_player.den"}]))
def _room_configs(h):
    module = h.load("orchestrator.music_handler")

    async def call():
        module._room_config_cache, module._room_config_cache_time = {}, 0
        return await module.get_room_configs()

    def default(result):
        assert result == {
            name: {"room_name": name, "primary_entity_id": entity}
            for name, entity in module._get_fallback_room_to_player().items()
        }
        assert "den" not in result

    return call, default


@wire_case("src/gateway/main.py", "get_feature_flag",
           requests=[("GET", "/api/features/public")], routes=["/api/features/public"],
           ok=(200, [{"name": "zz_flag", "enabled": False}]))
def _gateway_feature_flag(h):
    gateway = h.load("gateway.main")

    async def call():
        gateway._feature_flag_cache.clear()
        return await gateway.get_feature_flag("zz_flag", default=True)

    def default(result):
        assert result is True

    return call, default


def _metric_call(gateway):
    async def call():
        return await gateway._log_metric_to_db(
            timestamp=1.0, model="m1", backend="ollama", latency_seconds=0.5, tokens=10, tokens_per_second=20.0)

    def default(result):
        assert result is None

    return call, default


@wire_case("src/gateway/main.py", "_log_metric_to_db", label="shared_client",
           requests=[("POST", "/api/llm-backends/metrics")], routes=["/api/llm-backends/metrics"],
           ok=(201, {}))
def _gateway_metric_shared_client(h):
    gateway = h.load("gateway.main")
    h.monkeypatch.setattr(gateway, "metric_client", h.transport.client(timeout=5.0))
    return _metric_call(gateway)


@wire_case("src/gateway/main.py", "_log_metric_to_db", label="fallback_client",
           requests=[("POST", "/api/llm-backends/metrics")], routes=["/api/llm-backends/metrics"],
           ok=(201, {}))
def _gateway_metric_fallback_client(h):
    gateway = h.load("gateway.main")
    h.monkeypatch.setattr(gateway, "metric_client", None)
    return _metric_call(gateway)


def _follow_up_flag_case(instance):
    instance._follow_ups_enabled = True
    instance._feature_flag_check_interval = 60.0

    async def call():
        instance._last_feature_flag_check = 0.0
        await instance._refresh_feature_flags()
        return instance

    def default(result):
        assert result._follow_ups_enabled is True  # unchanged

    return call, default


FOLLOW_UPS_OFF = [{"name": "ai_follow_ups_enabled", "enabled": False}]


@wire_case("src/gateway/livekit_service.py", "_refresh_feature_flags",
           requests=[("GET", "/api/features/public")], routes=["/api/features/public"],
           ok=(200, FOLLOW_UPS_OFF))
def _livekit_flags(h):
    return _follow_up_flag_case(object.__new__(h.load("gateway.livekit_service").LiveKitService))


_WYOMING_MODULES = (
    "wyoming", "wyoming.server", "wyoming.event", "wyoming.audio", "wyoming.asr", "wyoming.tts",
    "wyoming.info", "wyoming.handle",
)


class _AnyNameModule(types.ModuleType):
    """`from <this> import Name` gives an empty class for any Name."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (), {})


@pytest.fixture(scope="module")
def wyoming_bridge():
    """gateway.wyoming_bridge with its handler class defined. The class body
    only exists when the `wyoming` package imports, and that package is in
    the gateway image, not in this environment; where it's missing the
    module is loaded once with empty stand-ins for the wyoming names. The
    method under test never touches them."""
    real = importlib.import_module("gateway.wyoming_bridge")
    if getattr(real, "WYOMING_AVAILABLE", False):
        return real
    saved = {name: sys.modules.get(name) for name in _WYOMING_MODULES}
    sys.modules.update({name: _AnyNameModule(name) for name in _WYOMING_MODULES})
    try:
        spec = importlib.util.spec_from_file_location("_wire_wyoming_bridge", _SRC / "gateway" / "wyoming_bridge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    assert module.WYOMING_AVAILABLE is True
    return module


@wire_case("src/gateway/wyoming_bridge.py", "_refresh_feature_flags",
           requests=[("GET", "/api/features/public")], routes=["/api/features/public"],
           ok=(200, FOLLOW_UPS_OFF))
def _wyoming_flags(h):
    return _follow_up_flag_case(object.__new__(h.with_admin_url(h.wyoming_bridge).AthenaWyomingHandler))


_jarvis_main_module = None


def _jarvis_main():
    global _jarvis_main_module
    if _jarvis_main_module is None:
        if str(JARVIS_BACKEND) not in sys.path:
            sys.path.insert(0, str(JARVIS_BACKEND))
        spec = importlib.util.spec_from_file_location("_wire_jarvis_web_main", JARVIS_BACKEND / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["_wire_jarvis_web_main"] = module
        spec.loader.exec_module(module)
        _jarvis_main_module = module
    return _jarvis_main_module


@wire_case("apps/jarvis-web/backend/main.py", "get_persistent_sessions_config", env="jarvis_web",
           requests=[("GET", "/api/features/public")], routes=["/api/features/public"],
           ok=(200, [{"name": "persistent_chat_sessions", "enabled": True, "config": {"a": 1}}]))
def _jarvis_persistent_sessions(h):
    main = h.with_admin_url(_jarvis_main())
    # jarvis-web keeps its key in a module constant (it has no shared.config).
    h.monkeypatch.setattr(main, "SERVICE_API_KEY", os.environ.get("SERVICE_API_KEY", ""))
    h.monkeypatch.setattr(main, "DATABASE_URL", "postgresql://zz")

    async def call():
        main._feature_cache, main._feature_cache_time = {}, 0.0
        return await main.get_persistent_sessions_config()

    def default(result):
        assert result is None

    return call, default


@wire_case("apps/jarvis-web/backend/main.py", "get_room_tv_configs", env="jarvis_web",
           requests=[("GET", "/api/room-tv/internal")], routes=["/api/room-tv/internal"],
           ok=(200, []))
def _jarvis_room_tv(h):
    main = h.with_admin_url(_jarvis_main())
    h.monkeypatch.setattr(main, "SERVICE_API_KEY", os.environ.get("SERVICE_API_KEY", ""))

    async def call():
        return await main.get_room_tv_configs()

    def default(result):
        assert result == {}

    return call, default


@wire_case("src/rag/directions/main.py", "lifespan",
           requests=[("GET", "/api/directions-settings/public")], routes=["/api/directions-settings/public"],
           ok=(200, {"default_travel_mode": "walking"}))
def _directions_lifespan(h):
    module = h.load("rag.directions.main")

    async def _noop(*args, **kwargs):
        return None

    class _Cache:
        def __init__(self, *args, **kwargs):
            pass

        connect = disconnect = _noop

    class _Admin:
        get_external_api_key = close = _noop

    h.monkeypatch.setattr(module, "startup_service", _noop)
    h.monkeypatch.setattr(module, "unregister_service", _noop)
    h.monkeypatch.setattr(module, "CacheClient", _Cache)
    h.monkeypatch.setattr(module, "get_admin_client", lambda: _Admin())
    module.load_default_settings()
    defaults = dict(module.SETTINGS)
    assert defaults and defaults != {"default_travel_mode": "walking"}

    async def call():
        h.monkeypatch.setattr(module, "SETTINGS", {"zz": "stale"})
        async with module.lifespan(module.app):
            return dict(module.SETTINGS)

    def default(result):
        assert result == defaults

    return call, default


PROGRESS_ROUTE = "/api/model-downloads/internal/{download_id}/progress"


@wire_case("src/control_agent/huggingface.py", "send_progress_callback",
           requests=[("POST", "/api/model-downloads/internal/7/progress")], routes=[PROGRESS_ROUTE])
def _progress_callback(h):
    module = h.load("control_agent.huggingface")
    # The agent sends its key only to a host on its own list.
    h.monkeypatch.setenv("ALLOWED_CALLBACK_HOSTS", "admin")

    async def call():
        return await module.send_progress_callback(
            "http://admin:8080/api/model-downloads", 7, "downloading", progress_percent=12.5,
            downloaded_bytes=1024)

    def default(result):
        assert result is None

    return call, default


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _clear_config_caches():
    if _HAS_SHARED_CONFIG:
        from shared.admin_url import _clear_cache_for_tests as clear_admin_url
        from shared.config import _clear_cache_for_tests as clear_config

        clear_config()
        clear_admin_url()


def _reset_refusal_state():
    service_key._reset_for_tests()
    # The Control Agent host gets no shared module; its once-per-minute
    # state is inline and resets through this hook.
    agent = sys.modules.get("control_agent.huggingface")
    hook = getattr(agent, "_reset_callback_log_state_for_tests", None)
    if hook is not None:
        hook()
    admin_config = sys.modules.get("shared.admin_config")
    if admin_config is not None:
        admin_config._admin_client = None


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", WIRE_KEY)
    monkeypatch.setenv("ADMIN_API_URL", ADMIN_URL)
    monkeypatch.setattr(service_key, "_clock", lambda: 1000.0)
    _clear_config_caches()
    _reset_refusal_state()
    yield
    _reset_refusal_state()
    _clear_config_caches()


def _needs(case: WireCase):
    """(module whose presence marks this case's environment, the CI job that has it)."""
    if case.env == "jarvis_web":
        return "sqlalchemy", "jarvis-web-behaviour"
    return "pydantic_settings", "orchestrator-behaviour"


def _build(case: WireCase, monkeypatch, request):
    transport = Transport(path for _method, path in case.requests)
    transport.script = [case.ok]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: transport.client(*a, **kw))
    harness = Harness(monkeypatch, transport)
    if case.file == "src/gateway/wyoming_bridge.py":
        harness.wyoming_bridge = request.getfixturevalue("wyoming_bridge")
    call, check_default = case.setup(harness)
    return transport, call, check_default


@pytest.fixture(autouse=True)
def _modules_imported_before_any_capture(request, _wire_env):
    """A service's main reconfigures structlog when it is first imported,
    which detaches a log capture that is already open. Autouse fixtures run
    before `captured_logs`, so the case's modules are imported here, with
    patches that are undone straight away."""
    case = getattr(getattr(request.node, "callspec", None), "params", {}).get("case")
    if isinstance(case, WireCase) and importlib.util.find_spec(_needs(case)[0]) is not None:
        with pytest.MonkeyPatch.context() as throwaway:
            _build(case, throwaway, request)
        _reset_refusal_state()


@pytest.fixture
def prepare(monkeypatch, request):
    """(case) -> (transport, call, check_default), with every httpx client
    the caller builds answered by the case's transport."""

    def build(case: WireCase):
        wanted, runs_in = _needs(case)
        if importlib.util.find_spec(wanted) is None:
            pytest.skip(f"{case.file} imports only where {wanted} is installed; this case runs in {runs_in}")
        return _build(case, monkeypatch, request)

    return build


def _stdlib_text(caplog):
    """Everything written through stdlib logging: each record's message and
    its attributes (an `extra=` field doesn't show in the message)."""
    return "\n".join(f"{r.name} {r.getMessage()} {sorted(vars(r).items())!r}" for r in caplog.records)


def _refused(logs):
    return [r for r in logs if r.get("event") == "admin_backend_refused"]


def _by_id(cases):
    return pytest.mark.parametrize("case", cases, ids=[c.id for c in cases])


def _named(*keys):
    return [_case(*key) for key in keys]


# W1 -----------------------------------------------------------------------

def _scanner():
    spec = importlib.util.spec_from_file_location("_wire_scanner", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_caller_file_has_a_wire_case():
    scanner = _scanner()
    caller_files = set(scanner.REVIEWED_TARGETS)
    assert len(caller_files) == 17
    wired = {case.file for case in WIRE_CASES}
    assert len(wired) == 16
    missing = caller_files - (wired | {"src/shared/admin_config.py"})
    assert not missing, f"caller file(s) with no wire case: {sorted(missing)}"
    assert wired <= caller_files, sorted(wired - caller_files)
    assert "src/control_agent/huggingface.py" in wired
    for case in WIRE_CASES:
        assert case.function in scanner.REVIEWED_TARGETS[case.file], case.id
        assert case.id.startswith(f"area_{scanner.area_of(case.file)}__"), case.id
        assert len(case.requests) == len(case.routes) >= 1
        assert all(route.startswith("/api/") for route in case.routes)
    assert len({case.id for case in WIRE_CASES}) == len(WIRE_CASES)
    assert sorted(c.function for c in WIRE_CASES if c.env == "jarvis_web") == [
        "get_persistent_sessions_config", "get_room_tv_configs"]


# W2 -----------------------------------------------------------------------

def _rotate_key(case, monkeypatch):
    """Change the configured key after the caller is imported and set up.
    jarvis-web keeps its key in a module constant by design (it has no
    shared.config), so there the constant is what changes."""
    monkeypatch.setenv("SERVICE_API_KEY", ROTATED_KEY)
    _clear_config_caches()
    if case.env == "jarvis_web":
        monkeypatch.setattr(_jarvis_main(), "SERVICE_API_KEY", ROTATED_KEY)


@_by_id(WIRE_CASES)
def test_request_carries_the_configured_key(case, prepare, monkeypatch):
    transport, call, _default = prepare(case)
    _rotate_key(case, monkeypatch)
    _run(call())
    sent = [(r.method, r.url.path) for r in transport.targeted]
    assert sent == list(case.requests)
    for request in transport.targeted:
        assert request.headers.get("X-Service-Key") == ROTATED_KEY, (
            f"{request.method} {request.url.path} sent X-Service-Key={request.headers.get('X-Service-Key')!r}"
        )
        assert "Authorization" not in request.headers


# W3 -----------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 503])
@_by_id(WIRE_CASES)
def test_refusal_falls_back_to_the_default(case, status, prepare):
    transport, call, check_default = prepare(case)
    transport.script = [(status, {"detail": "refused"})]
    check_default(_run(call()))
    assert len(transport.targeted) == len(case.requests), "one attempt per request, no retry"


# W4 -----------------------------------------------------------------------

@_by_id(_named(("src/shared/llm_router.py", "_get_backend_config")))
def test_llm_backend_fallback_is_not_cached(case, prepare):
    transport, call, _default = prepare(case)
    transport.script = [(401, {"detail": "refused"}), (200, BACKEND_ROWS)]
    _ollama_fallback(_run(call()))
    recovered = _run(call())
    assert recovered["backend_type"] == "mlx"
    assert len(transport.targeted) == 2


# W5 -----------------------------------------------------------------------

@_by_id(_named(("src/orchestrator/helpers.py", "get_feature_config")))
def test_feature_config_default_is_not_cached(case, prepare):
    transport, _call, _default = prepare(case)
    helpers = sys.modules["orchestrator.helpers"]
    runtime = sys.modules["orchestrator.nodes._runtime"]
    runtime.get_orch_feature_flag_cache().clear()
    flag = "state_question_routing_kill_switch"
    transport.script = [(401, {"detail": "refused"}), (200, [{"name": flag, "enabled": True, "config": {}}])]
    assert _run(helpers.get_feature_config(flag)) == {"enabled": False, "config": {}}
    assert _run(helpers.get_feature_config(flag))["enabled"] is True
    assert len(transport.targeted) == 2
    runtime.get_orch_feature_flag_cache().clear()


# W6 -----------------------------------------------------------------------

UNSET_KEY_CASES = _named(
    ("src/shared/llm_router.py", "_get_backend_config"),
    ("src/orchestrator/helpers.py", "get_feature_config"),
    ("src/control_agent/huggingface.py", "send_progress_callback"),
)


@_by_id(UNSET_KEY_CASES)
def test_unset_key_sends_no_header(case, prepare, monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "")
    _clear_config_caches()
    transport, call, _default = prepare(case)
    _run(call())
    assert [(r.method, r.url.path) for r in transport.targeted] == list(case.requests)
    for request in transport.targeted:
        assert "X-Service-Key" not in request.headers


# W7 -----------------------------------------------------------------------

CONTROL_AGENT_CASE = _named(("src/control_agent/huggingface.py", "send_progress_callback"))


@_by_id(CONTROL_AGENT_CASE)
def test_control_agent_warns_once_when_its_key_is_unset(case, prepare, monkeypatch, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    _clear_config_caches()
    transport, call, _default = prepare(case)
    _run(call())
    _run(call())
    assert len(transport.targeted) == 2, "the callback is still attempted"
    warnings = [r for r in captured_logs if r.get("log_level") == "warning" and "SERVICE_API_KEY" in repr(r)]
    assert len(warnings) == 1, captured_logs
    assert WIRE_KEY not in repr(captured_logs) and WIRE_KEY not in _stdlib_text(caplog)


# W8 -----------------------------------------------------------------------

def _progress_body_keys():
    """The body keys the progress route's write test sends, read from the
    admin-backend matrix file's literal (that file imports the app)."""
    for node in ast.parse(MATRIX_TEST.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PROGRESS_BODY_KEYS" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError("PROGRESS_BODY_KEYS not found as a literal")


@_by_id(CONTROL_AGENT_CASE)
def test_control_agent_body_keys_are_the_route_contract(case, prepare):
    transport, call, _default = prepare(case)
    _run(call())
    (request,) = transport.targeted
    expected = {
        "status", "progress_percent", "downloaded_bytes", "error_message", "download_path",
        "ollama_model_name", "ollama_imported",
    }
    assert set(json.loads(request.content)) == expected
    assert _progress_body_keys() == expected


# W9 -----------------------------------------------------------------------

REFUSAL_CASES = [(case, status) for case in WIRE_CASES for status in case.refusal_statuses]


@pytest.mark.parametrize(
    "case,status", REFUSAL_CASES, ids=[f"{case.id}-{status}" for case, status in REFUSAL_CASES])
def test_refusal_is_logged_once_as_an_error(case, status, prepare, captured_logs, caplog):
    caplog.set_level(logging.DEBUG)
    transport, call, _default = prepare(case)
    transport.script = [(status, {"detail": "refused"})]
    _run(call())
    first = _refused(captured_logs)
    assert sorted((r.get("status"), r.get("route")) for r in first) == sorted(
        (status, route) for route in case.routes
    ), f"admin_backend_refused records after a {status}: {first}"
    assert all(r["log_level"] == "error" for r in first)
    _run(call())
    assert len(_refused(captured_logs)) == len(first), "the second refusal in the window logs nothing"
    assert len(transport.targeted) == 2 * len(case.requests)
    # Neither structlog nor stdlib logging carries the key, or the concrete
    # id a templated route was called with.
    for text in (repr(captured_logs), _stdlib_text(caplog)):
        assert WIRE_KEY not in text
    assert all("zz-service" not in repr(r) and "/7/" not in repr(r) for r in _refused(captured_logs))


def test_the_stdlib_leak_check_sees_a_stdlib_line(caplog):
    """Positive control for the caplog half of the checks above."""
    caplog.set_level(logging.DEBUG)
    logging.getLogger("zz_planted").error("refused with %s", WIRE_KEY)
    logging.getLogger("zz_planted").error("refused", extra={"key": "zz-extra-" + WIRE_KEY})
    text = _stdlib_text(caplog)
    assert text.count(WIRE_KEY) >= 2 and "zz-extra-" in text


def test_refusal_cases_population():
    assert len(REFUSAL_CASES) == 3 * len(WIRE_CASES) - 1
    assert len(WIRE_CASES) == 20
    # The one route that answers 503 for its own reasons.
    assert [c.function for c in WIRE_CASES if 503 not in c.refusal_statuses] == ["get_service_url"]


# W9a ----------------------------------------------------------------------

@_by_id(_named(("src/shared/service_registry.py", "get_service_url")))
def test_a_route_s_own_503_is_not_a_refusal(case, prepare, captured_logs):
    transport, call, _default = prepare(case)
    transport.script = [(503, {"detail": "Service is disabled"})]
    assert _run(call()) is None
    assert any("disabled" in str(r.get("event", "")).lower() for r in captured_logs), captured_logs
    assert _refused(captured_logs) == []


# W9b ----------------------------------------------------------------------

@_by_id(WIRE_CASES)
def test_a_transport_error_is_not_a_refusal(case, prepare, captured_logs):
    """Every caller, the ones that used to skip certificate verification
    included: a connection that fails gives the default and no refusal line."""
    transport, call, check_default = prepare(case)
    transport.error = lambda request: httpx.ConnectError("connection refused", request=request)
    check_default(_run(call()))
    # A caller with two requests to the same host may stop at the first
    # failure (`_load_engines` does); none retries.
    assert 1 <= len(transport.targeted) <= len(case.requests)
    assert _refused(captured_logs) == []


# W4 for jarvis-web ----------------------------------------------------------

@_by_id(_named(("apps/jarvis-web/backend/main.py", "get_persistent_sessions_config")))
def test_jarvis_web_refusal_does_not_extend_a_stale_feature_cache(case, prepare):
    """A refusal doesn't count as a fresh read: with an expired cache that
    says the feature is off, a refused refresh answers from it once, and the
    next call asks again and gets the real value."""
    transport, _call, _default = prepare(case)
    main = _jarvis_main()
    main._feature_cache = {"persistent_chat_sessions": {"name": "persistent_chat_sessions", "enabled": False}}
    main._feature_cache_time = 0.0
    transport.script = [(401, {"detail": "refused"}), case.ok]
    try:
        assert _run(main.get_persistent_sessions_config()) is None
        assert _run(main.get_persistent_sessions_config()) == {"a": 1}
        assert len(transport.targeted) == 2
    finally:
        main._feature_cache, main._feature_cache_time = {}, 0.0
