"""jarvis-web never forwards inbound headers wholesale (V4.9, otto L3).

Attestation, identity and relay headers are for jarvis-web alone; nothing
the browser or the proxy sent may reach the orchestrator.
"""
from __future__ import annotations

import ast

import pytest

from . import _jarvis_web_harness as h

SENSITIVE = {
    "X-Jarvis-Edge-Attestation": "secret-attestation-value-0123456789",
    "X-Jarvis-Edge-Class": "home",
    "X-authentik-username": "alice",
    "X-authentik-groups": "household",
    "X-Jarvis-Relay-Key": "relay-key-0123456789abcdef0123456789",
    "X-Jarvis-Relay-Client": "203.0.113.9",
    "Authorization": "Bearer should-not-forward",
}


def test_no_wholesale_header_forwarding_in_source():
    for path in sorted(h.BACKEND.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "headers":
                        text = ast.unparse(kw.value)
                        assert text not in {"request.headers", "dict(request.headers)", "websocket.headers"}, (path.name, text)


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_forwards_none_of_the_inbound_headers(monkeypatch, path):
    h.configure()
    out = h.install_outbound(monkeypatch)
    h.client().post(path, json={"message": "hi"}, headers={**h.via_proxy(h.LAN), **h.CSRF, **SENSITIVE})
    sent = [kw for method, url, kw in out.calls if url.endswith(("/query", "/query/stream"))]
    assert sent, "floor: the orchestrator was called"
    for kw in sent:
        rendered = repr(kw.get("headers", {})) + repr(kw.get("json", {}))
        for value in SENSITIVE.values():
            assert value not in rendered
