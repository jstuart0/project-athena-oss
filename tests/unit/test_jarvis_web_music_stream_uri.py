"""jarvis-web's music-stream proxy takes only a Music Assistant item URI
(xander L5).

The URI is forwarded into a path on the gateway and on to Music Assistant's
/stream/, so a dot segment, query, fragment, backslash or percent-escape
could walk out of /stream/ or smuggle parameters. Refused with 400 before
any outbound call; a real item URI (named: spotify://track/…) is proxied.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

from . import _jarvis_web_harness as h


@pytest.fixture
def out(monkeypatch):
    h.configure()
    yield h.install_outbound(monkeypatch)
    h.configure()


def _get(uri):
    return h.client().get(f"/api/music/stream/{quote(uri, safe='')}", headers=h.via_proxy(h.LAN))


@pytest.mark.parametrize("uri", [
    "spotify://track/../../admin",
    "library://track/./x",
    "spotify://track/x?token=1",
    "spotify://track/x#frag",
    "spotify://track/..\\admin",
    "spotify://track/%2e%2e/admin",
    "not-a-uri",
    "spotify:/track/x",
])
def test_bad_uris_are_400_before_any_outbound_call(out, uri):
    resp = _get(uri)
    assert resp.status_code == 400 and resp.json() == {"detail": "invalid_music_uri"}, uri
    assert [c for c in out.calls if "/api/music/stream" in c[1]] == []


@pytest.mark.parametrize("uri", ["..", "spotify://track/x\nHost: evil"])
def test_uris_the_router_already_refuses_never_go_out(out, uri):
    """A bare ".." is folded by the client and a newline never matches the
    route: not 400, but never proxied either."""
    assert _get(uri).status_code in {400, 404}
    assert [c for c in out.calls if "/api/music/stream" in c[1]] == []


@pytest.mark.parametrize("uri", [
    "spotify://track/4uLU6hMCjMI75M1A2tKUQC",
    "library://track/123",
    "filesystem_local--a1b2://track/Music/Some Artist/Song.flac",
])
def test_item_uris_are_proxied(out, uri):
    resp = _get(uri)
    assert resp.status_code == 200, uri
    streamed = [c[1] for c in out.calls if c[0] == "STREAM"]
    assert streamed and streamed[-1].endswith(f"/api/music/stream/{uri}")
