"""Cross-image trust set (V4.5): every caller_trust jarvis-web can send is
one the orchestrator's QueryRequest accepts."""
from __future__ import annotations

import typing

from . import _jarvis_web_harness as jh
from . import _public_audience_harness as oh


def test_jarvis_trust_values_accepted_by_orchestrator():
    sent = set(jh.caller_auth.UPSTREAM_TRUST.values())
    accepted = set(typing.get_args(typing.get_args(oh.main.QueryRequest.model_fields["caller_trust"].annotation)[0]))
    assert "web_local" in sent
    assert sent <= accepted


def test_every_browser_class_maps_to_a_trust_value():
    ca = jh.caller_auth
    for cls in ca.BROWSER_CLASSES | {ca.CLASS_PUBLIC}:
        assert cls in ca.UPSTREAM_TRUST
    assert ca.UPSTREAM_TRUST[ca.CLASS_GUEST_NET] == "web_guest_net"
