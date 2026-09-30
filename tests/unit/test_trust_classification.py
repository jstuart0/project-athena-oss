"""Every caller_trust value is classified for both guest-name legs.

A new tag added to QueryRequest.caller_trust fails here until it's added to
EXPECTED: the request leg (a guest name sent in the request context), the
device leg (a guest session matched by device fingerprint) and the
household member's first name are each an allowlist.
"""
from __future__ import annotations

import typing
from types import SimpleNamespace

import pytest

from . import _public_audience_harness as h

# value -> (request leg keeps guest_name, device leg keeps guest_name, speaker_first_name kept, public scrub)
EXPECTED = {
    None: (False, False, False, False),
    "household": (False, False, False, False),
    "sms": (True, False, False, False),
    "web_authenticated": (False, False, True, False),
    "web_local": (False, False, False, False),
    "web_guest_net": (True, False, False, False),
    "web_public": (False, False, False, True),
}


def _values():
    annotation = h.main.QueryRequest.model_fields["caller_trust"].annotation
    return list(typing.get_args(typing.get_args(annotation)[0])) + [None]


def test_every_value_is_classified():
    assert set(_values()) == set(EXPECTED)


@pytest.mark.parametrize("trust", list(EXPECTED), ids=[str(k) for k in EXPECTED])
def test_legs(trust):
    from orchestrator.helpers import build_query_context

    request_leg, device_leg, first_name, public = EXPECTED[trust]
    request = SimpleNamespace(
        caller_trust=trust,
        context={"guest_id": 7, "guest_name": "Gina Guest", "speaker_first_name": "Pat", "location_override": {"a": 1}},
    )
    context = build_query_context(request, None, server_mode="guest", degraded=False)
    assert ("guest_name" in context) == request_leg
    assert ("speaker_first_name" in context) == first_name
    if public:
        assert context == {"location_override": {"a": 1}}

    device_only = SimpleNamespace(caller_trust=trust, context={})
    context = build_query_context(device_only, {"guest_id": 9, "guest_name": "Bob Device"}, server_mode="guest", degraded=False)
    assert ("guest_name" in context) == device_leg
    assert "guest_id" not in context or device_leg


@pytest.mark.parametrize("trust", list(EXPECTED), ids=[str(k) for k in EXPECTED])
def test_degraded_house_names_nobody(trust):
    """A degraded mode service drops both guest-name legs and the member's
    first name, whatever the caller (positive control: test_legs)."""
    from orchestrator.helpers import build_query_context

    request = SimpleNamespace(
        caller_trust=trust,
        context={"guest_id": 7, "guest_name": "Gina Guest", "speaker_first_name": "Pat"},
    )
    context = build_query_context(request, {"guest_id": 9, "guest_name": "Bob Device"}, server_mode="guest", degraded=True)
    assert not {"guest_name", "guest_id", "speaker_first_name"} & set(context)


def _vectors():
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures" / "speaker_first_name_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("raw,expected", _vectors(), ids=[repr(v[0])[:24] for v in _vectors()])
def test_orchestrator_recleans_with_the_shared_vectors(raw, expected):
    from orchestrator.helpers import build_query_context, clean_speaker_first_name

    assert clean_speaker_first_name(raw) == expected
    request = SimpleNamespace(caller_trust="web_authenticated", context={"speaker_first_name": raw})
    context = build_query_context(request, None, server_mode="guest", degraded=False)
    assert context.get("speaker_first_name") == expected
