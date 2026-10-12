"""Saving the ``home-assistant`` External API Key: the address policy, the token's
binding to its origin, and what a save does to the shared sign-in stop.

The address cases are the repository table shared with the resolver's tests
(tests/fixtures/ha_url_cases.json), judged here for the admin source.
"""
import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest

from app.models import ExternalAPIKey
from app.services import ha_access
from app.utils.encryption import decrypt_value, encrypt_value
from tests import ha_stack
from tests.ha_stack import TOKEN, URL, FakeHA

CASES = json.loads((pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ha_url_cases.json").read_text())["cases"]
KEYS = "/api/external-api-keys"
ORIGIN_A = "https://ha.example.invalid"


def body(url, *, token=TOKEN, service="home-assistant", **extra):
    return {"service_name": service, "api_name": "Home Assistant", "api_key": token, "endpoint_url": url, "enabled": True, **extra}


def stored(db, service="home-assistant"):
    db.expire_all()
    return db.query(ExternalAPIKey).filter_by(service_name=service).one()


def seed(db, owner, *, url=ORIGIN_A, token=TOKEN, service="home-assistant"):
    row = ExternalAPIKey(
        service_name=service, api_name="Home Assistant", api_key_encrypted=encrypt_value(token), endpoint_url=url,
        enabled=True, created_by_id=owner.id,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def saved(monkeypatch):
    calls = []

    async def spy(row):
        calls.append(row.service_name)

    monkeypatch.setattr(ha_access, "key_saved", spy)
    return calls


# -- the address policy -------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c["url"][:40] or "empty" for c in CASES])
def test_create_judges_the_address_with_the_admin_policy(owner_client, db, saved, case):
    response = owner_client.post(KEYS, json=body(case["url"]))
    if case["admin"] is None:
        assert response.status_code == 201, response.text
    else:
        assert response.status_code == 422
        assert response.json()["detail"] == f"Home Assistant address isn't allowed: {case['admin']}"
        assert db.query(ExternalAPIKey).count() == 0, "a refused address stores nothing"


@pytest.mark.parametrize("case", CASES, ids=[c["url"][:40] or "empty" for c in CASES])
def test_update_judges_the_address_with_the_admin_policy(owner_client, db, test_user, saved, case):
    seed(db, test_user)
    response = owner_client.put(f"{KEYS}/home-assistant", json=body(case["url"]))
    if case["admin"] is None:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 422
        assert response.json()["detail"] == f"Home Assistant address isn't allowed: {case['admin']}"
        assert stored(db).endpoint_url == ORIGIN_A, "a refused address changes nothing"


def test_other_services_keep_their_unrestricted_addresses(owner_client, saved):
    response = owner_client.post(KEYS, json=body("http://169.254.169.254/latest", service="api-football"))
    assert response.status_code == 201
    assert saved == [], "only the Home Assistant key reaches the sign-in stop"


# -- the token belongs to its origin ----------------------------------------------------------------------


def test_a_url_only_edit_to_another_origin_clears_the_token(owner_client, db, test_user, saved):
    seed(db, test_user)
    response = owner_client.put(f"{KEYS}/home-assistant", json=body("https://other.example.invalid", token=""))
    assert response.status_code == 200
    assert response.json()["token_cleared"] is True
    assert decrypt_value(stored(db).api_key_encrypted) == ""
    assert stored(db).endpoint_url == "https://other.example.invalid"
    from app.services.ha_access import lookup_from_db
    from shared.ha_credentials import resolve

    env = {"HA_URL": "http://ha.example.lan:8123", "HA_TOKEN": "env-token"}
    resolved = resolve(lookup_from_db(db), env, previous=None, service_api_key=None)
    assert resolved.configured is False and resolved.source == "admin", "no fall-through to the environment"


@pytest.mark.parametrize("new_url", ["http://ha.example.invalid", "https://ha.example.invalid:8123", "https://ha2.example.invalid"])
def test_a_scheme_port_or_host_change_is_a_new_origin(owner_client, db, test_user, saved, new_url):
    seed(db, test_user)
    response = owner_client.put(f"{KEYS}/home-assistant", json=body(new_url, token=""))
    assert response.json()["token_cleared"] is True
    assert decrypt_value(stored(db).api_key_encrypted) == ""


@pytest.mark.parametrize("same_origin", ["https://ha.example.invalid/", "https://ha.example.invalid:443", "https://HA.example.invalid"])
def test_an_edit_that_keeps_the_origin_keeps_the_token(owner_client, db, test_user, saved, same_origin):
    seed(db, test_user)
    response = owner_client.put(f"{KEYS}/home-assistant", json=body(same_origin, token=""))
    assert response.status_code == 200
    assert not response.json().get("token_cleared")
    assert decrypt_value(stored(db).api_key_encrypted) == TOKEN


def test_a_new_origin_with_a_new_token_stores_it_and_clears_nothing(owner_client, db, test_user, saved):
    seed(db, test_user)
    response = owner_client.put(f"{KEYS}/home-assistant", json=body("https://other.example.invalid", token="fresh-token"))
    assert not response.json().get("token_cleared")
    assert decrypt_value(stored(db).api_key_encrypted) == "fresh-token"


def test_a_row_with_an_empty_stored_address_counts_as_a_change(owner_client, db, test_user, saved):
    row = seed(db, test_user)
    row.endpoint_url = ""
    db.commit()
    response = owner_client.put(f"{KEYS}/home-assistant", json=body(ORIGIN_A, token=""))
    assert response.status_code == 200
    assert response.json()["token_cleared"] is True
    assert decrypt_value(stored(db).api_key_encrypted) == ""


def test_other_services_never_have_their_key_cleared_by_a_url_edit(owner_client, db, test_user, saved):
    seed(db, test_user, service="api-football", token="football-token")
    response = owner_client.put(f"{KEYS}/api-football", json=body("https://other.example.invalid", token="", service="api-football"))
    assert response.status_code == 200 and not response.json().get("token_cleared")
    assert decrypt_value(stored(db, "api-football").api_key_encrypted) == "football-token"


def test_the_response_and_the_listing_never_carry_the_token(owner_client, db, test_user, saved):
    seed(db, test_user, token="long-lived-secret-abcd")
    for response in (
        owner_client.get(KEYS), owner_client.get(f"{KEYS}/home-assistant"),
        owner_client.put(f"{KEYS}/home-assistant", json=body(ORIGIN_A, token="long-lived-secret-wxyz")),
    ):
        assert "long-lived-secret" not in response.text


# -- a save reaches the shared sign-in stop ---------------------------------------------------------------------


def test_a_create_and_an_update_each_tell_the_sign_in_stop_once(owner_client, db, test_user, saved):
    assert owner_client.post(KEYS, json=body(ORIGIN_A)).status_code == 201
    assert saved == ["home-assistant"]
    assert owner_client.put(f"{KEYS}/home-assistant", json=body(ORIGIN_A)).status_code == 200
    assert saved == ["home-assistant", "home-assistant"]


def test_a_refused_save_does_not_tell_the_sign_in_stop(owner_client, db, test_user, saved):
    seed(db, test_user)
    owner_client.put(f"{KEYS}/home-assistant", json=body("http://169.254.169.254"))
    owner_client.put(f"{KEYS}/home-assistant", json=body("ftp://x"))
    assert saved == []


def test_saving_the_key_again_clears_a_latched_token_end_to_end(owner_client, db, test_user, monkeypatch):
    monkeypatch.setattr(ha_access, "get_config", lambda: SimpleNamespace(service_api_key=ha_stack.SERVICE_KEY))
    seed(db, test_user, url=URL)
    fake = FakeHA(valid=())
    stack = ha_stack.build(fake, ha_stack.constant_source())

    async def trip():
        await stack.channel.request("GET", "/api/states")
        return await stack.channel.request("GET", "/api/states")

    assert asyncio.run(trip()).extensions.get("athena_ha_withheld") == "latched"
    fake.valid.add(TOKEN)
    assert owner_client.put(f"{KEYS}/home-assistant", json=body(URL)).status_code == 200
    after = asyncio.run(stack.channel.request("GET", "/api/states"))
    assert after.status_code == 200 and fake.failed_total == 1


# -- who may save ---------------------------------------------------------------------------------------------------


def test_a_viewer_cannot_save_the_key(viewer_client, db, saved):
    assert viewer_client.post(KEYS, json=body(ORIGIN_A)).status_code == 403
    assert db.query(ExternalAPIKey).count() == 0 and saved == []
