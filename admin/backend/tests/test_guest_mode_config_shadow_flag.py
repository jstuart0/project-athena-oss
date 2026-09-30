"""GET /api/guest-mode/config reports calendar_url_shadows_lodgify_api:
true only when the legacy calendar_url points at a lodgify.com host AND a
Lodgify API key is enabled -- the case where that URL's export slices add
guest time the API doesn't list."""
from __future__ import annotations

import pytest

from app.models import ExternalAPIKey, GuestModeConfig

URL = "/api/guest-mode/config"


def _config(db, user, calendar_url):
    db.add(GuestModeConfig(
        enabled=True, calendar_source="ical", calendar_url=calendar_url, guest_allowed_intents=[],
        guest_restricted_entities=[], guest_allowed_domains=[], created_by_id=user.id,
    ))
    db.commit()


def _key(db, user, enabled):
    from app.utils.encryption import encrypt_value

    db.add(ExternalAPIKey(service_name="lodgify", api_name="Lodgify", api_key_encrypted=encrypt_value("k"),
                          endpoint_url="https://api.lodgify.example", enabled=enabled, created_by_id=user.id))
    db.commit()


@pytest.mark.parametrize("calendar_url,key,expected", [
    ("https://www.lodgify.com/export/abc.ics", True, True),
    ("https://lodgify.com/export/abc.ics", True, True),
    ("https://www.lodgify.com/export/abc.ics", False, False),
    ("https://www.lodgify.com/export/abc.ics", None, False),
    ("https://calendar.example.com/abc.ics", True, False),
    ("https://notlodgify.com/abc.ics", True, False),
    (None, True, False),
    ("", True, False),
])
def test_shadow_flag(owner_client, db, test_user, calendar_url, key, expected):
    _config(db, test_user, calendar_url)
    if key is not None:
        _key(db, test_user, enabled=key)
    resp = owner_client.get(URL)
    assert resp.status_code == 200, resp.text
    assert resp.json()["calendar_url_shadows_lodgify_api"] is expected
