"""`guardrails.voice_response` on the assistant-profile route is typed.

A save with an out-of-range or mistyped value is a 422; a legacy profile
without the section still loads (with defaults); and a stored value that is
out of range (hand-edited, or written by an older build) is clamped on the way
out, so a read never fails. Other guardrail keys stay free-form.
"""
import json
import math

import pytest

from app.auth.oidc import get_current_user
from app.models import SystemSetting
from app.routes.settings import ASSISTANT_PROFILE_SETTING_KEY
from main import app

URL = "/api/settings/assistant-profile"
PUBLIC_URL = "/api/settings/assistant-profile/public"
DEFAULTS = {"max_sentences": 3, "max_tokens": 200, "max_tokens_long": 600, "ambient_fragment_gate": False}


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user

    app.dependency_overrides[get_current_user] = _get_user
    yield client


def _profile(client, **voice_response):
    body = client.get(URL).json()
    if voice_response is not None:
        body["guardrails"]["voice_response"] = voice_response
    return body


def _stored(db, guardrails_voice_response):
    body = {
        "assistant_name": "Jarvis", "project_name": "Athena", "identity": "an assistant",
        "persona_traits": [], "communication_style": [],
        "guardrails": {"voice_response": guardrails_voice_response},
    }
    # allow_nan so the NaN case reaches the database the way a hand edit could
    db.add(SystemSetting(key=ASSISTANT_PROFILE_SETTING_KEY, value=json.dumps(body, allow_nan=True), category="assistant"))
    db.commit()


def test_a_valid_save_round_trips(owner_client):
    wanted = {"max_sentences": 4, "max_tokens": 300, "max_tokens_long": 900, "ambient_fragment_gate": True}
    resp = owner_client.post(URL, json=_profile(owner_client, **wanted))
    assert resp.status_code == 200, resp.text
    assert owner_client.get(URL).json()["guardrails"]["voice_response"] == wanted
    assert owner_client.get(PUBLIC_URL).json()["guardrails"]["voice_response"] == wanted


def test_the_boundaries_save(owner_client):
    for wanted in (
        {"max_sentences": 1, "max_tokens": 32, "max_tokens_long": 64, "ambient_fragment_gate": False},
        {"max_sentences": 10, "max_tokens": 1024, "max_tokens_long": 2048, "ambient_fragment_gate": True},
    ):
        assert owner_client.post(URL, json=_profile(owner_client, **wanted)).status_code == 200
        assert owner_client.get(URL).json()["guardrails"]["voice_response"] == wanted


@pytest.mark.parametrize("bad", [
    {"max_tokens": 5000},
    {"max_tokens": 1025},
    {"max_tokens": 31},
    {"max_tokens": 0},
    {"max_tokens": -5},
    {"max_tokens": "300"},
    {"max_tokens": 200.5},
    {"max_tokens_long": 63},
    {"max_tokens_long": 2049},
    {"max_tokens_long": "600"},
    {"max_tokens_long": None},
    {"max_tokens_long": 600.5},
    {"max_tokens": None},
    {"max_tokens": True},
    {"max_sentences": 0},
    {"max_sentences": 11},
    {"max_sentences": "3"},
    {"ambient_fragment_gate": "yes"},
    {"ambient_fragment_gate": "true"},
    {"ambient_fragment_gate": 1},
    {"ambient_fragment_gate": None},
    {"max_tokenz": 200},
])
def test_a_bad_value_is_a_422_and_nothing_is_saved(owner_client, db, bad):
    before = owner_client.get(URL).json()
    resp = owner_client.post(URL, json=_profile(owner_client, **bad))
    assert resp.status_code == 422, (bad, resp.text)
    assert db.query(SystemSetting).filter(SystemSetting.key == ASSISTANT_PROFILE_SETTING_KEY).count() == 0
    assert owner_client.get(URL).json() == before


