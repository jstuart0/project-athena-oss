"""The orchestrator's AdminConfigClient against the real admin-backend app.

The client keeps its production defaults (including its default
``X-API-Key`` header); only the transport is swapped for an in-process
ASGI transport over ``main.app``, with DEV_MODE's auth bypass off. So every
assertion here is the real request the orchestrator makes, answered by the
real route, dependency and handler.

A negative control repeats the calls with the wrong service key: each
positive assertion below must be telling a 401 apart from a success.

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
    CalendarEvent, Guest, RoomGroup, RoomGroupAlias, UserSession, VoiceAutomation,
)
from main import app
from shared.admin_config import AdminConfigClient
from shared.config import get_config


class _RecordingTransport(httpx.ASGITransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append((request.method, request.url.path))
        return await super().handle_async_request(request)


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
