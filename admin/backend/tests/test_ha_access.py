"""admin-backend's one door to Home Assistant (app/services/ha_access.py).

The stack under test is the real resolver, latch and channel over the repository's
fake Home Assistant; nothing here opens a socket.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import structlog

from app.models import ExternalAPIKey
from app.services import ha_access
from app.utils.encryption import encrypt_value
from shared import ha_ws
from shared.ha_credentials import HA_KEY_SERVICE
from tests import ha_stack
from tests.ha_stack import TOKEN, URL, FakeHA

PHRASES = {
    ha_access.NOT_SET_UP, ha_access.TOKEN_REJECTED, ha_access.REFUSED, ha_access.BLOCKED, ha_access.CANT_CONNECT,
    ha_access.TIMED_OUT, ha_access.FAILED, ha_access.BUSY, ha_access.BAD_REQUEST,
}


def _row(owner, *, url=URL, token=TOKEN, enabled=True):
    return ExternalAPIKey(
        service_name=HA_KEY_SERVICE, api_name="Home Assistant", api_key_encrypted=encrypt_value(token),
        endpoint_url=url, enabled=enabled, created_by_id=owner.id,
    )


# -- the stored key -----------------------------------------------------------------------------------


def test_the_enabled_key_is_found_decrypted(db, test_user):
    db.add(_row(test_user))
    db.commit()
    lookup = ha_access.lookup_from_db(db)
    assert (lookup.status, lookup.url, lookup.token) == ("found", URL, TOKEN)


def test_no_row_and_a_disabled_row_are_absent(db, test_user):
    assert ha_access.lookup_from_db(db).status == "absent"
    db.add(_row(test_user, enabled=False))
    db.commit()
    assert ha_access.lookup_from_db(db).status == "absent"


def test_a_row_that_cannot_be_decrypted_is_unavailable_not_absent(db, test_user):
    row = _row(test_user)
    row.api_key_encrypted = "not-a-valid-ciphertext"
    db.add(row)
    db.commit()
    assert ha_access.lookup_from_db(db).status == "unavailable"


def test_reading_the_key_does_not_touch_last_used(db, test_user):
    db.add(_row(test_user))
    db.commit()
    for _ in range(3):
        ha_access.lookup_from_db(db)
    db.expire_all()
    assert db.query(ExternalAPIKey).one().last_used is None


# -- failures reach the browser as closed phrases ---------------------------------------------------------


def _response(status, withheld=None):
    extensions = {"athena_ha_withheld": withheld} if withheld else {}
    return httpx.Response(status, request=httpx.Request("GET", URL + "/api/states"), extensions=extensions)


FAILURES = [
    (ha_ws.HomeAssistantNotConfiguredError("secret detail"), 409, ha_access.NOT_SET_UP),
    (ha_ws.AuthInvalid("secret detail"), 502, ha_access.TOKEN_REJECTED),
    (ha_ws.HABanned(), 502, ha_access.BLOCKED),
    (ha_ws.HAWithheld("gated"), 503, ha_access.BUSY),
    (ha_ws.HAWithheld("latched"), 502, ha_access.TOKEN_REJECTED),
    (ha_ws.HAWithheld("admin_only"), 502, ha_access.REFUSED),
    (asyncio.TimeoutError("secret detail"), 504, ha_access.TIMED_OUT),
    (httpx.ReadTimeout("secret detail"), 504, ha_access.TIMED_OUT),
    (httpx.ConnectError("secret detail"), 502, ha_access.CANT_CONNECT),
    (ConnectionRefusedError("secret detail"), 502, ha_access.CANT_CONNECT),
    (ValueError("secret detail"), 400, ha_access.BAD_REQUEST),
    (RuntimeError("secret detail"), 502, ha_access.FAILED),
    (_response(401), 502, ha_access.TOKEN_REJECTED),
    (_response(403), 502, ha_access.REFUSED),
    (_response(500), 502, ha_access.FAILED),
    (_response(503, "gated"), 503, ha_access.BUSY),
    (_response(401, "latched"), 502, ha_access.TOKEN_REJECTED),
    (_response(401, "budget"), 502, ha_access.TOKEN_REJECTED),
    (_response(401, "admin_only"), 502, ha_access.REFUSED),
]


@pytest.mark.parametrize("failure,status,phrase", FAILURES, ids=[f"{type(f).__name__}-{i}" for i, (f, _, _) in enumerate(FAILURES)])
def test_every_failure_maps_to_one_closed_phrase(failure, status, phrase):
    error = ha_access.http_error(failure)
    assert (error.status_code, error.detail) == (status, phrase)
    assert ha_access.error_text(failure) == phrase
    assert "secret" not in error.detail and error.detail in PHRASES


def test_the_http_status_error_maps_like_its_response():
    response = _response(401)
    error = httpx.HTTPStatusError("secret detail", request=response.request, response=response)
    assert ha_access.error_text(error) == ha_access.TOKEN_REJECTED


# -- the doors -----------------------------------------------------------------------------------------------


def test_before_the_stack_exists_the_doors_raise_not_configured():
    with pytest.raises(ha_ws.HomeAssistantNotConfiguredError):
        ha_access.channel()
    with pytest.raises(ha_ws.HomeAssistantNotConfiguredError):
        ha_access.credentials()
    with pytest.raises(ha_ws.HomeAssistantNotConfiguredError):
        ha_access.ws_session(ha_ws.PIPELINES)


def test_the_websocket_door_opens_the_pipelines_profile_only():
    ha_stack.build(FakeHA(), ha_stack.constant_source())
    for profile in (ha_ws.REGISTRY_READ, ha_ws.PROOF):
        with pytest.raises(TypeError):
            ha_access.ws_session(profile)

    async def go():
        async with ha_access.ws_session(ha_ws.PIPELINES) as ws:
            return await ws.command({"type": "assist_pipeline/pipeline/list"})

    assert asyncio.run(go())["success"] is True


def test_the_pipelines_session_cannot_send_a_device_command():
    ha_stack.build(FakeHA(), ha_stack.constant_source())

    async def go():
        async with ha_access.ws_session(ha_ws.PIPELINES) as ws:
            await ws.command({"type": "call_service", "domain": "light", "service": "turn_on"})

    with pytest.raises(Exception) as caught:
        asyncio.run(go())
    assert "call_service" not in ha_access.http_error(caught.value).detail


# -- the operator saved the key ------------------------------------------------------------------------------


@pytest.fixture
def same_service_key(monkeypatch):
    monkeypatch.setattr(ha_access, "get_config", lambda: SimpleNamespace(service_api_key=ha_stack.SERVICE_KEY))


def _latched_stack():
    fake = FakeHA(valid=())
    stack = ha_stack.build(fake, ha_stack.constant_source())

    async def trip():
        first = await stack.channel.request("GET", "/api/states")
        second = await stack.channel.request("GET", "/api/states")
        return first, second

    first, second = asyncio.run(trip())
    assert first.status_code == 401 and second.extensions.get("athena_ha_withheld") == "latched"
    assert fake.failed_total == 1 and fake.rest_sent == [], "the dedicated proof failed; the request itself was never sent"
    return fake, stack


def test_saving_the_key_again_lets_exactly_one_gated_attempt_through_and_it_can_recover(same_service_key, test_user):
    fake, stack = _latched_stack()
    fake.valid.add(TOKEN)

    async def go():
        before = await stack.channel.request("GET", "/api/states")
        await ha_access.key_saved(_row(test_user))
        after = await stack.channel.request("GET", "/api/states")
        again = await stack.channel.request("GET", "/api/states")
        return before, after, again

    before, after, again = asyncio.run(go())
    assert before.extensions.get("athena_ha_withheld") == "latched", "without a save the stop holds"
    assert after.status_code == 200 and again.status_code == 200
    assert fake.failed_total == 1, "the recovery added no failed sign-in"


def test_saving_a_still_wrong_token_costs_one_more_failed_sign_in_and_stops_again(same_service_key, test_user):
    fake, stack = _latched_stack()

    async def go():
        await ha_access.key_saved(_row(test_user))
        first = await stack.channel.request("GET", "/api/states")
        second = await stack.channel.request("GET", "/api/states")
        third = await stack.channel.request("GET", "/api/states")
        return first, second, third

    first, second, third = asyncio.run(go())
    assert first.status_code == 401
    assert second.extensions.get("athena_ha_withheld") and third.extensions.get("athena_ha_withheld")
    assert fake.failed_total == 2, "exactly one gated attempt per save"


def test_with_the_budget_spent_a_save_allows_exactly_one_attempt_then_the_budget_withholds_again(same_service_key, test_user):
    fake = FakeHA(valid=())
    stack = ha_stack.build(fake, ha_stack.constant_source())

    async def go():
        await stack.redis.hset(stack.latch._k("budget:v2"), mapping={"n": "3", "r": "0", "spent_at": "2026-10-11T11:59:00+00:00"})
        spent = await stack.channel.request("GET", "/api/states")
        before = fake.failed_total
        await ha_access.key_saved(_row(test_user))
        attempt = await stack.channel.request("GET", "/api/states")
        used = fake.failed_total - before
        after = await stack.channel.request("GET", "/api/states")
        return spent, attempt, used, fake.failed_total - before - used, after

    spent, attempt, used, extra, after = asyncio.run(go())
    assert spent.extensions.get("athena_ha_withheld") == "budget"
    assert used == 1, "exactly one gated attempt after the save"
    assert extra == 0 and after.extensions.get("athena_ha_withheld") in ("budget", "latched"), "then it withholds again"


def test_saving_the_key_applies_it_now_not_at_the_next_minute(same_service_key, test_user):
    holder = {"token": "old-token"}

    async def source():
        from shared.ha_credentials import KeyLookup

        return KeyLookup.found(URL, holder["token"])

    fake = FakeHA(valid=("old-token", "new-token"))
    stack = ha_stack.build(fake, source)
    assert stack.resolver.current().token.reveal() == "old-token"
    holder["token"] = "new-token"
    asyncio.run(ha_access.key_saved(_row(test_user, token="new-token")))
    assert stack.resolver.current().token.reveal() == "new-token"


def test_key_saved_before_the_stack_exists_does_nothing(test_user):
    asyncio.run(ha_access.key_saved(_row(test_user)))


def test_key_saved_never_raises(same_service_key, test_user):
    ha_stack.build(FakeHA(), ha_stack.constant_source())
    row = _row(test_user)
    row.api_key_encrypted = "not-a-valid-ciphertext"
    asyncio.run(ha_access.key_saved(row))


# -- lifecycle -------------------------------------------------------------------------------------------------


def test_reset_for_test_closes_the_channel_and_drops_the_singletons():
    stack = ha_stack.build(FakeHA(), ha_stack.constant_source())
    ha_access.reset_for_test()
    assert stack.channel._client.is_closed
    with pytest.raises(ha_ws.HomeAssistantNotConfiguredError):
        ha_access.channel()


def test_init_starts_one_refresh_task_and_close_ends_it(monkeypatch):
    monkeypatch.setattr(ha_access, "admin_db_source", lambda: ha_stack.constant_source())

    async def go():
        before = {t for t in asyncio.all_tasks()}
        await ha_access.init(None, inner=FakeHA().rest_transport("X"), getaddrinfo=ha_stack._resolves)
        started = {t for t in asyncio.all_tasks()} - before
        assert ha_access.channel().configured
        await ha_access.close()
        await asyncio.sleep(0)
        return started

    started = asyncio.run(go())
    assert len(started) == 1 and all(t.done() for t in started)
    with pytest.raises(ha_ws.HomeAssistantNotConfiguredError):
        ha_access.channel()


def test_a_second_init_replaces_the_first_and_closes_it(monkeypatch):
    monkeypatch.setattr(ha_access, "admin_db_source", lambda: ha_stack.constant_source())

    async def go():
        await ha_access.init(None, inner=FakeHA().rest_transport("X"), getaddrinfo=ha_stack._resolves)
        first = ha_access.channel()
        await ha_access.init(None, inner=FakeHA().rest_transport("X"), getaddrinfo=ha_stack._resolves)
        second = ha_access.channel()
        await ha_access.close()
        return first, second

    first, second = asyncio.run(go())
    assert first is not second and first._client.is_closed


# -- the admin half of secret hygiene ---------------------------------------------------------------------------


def test_no_token_or_address_reaches_a_log_a_repr_or_an_error_detail(same_service_key, test_user):
    fake, stack = _latched_stack()
    leaked = []
    with structlog.testing.capture_logs() as logs:
        for failure in (
            _response(401), ha_ws.AuthInvalid(TOKEN), httpx.ConnectError(f"{URL} {TOKEN}"), ValueError(TOKEN),
            RuntimeError(f"{URL}/api/states {TOKEN}"),
        ):
            error = ha_access.http_error(failure)
            leaked.append(error.detail)
        asyncio.run(ha_access.key_saved(_row(test_user)))
    surfaces = [repr(logs), repr(leaked), repr(sorted(stack.redis._data.items())), repr(stack.resolver.current()), str(stack.resolver.current()), repr(ha_access.credentials())]
    for surface in surfaces:
        assert TOKEN not in surface and "ha-fixture.invalid" not in surface
