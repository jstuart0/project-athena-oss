"""The orchestrator's AdminConfigClient against the real admin-backend app.

The client keeps its production defaults (including its default
``X-API-Key`` header); only the transport is swapped for an in-process
ASGI transport over ``main.app``, with DEV_MODE's auth bypass off. So every
assertion here is the real request the orchestrator makes, answered by the
real route, dependency and handler.

A negative control repeats the calls with the wrong service key: each
positive assertion below must be telling a 401 apart from a success.

The reads of the reviewed routes (feature flags, LLM backends, component
models, escalation) and ``LLMRouter``'s backend lookup are covered the same
way, on seeded rows so a default can't pass for data.

Not covered here (their modules import the orchestrator runtime, which the
admin test environment doesn't carry): ``_get_house_layout``,
``get_origin_placeholder_patterns`` and the emerging-intent discovery
calls. They're covered by
``tests/unit/test_admin_guest_route_callers_send_service_key.py`` and by
the post-roll in-pod probe.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.models import (
    CalendarEvent, ComponentModelAssignment, EscalationPreset, EscalationState, Feature, Guest,
    LLMBackend, RoomGroup, RoomGroupAlias, UserSession, VoiceAutomation,
)
from main import app
from shared.admin_config import AdminConfigClient
from shared.config import _clear_cache_for_tests, get_config
from shared.llm_router import LLMRouter


class _RecordingTransport(httpx.ASGITransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.sent = []       # the httpx.Request objects
        self.statuses = []

    async def handle_async_request(self, request):
        self.requests.append((request.method, request.url.path))
        self.sent.append(request)
        response = await super().handle_async_request(request)
        self.statuses.append(response.status_code)
        return response


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    # Both the oidc module the served app holds and the live one (another
    # test file evicts and re-imports app.* mid-suite).
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)


def _admin(api_key):
    admin = AdminConfigClient(admin_url="http://admin", api_key=api_key)
    original = admin.client
    transport = _RecordingTransport(app=app)
    admin.client = httpx.AsyncClient(transport=transport, headers=dict(original.headers), base_url="http://admin")
    assert admin.client.headers.get("X-API-Key") == api_key, "the client's default header is kept"
    return admin, transport


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def seeded(client, db):
    """`client` installs the test get_db override on main.app."""
    now = datetime.now(timezone.utc)
    ev = CalendarEvent(external_id="ev-seam", checkin=now - timedelta(days=1), checkout=now + timedelta(days=1),
                       status="confirmed", source="manual")
    db.add(ev)
    db.commit()
    guest = Guest(calendar_event_id=ev.id, name="Seam Guest", is_primary=True)
    db.add(guest)
    db.commit()
    db.add(UserSession(session_id="seam-session", guest_id=guest.id, device_id="dev-seam-1"))
    group = RoomGroup(name="seam_floor", display_name="Seam Floor")
    db.add(group)
    db.commit()
    db.add(RoomGroupAlias(room_group_id=group.id, alias="downstairs"))
    rows = {}
    for key, kw in (("ana", dict(owner_type="guest", guest_name="Ana", calendar_event_id=ev.id)),
                    ("ana_other_stay", dict(owner_type="guest", guest_name="Ana", calendar_event_id=ev.id + 1000)),
                    ("bo", dict(owner_type="guest", guest_name="Bo", calendar_event_id=ev.id + 2000))):
        row = VoiceAutomation(name=f"{key} auto", trigger_config={"type": "time"}, actions_config=[],
                              status="active", **kw)
        db.add(row)
        db.commit()
        rows[key] = row.id
    db.commit()
    return {"guest": guest.id, "stay": ev.id, **rows}


OWNER = dict(caller_mode="owner", caller_guest_name=None, caller_guest_stay=None)


def _ana(seeded):
    return dict(caller_mode="guest", caller_guest_name="Ana", caller_guest_stay=seeded["stay"])
NEW_OWNER_ROW = {"name": "Seam owner", "owner_type": "owner", "trigger_config": {"type": "time", "time": "07:00"},
                 "actions_config": [{"service": "light.turn_on", "entity_id": "light.porch"}]}


def test_room_groups_and_device_session_over_the_real_app(seeded):
    admin, _ = _admin(get_config().service_api_key)
    session = _run(admin.get_user_session_by_device("dev-seam-1"))
    assert session is not None and session["guest_id"] == seeded["guest"]
    assert "seam_floor" in [g["name"] for g in _run(admin.get_room_groups())]
    assert _run(admin.resolve_room_group("downstairs"))["name"] == "seam_floor"


def test_owner_scope_create_list_archive(seeded):
    admin, _ = _admin(get_config().service_api_key)
    created = _run(admin.create_voice_automation(dict(NEW_OWNER_ROW), **OWNER))
    assert created and isinstance(created.get("id"), int)
    listed = _run(admin.get_voice_automations(owner_type="owner", **OWNER))
    assert created["id"] in [r["id"] for r in listed]
    assert _run(admin.archive_voice_automation(created["id"], "x", **OWNER)) is True


def test_guest_scope_sees_and_changes_only_its_own_rows(seeded, db):
    admin, _ = _admin(get_config().service_api_key)
    listed = _run(admin.get_voice_automations(owner_type="guest", **_ana(seeded)))
    assert [r["id"] for r in listed] == [seeded["ana"]]
    assert _run(admin.archive_voice_automation(seeded["bo"], "x", **_ana(seeded))) is False
    assert _run(admin.archive_voice_automation(seeded["ana_other_stay"], "x", **_ana(seeded))) is False
    assert _run(admin.archive_voice_automation(seeded["ana"], "guest_asked", **_ana(seeded))) is True
    db.expire_all()
    assert db.query(VoiceAutomation).get(seeded["bo"]).status == "active"
    assert db.query(VoiceAutomation).get(seeded["ana_other_stay"]).status == "active"
    archived = db.query(VoiceAutomation).get(seeded["ana"])
    assert archived.status == "archived"
    assert archived.archive_reason == "guest_asked"


def test_hard_delete_is_refused_to_the_service(seeded, db):
    admin, _ = _admin(get_config().service_api_key)
    assert _run(admin.delete_voice_automation(seeded["ana"])) is False
    db.expire_all()
    assert db.query(VoiceAutomation).get(seeded["ana"]) is not None


@pytest.mark.parametrize("name,stay", [("", 5), ("Ana", None), ("Ana", 0), ("Ana", "5")])
def test_an_unscoped_guest_call_never_sends(seeded, name, stay):
    admin, transport = _admin(get_config().service_api_key)
    with pytest.raises(ValueError):
        _run(admin.get_voice_automations(caller_mode="guest", caller_guest_name=name, caller_guest_stay=stay))
    assert transport.requests == []


def test_negative_control_wrong_key_fails_every_call(seeded):
    admin, transport = _admin("wrong-key")
    assert _run(admin.get_user_session_by_device("dev-seam-1")) is None
    assert _run(admin.get_room_groups()) == []
    assert _run(admin.resolve_room_group("downstairs")) is None
    assert _run(admin.create_voice_automation(dict(NEW_OWNER_ROW), **OWNER)) is None
    assert _run(admin.get_voice_automations(owner_type="owner", **OWNER)) == []
    assert _run(admin.archive_voice_automation(seeded["ana"], "x", **OWNER)) is False
    # Every call really reached the app (and was refused there).
    assert len(transport.requests) == 6


# ---------------------------------------------------------------------------
# The reviewed routes
# ---------------------------------------------------------------------------

SEAM_COMPONENT = "seam_component"
REVIEWED_READS = [
    ("GET", "/api/features/public"),
    ("GET", "/api/llm-backends/public"),
    ("GET", f"/api/component-models/component/{SEAM_COMPONENT}"),
    ("GET", "/api/escalation/presets/active/public"),
]


@pytest.fixture
def reviewed_seed(client, db):
    db.add(Feature(name="seam_flag", display_name="Seam flag", category="processing", enabled=True))
    db.add(LLMBackend(model_name="m-seam", backend_type="mlx", endpoint_url="http://mlx.example:8080", enabled=True))
    db.add(ComponentModelAssignment(component_name=SEAM_COMPONENT, display_name="Seam component",
                                    model_name="seam-model:1b", enabled=True))
    db.add(EscalationPreset(name="seam-preset", is_active=True))
    db.commit()


def _reviewed_reads(admin):
    admin.enable_feature_flag("use_database_model_config")
    return (
        _run(admin.get_feature_flags()),
        _run(admin.get_llm_backends()),
        _run(admin.get_component_model(SEAM_COMPONENT)),
        _run(admin.get_active_escalation_preset()),
    )


def test_reviewed_reads_return_seeded_data_with_the_key(reviewed_seed):
    key = get_config().service_api_key
    admin, transport = _admin(key)
    flags, backends, component, preset = _reviewed_reads(admin)
    assert flags.get("seam_flag") is True
    assert "m-seam" in [b["model_name"] for b in backends]
    assert component is not None and component["model_name"] == "seam-model:1b"
    assert preset is not None and preset["name"] == "seam-preset"
    assert transport.requests == REVIEWED_READS
    for request in transport.sent:
        assert request.headers.get("X-API-Key") == key, "the client's default header is kept"
        assert request.headers.get("X-Service-Key") == key, (
            f"{request.method} {request.url.path} sent no X-Service-Key"
        )


def test_reviewed_reads_fall_back_with_a_wrong_key(reviewed_seed):
    admin, transport = _admin("wrong-key")
    flags, backends, component, preset = _reviewed_reads(admin)
    assert (flags, backends, component, preset) == ({}, [], None, None)
    assert transport.requests == REVIEWED_READS


class _CallerKey:
    """Gives the caller a different SERVICE_API_KEY from the app's, as two
    processes would have: the caller's value is in force except while the
    app is handling a request."""

    def __init__(self, monkeypatch, transport, caller_key):
        server_key = get_config().service_api_key
        inner = transport.handle_async_request

        def use(key):
            monkeypatch.setenv("SERVICE_API_KEY", key)
            _clear_cache_for_tests()

        async def handle(request):
            use(server_key)
            try:
                return await inner(request)
            finally:
                use(caller_key)

        transport.handle_async_request = handle
        use(caller_key)
        self.restore = lambda: use(server_key)


def _router(monkeypatch, caller_key):
    router = LLMRouter(admin_url="http://admin", persist_metrics=False)
    transport = _RecordingTransport(app=app)
    router.client = httpx.AsyncClient(transport=transport, base_url="http://admin")
    return router, transport, _CallerKey(monkeypatch, transport, caller_key)


def test_llm_router_reads_its_backend_through_the_real_app(reviewed_seed, monkeypatch):
    key = get_config().service_api_key
    router, transport, caller = _router(monkeypatch, key)
    try:
        config = _run(router._get_backend_config("m-seam"))
    finally:
        caller.restore()
    assert config["backend_type"] == "mlx"
    assert config["endpoint_url"] == "http://mlx.example:8080"
    (request,) = transport.sent
    assert (request.method, request.url.path) == ("GET", "/api/llm-backends/public")
    assert request.headers.get("X-Service-Key") == key, "the router sent no X-Service-Key"
    assert "X-API-Key" not in request.headers


def test_llm_router_falls_back_when_the_app_refuses_it(reviewed_seed, monkeypatch):
    router, transport, caller = _router(monkeypatch, "wrong-key")
    try:
        config = _run(router._get_backend_config("m-seam"))
        ollama_url = get_config().ollama_url
    finally:
        caller.restore()
    assert transport.statuses == [401]
    assert config["backend_type"] == "ollama"
    assert config["endpoint_url"] == ollama_url
    assert (config["max_tokens"], config["timeout_seconds"]) == (2048, 60)


def _stored_state(db, session_id):
    db.expire_all()
    row = db.query(EscalationState).filter(EscalationState.session_id == session_id).first()
    return None if row is None else (row.escalated_to, row.turns_remaining)


def test_escalation_state_write_through_the_real_app(client, db):
    admin, _ = _admin(get_config().service_api_key)
    assert _run(admin.update_escalation_state("seam-session", "complex", 3)) is True
    state = _run(admin.get_escalation_state("seam-session"))
    assert (state["escalated_to"], state["turns_remaining"]) == ("complex", 3)
    assert _stored_state(db, "seam-session") == ("complex", 3)

    refused, transport = _admin("wrong-key")
    assert _run(refused.update_escalation_state("seam-session", "super_complex", 9)) is False
    assert _run(refused.get_escalation_state("seam-session")) is None
    assert transport.statuses == [401, 401]
    assert _stored_state(db, "seam-session") == ("complex", 3)


# R6 -----------------------------------------------------------------------

# Every AdminConfigClient method that reports a refusal, with arguments that
# reach its request.
NOTED_READS = {
    "get_intent_routing": (),
    "get_provider_routing": (),
    "get_llm_backends": (),
    "get_feature_flags": (),
    "get_tool_api_key_requirements": ("zz_tool",),
    "get_tool_calling_settings": (),
    "get_fallback_triggers": (),
    "get_active_escalation_preset": (),
    "get_escalation_state": ("zz-session",),
    "update_escalation_state": ("zz-session", "complex", 3),
    "get_component_model": ("zz_component",),
    "get_all_component_models": (),
    "get_gateway_config": (),
    "get_voice_config_stt": (),
    "get_voice_config_tts": (),
    "get_voice_config_all": (),
    "get_voice_interface_config": ("zz_interface",),
    "check_voice_services_health": (),
}


def _methods_with_a_refusal_note():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(inspect.getmodule(AdminConfigClient)))
    (cls,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AdminConfigClient"]
    return {
        fn.name for fn in cls.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "note_admin_refusal" for c in ast.walk(fn))
    }


def _fresh_refusal_logs():
    """Both sides' rate limits emptied and their loggers rebound, so each
    refusal below writes its own line on the caller and on the app."""
    caller = AdminConfigClient.get_feature_flags.__globals__["note_admin_refusal"].__globals__
    (served,) = [m.cls.__call__.__globals__ for m in app.user_middleware
                 if getattr(m.cls, "__name__", "") == "AuthRejectionMiddleware"]
    for namespace in (caller, served):
        namespace["_reset_for_tests"]()
        vars(namespace["logger"]).pop("bind", None)


def test_every_reviewed_admin_client_read_notes_its_own_route(client, db):
    """Each method sends the key, and the route it reports as refused is the
    route the app matched for its request (the app's own rejection line
    carries that route's template)."""
    import structlog

    assert _methods_with_a_refusal_note() == set(NOTED_READS)
    assert len(NOTED_READS) >= 18
    for name, args in sorted(NOTED_READS.items()):
        admin, transport = _admin("wrong-key")
        admin.enable_feature_flag("use_database_model_config")
        _fresh_refusal_logs()
        with structlog.testing.capture_logs() as logs:
            _run(getattr(admin, name)(*args))
        assert transport.statuses == [401], f"{name}: {transport.requests} {transport.statuses}"
        (request,) = transport.sent
        assert request.headers.get("X-Service-Key") == "wrong-key", f"{name} sent no X-Service-Key"
        matched = [r["route"] for r in logs if r.get("event") == "admin_auth_rejected"]
        noted = [(r["route"], r["status"]) for r in logs if r.get("event") == "admin_backend_refused"]
        assert len(matched) == 1 and matched[0].startswith("/api/"), f"{name}: the app matched {matched}"
        assert noted == [(matched[0], 401)], f"{name}: noted {noted}, the app matched {matched}"
