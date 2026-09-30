"""The signed-in household member's first name: cleaning and header decoding.

The vectors are shared with the orchestrator's re-cleaning test, so the two
sides can't drift. Header cases go through a real attested edge request:
Starlette hands header values over latin-1-decoded, so a UTF-8 name on the
wire must be re-decoded before it's cleaned.
"""
from __future__ import annotations

import json

import pytest

from . import _jarvis_web_harness as h

caller_auth = h.caller_auth
VECTORS = json.loads((h.REPO_ROOT / "tests" / "fixtures" / "speaker_first_name_vectors.json").read_text(encoding="utf-8"))
SECRET = "edge-attestation-current-7f3a9c2e1b8d4f6a"
EDGE_ENV = {**h.HOME_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household"}


@pytest.fixture(autouse=True)
def _reset():
    h.configure(EDGE_ENV)
    yield
    h.configure()


@pytest.mark.parametrize("raw,expected", VECTORS, ids=[repr(v[0])[:24] for v in VECTORS])
def test_vectors(raw, expected):
    assert caller_auth._speaker_first_name(raw) == expected


def _signed_in_chat(monkeypatch, name_value):
    out = h.install_outbound(monkeypatch)
    headers = [
        ("x-forwarded-for", h.INTERNET),
        ("x-jarvis-edge-class", "authenticated"),
        ("x-jarvis-edge-attestation", SECRET),
        ("x-authentik-username", "pat"),
        ("x-authentik-groups", "household"),
        ("x-jarvis-request", "1"),
    ]
    if name_value is not None:
        headers.append(("x-authentik-name", name_value))
    resp = h.client().post("/api/chat", json={"message": "hi"}, headers=headers)
    assert resp.status_code == 200
    return out.orchestrator_bodies()[-1]


@pytest.mark.parametrize("wire,expected", [
    ("José Example".encode("utf-8"), "José"),
    ("Zoë".encode("utf-8"), "Zoë"),
])
def test_utf8_header_is_decoded(monkeypatch, wire, expected):
    body = _signed_in_chat(monkeypatch, wire)
    assert body["caller_trust"] == "web_authenticated"
    assert body["context"].get("speaker_first_name") == expected


def _edge_scope_headers(name_bytes):
    """Headers exactly as the ASGI server hands them over: raw bytes, read
    by Starlette as latin-1. (The test client can't carry non-UTF-8 header
    bytes: it re-encodes every value as UTF-8.)"""
    from starlette.datastructures import Headers

    raw = [
        (b"x-forwarded-for", h.INTERNET.encode()),
        (b"x-jarvis-edge-class", b"authenticated"),
        (b"x-jarvis-edge-attestation", SECRET.encode()),
        (b"x-authentik-username", b"pat"),
        (b"x-authentik-groups", b"household"),
        (b"x-authentik-name", name_bytes),
    ]
    return Headers(raw=raw)


@pytest.mark.parametrize("wire,expected", [
    (b"\xff\xfe", None),
    ("José Example".encode("utf-8"), "José"),
])
def test_raw_header_bytes_through_edge_verdict(wire, expected):
    verdict = caller_auth.edge_verdict(h.PROXY, _edge_scope_headers(wire), caller_auth.settings())
    assert verdict[0] == caller_auth.CLASS_AUTHENTICATED
    assert verdict[4] == expected


def test_no_header_gives_no_name(monkeypatch):
    body = _signed_in_chat(monkeypatch, None)
    assert "speaker_first_name" not in body["context"]


def test_name_is_read_only_for_a_household_member():
    """A signed-in non-member's verdict carries no name (the Bearer upgrade
    that may follow never adds one)."""
    from starlette.datastructures import Headers

    headers = _edge_scope_headers("Pat Example".encode("utf-8"))
    raw = [(k, v if k != b"x-authentik-groups" else b"visitors") for k, v in headers.raw]
    verdict = caller_auth.edge_verdict(h.PROXY, Headers(raw=raw), caller_auth.settings())
    assert verdict[0] == caller_auth.CLASS_NOT_HOUSEHOLD
    assert verdict[4] is None
