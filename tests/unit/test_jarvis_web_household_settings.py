"""jarvis-web reads its room, thermostat and appliance entities through
``household`` (admin value, then env, then today's default).

The image has no ``shared`` package: the reader is built on the copied pure
module and the fetch is injected from main.py. Handlers only ever ``peek()``.
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

from . import _jarvis_web_harness as h

main = h.main
household = h.household

ENV_NAMES = (
    "DEFAULT_ROOM", "CLIMATE_ENTITY", "MIN_TEMP", "MAX_TEMP", "OVEN_ENTITY_ID", "FRIDGE_ENTITY_ID",
    "FREEZER_ENTITY_ID", "STOVE_COOK_MODE_SENSOR_ID", "STOVE_DISPLAY_TEMP_SENSOR_ID",
    "STOVE_TIMER_SENSOR_ID", "FRIDGE_DOOR_SENSOR_ID",
)
MAIN_SOURCE = Path(main.__file__).read_text()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(household, "_cache", None)  # restored on teardown
    main.mode_override = None
    yield
    main.mode_override = None
    h.configure()


def _body(value, revision=1):
    return {"jarvis_web": {"value": value, "stored": {}, "revision": revision}}


def _configure_fetch(value):
    async def fetch():
        return _body(value)

    return household.configure(fetch=fetch)


def _load(value):
    _configure_fetch(value)
    asyncio.run(household.warm())
    return household.settings()


# --- precedence --------------------------------------------------------------------------


def test_nothing_set_gives_todays_defaults():
    cfg = household.settings()
    assert (cfg.default_room, cfg.climate_entity, cfg.min_temp, cfg.max_temp) == ("guest", "climate.thermostat", 65, 75)
    assert cfg.oven_entity == cfg.fridge_entity == cfg.freezer_entity == ""
    assert cfg.source == "default"


def test_env_alone_behaves_as_before(monkeypatch):
    monkeypatch.setenv("DEFAULT_ROOM", "Kitchen")
    monkeypatch.setenv("CLIMATE_ENTITY", "climate.env")
    monkeypatch.setenv("MIN_TEMP", "60")
    monkeypatch.setenv("MAX_TEMP", "80")
    monkeypatch.setenv("OVEN_ENTITY_ID", "water_heater.env_oven")
    cfg = household.settings()
    assert (cfg.default_room, cfg.climate_entity, cfg.min_temp, cfg.max_temp) == ("Kitchen", "climate.env", 60, 80)
    assert cfg.oven_entity == "water_heater.env_oven"
    assert cfg.source == "env"


def test_admin_value_beats_env_field_by_field(monkeypatch):
    monkeypatch.setenv("CLIMATE_ENTITY", "climate.env")
    monkeypatch.setenv("OVEN_ENTITY_ID", "water_heater.env_oven")
    cfg = _load({"climate_entity": "climate.admin"})
    assert cfg.climate_entity == "climate.admin"
    assert cfg.sources["climate_entity"] == "admin"
    assert cfg.oven_entity == "water_heater.env_oven"
    assert cfg.sources["oven_entity"] == "env"
    assert cfg.default_room == "guest"


def test_admin_unreachable_keeps_env(monkeypatch):
    monkeypatch.setenv("CLIMATE_ENTITY", "climate.env")

    async def refuses():
        return None

    household.configure(fetch=refuses)
    asyncio.run(household.warm())
    assert household.settings().climate_entity == "climate.env"


def test_a_fetch_that_raises_keeps_env_and_never_propagates(monkeypatch):
    monkeypatch.setenv("CLIMATE_ENTITY", "climate.env")

    async def boom():
        raise RuntimeError("secret-detail")

    household.configure(fetch=boom)
    asyncio.run(household.warm())
    assert household.settings().climate_entity == "climate.env"


# --- invalid stored values ----------------------------------------------------------------


def test_an_admin_climate_entity_in_the_wrong_domain_is_ignored_and_env_is_used(monkeypatch):
    monkeypatch.setenv("CLIMATE_ENTITY", "climate.env")
    cfg = _load({"climate_entity": "switch.heater"})
    assert cfg.climate_entity == "climate.env"
    assert cfg.sources["climate_entity"] == "env"


def test_an_invalid_stored_appliance_falls_through_to_the_default():
    cfg = _load({"oven_entity": "water_heater.hot_water_tank", "fridge_entity": "water_heater.fridge"})
    assert cfg.oven_entity == ""
    assert cfg.fridge_entity == "water_heater.fridge"


# --- the thermostat bounds are a unit ------------------------------------------------------


def test_both_admin_bounds_are_used_together(monkeypatch):
    monkeypatch.setenv("MIN_TEMP", "50")
    monkeypatch.setenv("MAX_TEMP", "90")
    cfg = _load({"min_temp": 62, "max_temp": 80})
    assert (cfg.min_temp, cfg.max_temp) == (62, 80)


def test_one_admin_bound_alone_is_not_used(monkeypatch):
    monkeypatch.setenv("MIN_TEMP", "50")
    monkeypatch.setenv("MAX_TEMP", "90")
    cfg = _load({"min_temp": 62})
    assert (cfg.min_temp, cfg.max_temp) == (50, 90)
    assert cfg.sources["min_temp"] == "env"


def test_inverted_admin_bounds_are_not_used():
    cfg = _load({"min_temp": 80, "max_temp": 62})
    assert (cfg.min_temp, cfg.max_temp) == (65, 75)


# --- the handlers -------------------------------------------------------------------------


@pytest.fixture
def out(monkeypatch):
    out = h.install_outbound(monkeypatch)
    h.configure()
    return out


def _owner_post(path, body):
    return h.client().post(path, json=body, headers={**h.via_proxy(h.LAN), **h.CSRF})


def test_the_temperature_is_clamped_to_the_admin_bounds(out):
    household.install_for_test(sources={"jarvis_web": {"climate_entity": "climate.upstairs", "min_temp": 62, "max_temp": 80}})
    resp = _owner_post("/api/climate/temperature", {"temperature": 95})
    assert resp.status_code == 200 and resp.json()["temperature"] == 80
    method, url, kw = out.calls[-1]
    assert url.endswith("/api/services/climate/set_temperature")
    assert kw["json"] == {"entity_id": "climate.upstairs", "temperature": 80}
    assert _owner_post("/api/climate/temperature", {"temperature": 40}).json()["temperature"] == 62


def test_the_temperature_is_clamped_to_the_defaults_without_admin_bounds(out):
    household.install_for_test(sources={})
    assert _owner_post("/api/climate/temperature", {"temperature": 95}).json()["temperature"] == 75


def test_the_climate_read_uses_the_admin_entity_and_bounds(out):
    household.install_for_test(sources={"jarvis_web": {"climate_entity": "climate.upstairs", "min_temp": 62, "max_temp": 80}})
    resp = h.client().get("/api/climate", headers=h.via_proxy(h.LAN))
    assert resp.status_code == 200
    assert resp.json()["min_temp"] == 62 and resp.json()["max_temp"] == 80
    assert out.calls[-1][1].endswith("/api/states/climate.upstairs")


def test_the_chat_request_carries_the_admin_room(out):
    household.install_for_test(sources={"jarvis_web": {"default_room": "Kitchen"}})
    resp = h.client().post("/api/chat", json={"message": "hello"}, headers={**h.via_proxy(h.LAN), **h.CSRF})
    assert resp.status_code == 200, resp.text
    assert out.orchestrator_bodies()[-1]["room"] == "Kitchen"


def test_the_oven_is_set_on_the_admin_entity(out):
    household.install_for_test(sources={"jarvis_web": {"oven_entity": "water_heater.admin_oven"}})
    assert _owner_post("/api/appliances/oven/temperature", {"temperature": 350}).status_code == 200
    assert out.calls[-1][2]["json"]["entity_id"] == "water_heater.admin_oven"


def test_an_unconfigured_appliance_names_the_page_and_the_variable(out):
    household.install_for_test(sources={})
    resp = h.client().get("/api/appliances/oven", headers=h.via_proxy(h.LAN))
    assert resp.status_code == 503
    assert "Devices page" in resp.json()["detail"] and "OVEN_ENTITY_ID" in resp.json()["detail"]


# --- peek, never await ---------------------------------------------------------------------


def test_a_hanging_admin_fetch_does_not_delay_a_handler(out):
    started = []

    async def hangs():
        started.append(True)
        await asyncio.sleep(30)

    household.configure(fetch=hangs)
    began = time.monotonic()
    resp = h.client().get("/api/appliances/oven", headers=h.via_proxy(h.LAN))
    assert resp.status_code == 503
    assert time.monotonic() - began < 5


def test_peek_returns_at_once_while_the_fetch_hangs():
    async def run():
        async def hangs():
            await asyncio.sleep(30)

        household.configure(fetch=hangs)
        began = time.monotonic()
        cfg = household.settings()
        elapsed = time.monotonic() - began
        await household.aclose()
        return cfg, elapsed

    cfg, elapsed = asyncio.run(run())
    assert cfg.climate_entity == "climate.thermostat"
    assert elapsed < 0.5


def test_only_the_startup_hook_awaits_the_reader():
    tree = ast.parse(MAIN_SOURCE)
    awaited = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "household":
                    awaited.setdefault(func.attr, set()).add(fn.name)
    assert awaited == {"warm": {"startup"}, "aclose": {"shutdown"}}


def test_main_reads_no_household_environment_variable_itself():
    names = "|".join(ENV_NAMES)
    import re

    assert not re.findall(r'os\.getenv\("(?:%s)"' % names, MAIN_SOURCE)


def test_entities_in_ha_urls_are_quoted():
    tree = ast.parse(MAIN_SOURCE)
    bare = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = ast.unparse(node)
            if "/api/states/" in text and "cfg." in text and "quote(" not in text:
                bare.append(node.lineno)
    assert not bare, bare


# --- the fetch main.py injects ---------------------------------------------------------------


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.response


def test_the_injected_fetch_asks_for_the_jarvis_section_with_the_service_key(monkeypatch):
    client = _Client(_Resp(200, _body({"default_room": "Den"})))
    monkeypatch.setattr(main, "_household_client", client)
    monkeypatch.setattr(main, "ADMIN_BACKEND_URL", "http://admin.local:8080")
    body = asyncio.run(main._fetch_household())
    assert body["jarvis_web"]["value"] == {"default_room": "Den"}
    url, kw = client.calls[0]
    assert url == "http://admin.local:8080/api/settings/household"
    assert kw["params"] == [("sections", "jarvis_web")]
    assert kw["headers"] == main._service_key_headers() and kw["headers"]


def test_a_refusal_is_noted_by_route_template_and_gives_none(monkeypatch):
    noted = []
    monkeypatch.setattr(main, "_household_client", _Client(_Resp(401, {})))
    monkeypatch.setattr(main, "ADMIN_BACKEND_URL", "http://admin.local:8080")
    monkeypatch.setattr(main, "note_admin_refusal", lambda status, route: noted.append((status, route)))
    assert asyncio.run(main._fetch_household()) is None
    assert noted == [(401, "/api/settings/household")]


def test_no_admin_url_means_no_call(monkeypatch):
    client = _Client(_Resp(200, {}))
    monkeypatch.setattr(main, "_household_client", client)
    monkeypatch.setattr(main, "ADMIN_BACKEND_URL", "")
    assert asyncio.run(main._fetch_household()) is None
    assert client.calls == []


# --- startup and shutdown --------------------------------------------------------------------


def test_startup_loads_admin_values_before_the_first_request(monkeypatch):
    async def fetch():
        return _body({"default_room": "Den"})

    monkeypatch.setattr(main, "_fetch_household", fetch)
    monkeypatch.setattr(main, "_SA_URL", "")
    asyncio.run(main.startup())
    assert household.settings().default_room == "Den"


def test_shutdown_closes_the_cache_and_the_http_client(monkeypatch):
    closed = []

    class _Closable:
        async def aclose(self):
            closed.append(True)

    household.configure(fetch=lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "_household_client", _Closable())
    monkeypatch.setattr(main, "_engine", None)
    asyncio.run(main.shutdown())
    assert closed == [True]
    assert main._household_client is None
