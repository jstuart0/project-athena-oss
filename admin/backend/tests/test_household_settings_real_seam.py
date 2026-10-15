"""The services' AdminConfigClient.fetch_household_settings against the real
admin-backend app.

Only the transport is swapped for an in-process ASGI transport over
``main.app`` with DEV_MODE's auth bypass off, so every assertion is the request
a service really makes, answered by the real route, dependency and handler. A
wrong or missing key is the negative control: each success below is telling a
200 apart from a 401.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.applications import Starlette

from app.models import SystemSetting
from main import app
from shared import household
from shared.admin_config import AdminConfigClient
from shared.config import get_config

FEED = {"id": "rtd_bus", "name": "RTD Local Bus", "url": "https://feeds.example.org/gtfs.zip?api_key=SECRET"}


class _RecordingTransport(httpx.ASGITransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.statuses = []

    async def handle_async_request(self, request):
        self.sent.append(request)
        response = await super().handle_async_request(request)
        self.statuses.append(response.status_code)
        return response


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)


def _admin(api_key, served=None):
    admin = AdminConfigClient(admin_url="http://admin", api_key=api_key)
    transport = _RecordingTransport(app=served or app)
    admin.client = httpx.AsyncClient(transport=transport, base_url="http://admin")
    return admin, transport


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def seeded(client, db):
    rows = {
        "household.sms": {"v": 1, "revision": 3, "value": {"default_country_code": "44"}},
        "household.amtrak": {"v": 1, "revision": 1, "value": {"default_station": "BOS"}},
        "household.transit": {"v": 1, "revision": 2, "value": {"gtfs_feeds": [FEED], "region_name": "Denver"}},
    }
    for key, value in rows.items():
        db.add(SystemSetting(key=key, value=json.dumps(value), category="household"))
    db.commit()


def good_key():
    return get_config().service_api_key


def test_multiple_sections_come_back_whole_for_the_service(seeded):
    admin, transport = _admin(good_key())
    body = _run(admin.fetch_household_settings(("sms", "transit", "amtrak")))
    assert list(body) == ["amtrak", "transit", "sms"] or set(body) == {"sms", "transit", "amtrak"}
    assert body["sms"]["value"] == {"default_country_code": "44"} and body["sms"]["revision"] == 3
    assert body["transit"]["value"]["gtfs_feeds"][0]["url"] == FEED["url"]  # the service sees the whole URL
    (request,) = transport.sent
    assert request.url.path == "/api/settings/household"
    assert request.url.params.get_list("sections") == ["sms", "transit", "amtrak"]
    assert request.headers["X-Service-Key"] == good_key() and transport.statuses == [200]


def test_a_single_section(seeded):
    admin, _transport = _admin(good_key())
    body = _run(admin.fetch_household_settings(("amtrak",)))
    assert set(body) == {"amtrak"} and body["amtrak"]["value"] == {"default_station": "BOS"}


def test_no_sections_asks_for_all_of_them(seeded):
    admin, transport = _admin(good_key())
    body = _run(admin.fetch_household_settings(()))
    assert set(body) >= {"sms", "amtrak", "transit", "location", "jarvis_web"}
    assert "sections" not in transport.sent[0].url.params


def test_an_unknown_section_is_dropped(seeded):
    admin, _transport = _admin(good_key())
    body = _run(admin.fetch_household_settings(("sms", "from_the_future")))
    assert set(body) == {"sms"}
    assert _run(admin.fetch_household_settings(("from_the_future",))) == {}


def test_a_section_with_nothing_stored_has_no_value(seeded):
    admin, _transport = _admin(good_key())
    body = _run(admin.fetch_household_settings(("gateway",)))
    assert body["gateway"]["value"] is None and body["gateway"]["revision"] == 0


def test_the_wrong_key_and_no_key_are_refused(seeded):
    wrong, transport = _admin("wrong-key")
    assert _run(wrong.fetch_household_settings(("sms",))) is None and transport.statuses == [401]
    missing, transport = _admin("placeholder")
    missing.api_key = ""
    assert _run(missing.fetch_household_settings(("sms",))) is None
    assert "X-Service-Key" not in transport.sent[0].headers and transport.statuses == [401]


def test_a_404_from_an_old_admin_is_none():
    admin, transport = _admin(good_key(), served=Starlette())
    assert _run(admin.fetch_household_settings(("sms",))) is None and transport.statuses == [404]


def test_the_whole_path_a_stored_value_reaches_a_resolver(seeded):
    admin, _transport = _admin(good_key())
    household.reset_for_test()
    cache = household.configure(sections=("sms", "amtrak", "transit"), fetch=lambda: admin.fetch_household_settings(("sms", "amtrak", "transit")))
    _run(cache.current())
    assert household.sms_country_code() == "44" and household.sms_country_code().source == "admin"
    assert household.amtrak_station() == "BOS"
    (feed,) = household.transit().feeds
    assert feed["url"] == FEED["url"] and feed["origin"] == "admin"