def test_a_voice_response_that_is_not_an_object_is_a_422(owner_client):
    body = owner_client.get(URL).json()
    for bad in ("loud", 5, [1], None):
        body["guardrails"]["voice_response"] = bad
        assert owner_client.post(URL, json=body).status_code == 422, bad


def test_a_partial_section_is_filled_with_defaults(owner_client):
    assert owner_client.post(URL, json=_profile(owner_client, max_tokens=100)).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["voice_response"] == {**DEFAULTS, "max_tokens": 100}


def test_a_legacy_profile_without_the_section_still_saves_and_loads(owner_client):
    body = owner_client.get(URL).json()
    del body["guardrails"]["voice_response"]
    assert owner_client.post(URL, json=body).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["voice_response"] == DEFAULTS


def test_a_stored_legacy_profile_without_the_section_loads_with_defaults(owner_client, db):
    body = {
        "assistant_name": "Jarvis", "project_name": "Athena", "identity": "an assistant",
        "persona_traits": [], "communication_style": [], "guardrails": {"validation": {"min_response_chars": 7}},
    }
    db.add(SystemSetting(key=ASSISTANT_PROFILE_SETTING_KEY, value=json.dumps(body), category="assistant"))
    db.commit()
    guardrails = owner_client.get(URL).json()["guardrails"]
    assert guardrails["voice_response"] == DEFAULTS
    assert guardrails["validation"]["min_response_chars"] == 7


def test_other_guardrail_keys_stay_free_form(owner_client):
    body = _profile(owner_client, **DEFAULTS)
    body["guardrails"]["something_new"] = {"anything": [1, "two"]}
    assert owner_client.post(URL, json=body).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["something_new"] == {"anything": [1, "two"]}


@pytest.mark.parametrize("stored,expected", [
    ({"max_tokens": 99999, "max_sentences": 99, "max_tokens_long": 99999, "ambient_fragment_gate": True},
     {"max_tokens": 1024, "max_sentences": 10, "max_tokens_long": 2048, "ambient_fragment_gate": True}),
    ({"max_tokens": 1, "max_sentences": -4, "max_tokens_long": 1},
     {"max_tokens": 32, "max_sentences": 1, "max_tokens_long": 64, "ambient_fragment_gate": False}),
    ({"max_tokens": "300", "max_sentences": "2", "max_tokens_long": "700"},
     {"max_tokens": 300, "max_sentences": 2, "max_tokens_long": 700, "ambient_fragment_gate": False}),
    ({"max_tokens_long": "abc"}, DEFAULTS),
    ({"max_tokens": "abc", "max_sentences": None}, DEFAULTS),
    ({"max_tokens": float("nan"), "max_sentences": float("inf")}, DEFAULTS),
    ({"ambient_fragment_gate": "yes please"}, DEFAULTS),
    ("not an object", DEFAULTS),
])
def test_a_bad_stored_value_is_clamped_on_read_not_an_error(owner_client, db, stored, expected):
    _stored(db, stored)
    for url in (URL, PUBLIC_URL):
        resp = owner_client.get(url)
        assert resp.status_code == 200, (stored, resp.text)
        assert resp.json()["guardrails"]["voice_response"] == expected


def test_nan_really_reaches_the_database_in_the_nan_case(db):
    _stored(db, {"max_tokens": float("nan")})
    raw = db.query(SystemSetting).filter(SystemSetting.key == ASSISTANT_PROFILE_SETTING_KEY).one().value
    assert math.isnan(json.loads(raw)["guardrails"]["voice_response"]["max_tokens"])


# --- a save that doesn't mention voice_response keeps what is stored ------------------------------


def _ui_payload(client):
    """The shape the admin form posts: every text rule, no voice_response."""
    body = client.get(URL).json()
    body["guardrails"].pop("voice_response", None)
    body["assistant_name"] = "Renamed"
    return body


