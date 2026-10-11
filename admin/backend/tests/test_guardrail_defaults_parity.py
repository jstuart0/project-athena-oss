"""One set of guardrail defaults in three places.

The orchestrator and gateway read `shared.assistant_profile.DEFAULT_GUARDRAILS`
when the admin API has no override; the admin API serves its own default dict
and validates saves with `VoiceResponseSettings`. They must agree, or a fresh
install and a saved-once install behave differently.
"""
from app.routes.settings import DEFAULT_ASSISTANT_PROFILE_CONFIG, VoiceResponseSettings
from shared.assistant_profile import (
    DEFAULT_GUARDRAILS,
    VOICE_MAX_SENTENCES_RANGE,
    VOICE_MAX_TOKENS_LONG_RANGE,
    VOICE_MAX_TOKENS_RANGE,
)

ADMIN_GUARDRAILS = DEFAULT_ASSISTANT_PROFILE_CONFIG["guardrails"]


def test_voice_response_defaults_agree_everywhere():
    shared_defaults = DEFAULT_GUARDRAILS["voice_response"]
    assert shared_defaults == {"max_sentences": 3, "max_tokens": 200, "max_tokens_long": 600, "ambient_fragment_gate": False}
    assert ADMIN_GUARDRAILS["voice_response"] == shared_defaults
    assert VoiceResponseSettings().model_dump() == shared_defaults


def test_validation_defaults_agree():
    assert ADMIN_GUARDRAILS["validation"] == DEFAULT_GUARDRAILS["validation"]


def test_the_model_ranges_match_the_read_time_clamp():
    fields = VoiceResponseSettings.model_fields
    tokens = {type(m).__name__: m for m in fields["max_tokens"].metadata}
    sentences = {type(m).__name__: m for m in fields["max_sentences"].metadata}
    assert (tokens["Ge"].ge, tokens["Le"].le) == VOICE_MAX_TOKENS_RANGE
    long_tokens = {type(m).__name__: m for m in fields["max_tokens_long"].metadata}
    assert (long_tokens["Ge"].ge, long_tokens["Le"].le) == VOICE_MAX_TOKENS_LONG_RANGE
    assert (sentences["Ge"].ge, sentences["Le"].le) == VOICE_MAX_SENTENCES_RANGE


def test_the_default_dicts_do_not_share_state():
    assert ADMIN_GUARDRAILS["voice_response"] is not DEFAULT_GUARDRAILS["voice_response"]
