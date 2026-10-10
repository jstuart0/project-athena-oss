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
        "max_sentences": 2, "max_tokens": 1024, "max_tokens_long": 1024, "ambient_fragment_gate": False,
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
        assert js.count(f"getElementById('{element_id}')") >= 2, f"{element_id}: loaded and saved"
        assert field in js
    assert 'min="64" max="2048"' in html and 'min="32" max="1024"' in html and 'min="1" max="10"' in html


def test_the_form_sends_voice_response_and_never_builds_html_from_it():
    js = _frontend("app.js")
    start = js.index("async function saveAssistantProfileSettings")
    end = js.index("async function loadServices")
    block = js[start:end]
    assert "voice_response: {" in block
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block


# --- the long limit may not be below the short one ---------------------------------------------------------------


def test_a_save_with_the_long_limit_below_the_short_one_is_a_422(owner_client, db):
    resp = owner_client.post(URL, json=_profile(owner_client, max_tokens=500, max_tokens_long=400))
    assert resp.status_code == 422, resp.text
    assert db.query(SystemSetting).filter(SystemSetting.key == ASSISTANT_PROFILE_SETTING_KEY).count() == 0


def test_equal_limits_are_fine(owner_client):
    assert owner_client.post(URL, json=_profile(owner_client, max_tokens=500, max_tokens_long=500)).status_code == 200


def test_raising_only_the_short_limit_past_the_stored_long_one_is_a_422_not_a_500(owner_client):
    assert owner_client.post(URL, json=_profile(owner_client, max_tokens=200, max_tokens_long=600)).status_code == 200
    resp = owner_client.post(URL, json=_profile(owner_client, max_tokens=700))     # stored long limit is 600
    assert resp.status_code == 422, resp.text
    assert owner_client.get(URL).json()["guardrails"]["voice_response"]["max_tokens"] == 200


def test_raising_only_the_short_limit_within_the_stored_long_one_is_fine(owner_client):
    assert owner_client.post(URL, json=_profile(owner_client, max_tokens=200, max_tokens_long=900)).status_code == 200
    assert owner_client.post(URL, json=_profile(owner_client, max_tokens=800)).status_code == 200
    assert owner_client.get(URL).json()["guardrails"]["voice_response"]["max_tokens"] == 800


def test_a_stored_pair_in_the_wrong_order_is_raised_on_read(owner_client, db):
    _stored(db, {"max_tokens": 900, "max_tokens_long": 300})
    voice = owner_client.get(URL).json()["guardrails"]["voice_response"]
    assert voice["max_tokens"] == 900 and voice["max_tokens_long"] == 900


# --- the form: plain-language help, accessibility, field-level validation --------------------------------------


def test_the_voice_fieldset_is_labelled_and_every_control_is_described():
    import re

    html = _frontend("index.html")
    start = html.index("<fieldset")
    block = html[start:html.index("</fieldset>", start)]
    assert "<legend" in block and "Voice response" in block
    for element_id in CONTROLS:
        assert f'aria-describedby="{element_id}-help"' in block, element_id
        assert f'id="{element_id}-help"' in block, element_id
    assert re.search(r"about &frac34; of a word|3/4 of a word|&frac34; of a word", block)
    assert "20&ndash;30 seconds" in block
    assert "Sorry, I didn" in block and "Off by default" in block


def test_the_two_sentence_limits_and_the_minimum_length_are_told_apart():
    html = _frontend("index.html")
    assert "Simple responses: max sentences" in html and "Spoken answers: max sentences" in html
    assert "Minimum length for an unfinished answer (characters)" in html
    assert "Max Simple Sentences" not in html


def test_every_control_in_the_assistant_card_has_a_label_for_it():
    import re

    html = _frontend("index.html")
    start = html.index('id="assistant-name"') - 600
    end = html.index('id="assistant-profile-status"')
    card = html[start:end]
    ids = re.findall(r'<(?:input|textarea)[^>]*\sid="(assistant-[a-z-]+)"', card)
    assert len(ids) >= 17
    for element_id in ids:
        assert re.search(rf'<label[^>]*for="{element_id}"|<label[^>]*>\s*<input[^>]*id="{element_id}"', card), element_id


def test_the_form_checks_its_fields_before_posting_and_does_not_touch_apirequest():
    js = _frontend("app.js")
    start = js.index("function applyAssistantCrossFieldRules")
    save = js.index("async function saveAssistantProfileSettings")
    check = js[start:save]
    assert "reportValidity" in check and "setCustomValidity" in check and "checkValidity" in check
    assert "longTokens < tokens" in check, "the long limit may not be below the short one"
    assert "minChars > maxChars" in check, "the minimum length may not exceed the maximum"
    assert "Must be at least the spoken answer limit" in check
    body = js[save:js.index("async function loadServices")]
    assert body.index("validateAssistantNumberFields()") < body.index("apiRequest(")
    assert "innerHTML" not in check + body and "insertAdjacentHTML" not in check + body


def test_an_invalid_field_is_marked_for_assistive_tech_and_its_message_is_shown():
    js = _frontend("app.js")
    start = js.index("function markAssistantFieldValidity")
    block = js[start:js.index("async function saveAssistantProfileSettings")]
    assert "setAttribute('aria-invalid', invalid ? 'true' : 'false')" in block
    assert "classList.toggle('border-red-500', invalid)" in block
    assert "firstInvalid.validationMessage" in block
    assert "text-red-400" in block


def test_editing_a_field_clears_its_error():
    js = _frontend("app.js")
    start = js.index("function wireAssistantFieldValidation")
    block = js[start:js.index("async function saveAssistantProfileSettings")]
    assert "addEventListener('input'" in block
    assert "setCustomValidity('')" in block and "markAssistantFieldValidity(el, false)" in block
    assert "status.textContent = ''" in block
    assert "wireAssistantFieldValidation();" in js[js.index("async function loadAssistantProfileSettings"):js.index("function applyAssistantCrossFieldRules")]


def test_every_numeric_field_the_form_checks_exists_and_is_required():
    import re

    js, html = _frontend("app.js"), _frontend("index.html")
    ids = re.findall(r"'(assistant-[a-z-]+)',", js[js.index("const ASSISTANT_NUMBER_FIELD_IDS"):js.index("function markAssistantFieldValidity")])
    assert len(ids) == 6
    for element_id in ids:
        tag = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', html).group(0)
        assert "required" in tag and 'type="number"' in tag and "step=" in tag, element_id


# --- other numeric guardrails are checked on save too ---------------------------------------------------------------------


def _with(client, section, **values):
    body = client.get(URL).json()
    body["guardrails"][section] = {**body["guardrails"][section], **values}
    return body


@pytest.mark.parametrize("bad", [0, 11, -1, None, "3", 2.5, True])
def test_a_bad_simple_response_sentence_limit_is_a_422(owner_client, bad):
    assert owner_client.post(URL, json=_with(owner_client, "simple_response", max_sentences=bad)).status_code == 422


@pytest.mark.parametrize("good", [1, 2, 10])
def test_a_good_simple_response_sentence_limit_saves(owner_client, good):
    assert owner_client.post(URL, json=_with(owner_client, "simple_response", max_sentences=good)).status_code == 200


def test_a_minimum_length_above_the_maximum_is_a_422(owner_client):
    resp = owner_client.post(URL, json=_with(owner_client, "validation", min_response_chars=500, max_response_chars=100))
    assert resp.status_code == 422, resp.text


def test_equal_minimum_and_maximum_lengths_save(owner_client):
    assert owner_client.post(URL, json=_with(owner_client, "validation", min_response_chars=50, max_response_chars=50)).status_code == 200