def test_the_form_save_keeps_voice_response(owner_client):
    wanted = {"max_sentences": 5, "max_tokens": 400, "max_tokens_long": 1200, "ambient_fragment_gate": True}
    assert owner_client.post(URL, json=_profile(owner_client, **wanted)).status_code == 200
    resp = owner_client.post(URL, json=_ui_payload(owner_client))
    assert resp.status_code == 200, resp.text
    saved = owner_client.get(URL).json()
    assert saved["assistant_name"] == "Renamed"
    assert saved["guardrails"]["voice_response"] == wanted


def test_a_partial_voice_response_keeps_the_other_stored_keys(owner_client):
    wanted = {"max_sentences": 5, "max_tokens": 400, "max_tokens_long": 1200, "ambient_fragment_gate": True}
    assert owner_client.post(URL, json=_profile(owner_client, **wanted)).status_code == 200
    assert owner_client.post(URL, json=_profile(owner_client, max_tokens=300)).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["voice_response"] == {**wanted, "max_tokens": 300}


def test_guardrail_keys_the_form_did_not_send_survive_a_save(owner_client):
    body = _profile(owner_client, **DEFAULTS)
    body["guardrails"]["kept_rule"] = ["stays"]
    assert owner_client.post(URL, json=body).status_code == 200
    sent = _ui_payload(owner_client)
    sent["guardrails"].pop("kept_rule")          # the form never knew about this key
    assert owner_client.post(URL, json=sent).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["kept_rule"] == ["stays"]


def test_what_the_form_does_send_replaces_the_stored_value(owner_client):
    body = _profile(owner_client, **DEFAULTS)
    body["guardrails"]["accuracy"] = ["first"]
    assert owner_client.post(URL, json=body).status_code == 200
    body["guardrails"]["accuracy"] = ["second"]
    assert owner_client.post(URL, json=body).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["accuracy"] == ["second"]


def test_a_save_over_a_hand_edited_stored_value_comes_back_in_range(owner_client, db):
    _stored(db, {"max_tokens": 99999, "max_tokens_long": 1, "max_sentences": 2})
    assert owner_client.post(URL, json=_ui_payload(owner_client)).status_code == 200
    stored = json.loads(db.query(SystemSetting).filter(SystemSetting.key == ASSISTANT_PROFILE_SETTING_KEY).one().value)
    assert stored["guardrails"]["voice_response"] == {
        "max_sentences": 2, "max_tokens": 1024, "max_tokens_long": 64, "ambient_fragment_gate": False,
    }


# --- the admin form carries the controls -------------------------------------------------------------


def _frontend(name):
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "admin" / "frontend"
    return (root / name).read_text(encoding="utf-8")


CONTROLS = {
    "assistant-voice-max-sentences": "max_sentences",
    "assistant-voice-max-tokens": "max_tokens",
    "assistant-voice-max-tokens-long": "max_tokens_long",
    "assistant-ambient-fragment-gate": "ambient_fragment_gate",
}


def test_the_form_has_a_labelled_control_for_every_voice_response_field():
    import re

    from app.routes.settings import VoiceResponseSettings

    html, js = _frontend("index.html"), _frontend("app.js")
    assert set(CONTROLS.values()) == set(VoiceResponseSettings.model_fields)
    for element_id, field in CONTROLS.items():
        assert f'id="{element_id}"' in html, element_id
        assert re.search(rf'<label[^>]*for="{element_id}"|<label[^>]*>\s*<input[^>]*id="{element_id}"', html), f"{element_id} has a label"
        assert js.count(f"getElementById('{element_id}')") == 2, f"{element_id}: loaded and saved"
        assert field in js
    assert 'min="64" max="2048"' in html and 'min="32" max="1024"' in html and 'min="1" max="10"' in html


def test_the_form_sends_voice_response_and_never_builds_html_from_it():
    js = _frontend("app.js")
    start = js.index("async function saveAssistantProfileSettings")
    end = js.index("async function loadServices")
    block = js[start:end]
    assert "voice_response: {" in block
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block
