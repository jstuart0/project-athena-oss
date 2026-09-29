"""chat-embed <-> jarvis-web, end to end (V8.1, D16, G7).

chat-embed's outbound httpx runs through httpx.ASGITransport into
jarvis-web's real app, with the same JARVIS_RELAY_KEY on both sides; only
jarvis-web's own upstream (the orchestrator) is faked.
"""
from __future__ import annotations

import importlib.util
import json
import sys

import httpx
import httpx._client
import pytest
from fastapi.testclient import TestClient

from . import _jarvis_web_harness as h

CHAT_EMBED = h.REPO_ROOT / "apps" / "chat-embed"
RELAY_KEY = "relay-key-for-tests-0123456789abcdef"
VISITOR = "203.0.113.77"


def _load_chat_embed(monkeypatch, **env):
    base = {"ATHENA_CHAT_URL": "http://jarvis-web/api/chat", "STREAM_URL": "http://jarvis-web/api/chat/stream",
            "JARVIS_RELAY_KEY": RELAY_KEY, "CORS_ORIGINS": "https://site.example"}
    base.update(env)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    for key in ("TRUSTED_PROXY_CIDRS", "RATE_LIMIT_RPM", "TRUST_CF_CONNECTING_IP"):
        if key not in base:
            monkeypatch.delenv(key, raising=False)
    if str(CHAT_EMBED) not in sys.path:
        sys.path.insert(0, str(CHAT_EMBED))
    spec = importlib.util.spec_from_file_location("_chat_embed_contract_main", CHAT_EMBED / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_chat_embed_contract_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def jarvis(monkeypatch):
    h.configure({**h.HOME_ENV, "JARVIS_RELAY_KEY": RELAY_KEY})
    out = h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "Alice Renter", "id": 7})
    yield out
    h.configure()


def _wire(monkeypatch, embed, capture=None):
    """chat-embed's client -> jarvis-web's ASGI app (a real httpx client,
    since the jarvis harness fakes httpx.AsyncClient for jarvis-web's own
    upstream calls)."""
    def _client(**kwargs):
        kwargs.pop("timeout", None)
        transport = httpx.ASGITransport(app=h.main.app)
        client = httpx._client.AsyncClient(transport=transport, base_url="http://jarvis-web", **kwargs)
        if capture is not None:
            original = client.send

            async def _send(request, *a, **kw):
                capture.append(request)
                return await original(request, *a, **kw)

            client.send = _send
        return client

    if hasattr(embed, "_http_client"):
        monkeypatch.setattr(embed, "_http_client", _client)
    else:
        # an unrepaired tree has no seam: swap the module's own httpx binding
        # (only chat-embed's), so its behaviour is still what gets judged
        from types import SimpleNamespace

        monkeypatch.setattr(embed, "httpx", SimpleNamespace(
            AsyncClient=_client, TimeoutException=httpx.TimeoutException, HTTPError=httpx.HTTPError,
        ))


def _events(text):
    return [json.loads(line[6:]) for line in text.split("\n\n") if line.startswith("data: ")]


def _embed_client(embed, peer="198.51.100.200"):
    return TestClient(embed.app, client=(peer, 40000))


def test_stream_relayed_end_to_end(monkeypatch, jarvis):
    embed = _load_chat_embed(monkeypatch)
    _wire(monkeypatch, embed)
    resp = _embed_client(embed).post("/api/chat/stream", json={"message": "what's the weather"})
    events = _events(resp.text)
    assert any(e["type"] == "token" for e in events)
    assert [e["type"] for e in events].count("done") == 1 and events[-1]["type"] == "done"
    assert events[-1]["session_id"].startswith("pub-")
    body = jarvis.orchestrator_bodies()[-1]
    assert body["caller_trust"] == "web_public" and "guest_name" not in body["context"]


def test_nonstream_relayed_and_session_continues(monkeypatch, jarvis):
    """Public multi-turn continuity: the second turn keeps the pub- id."""
    embed = _load_chat_embed(monkeypatch)
    _wire(monkeypatch, embed)
    c = _embed_client(embed)
    first = c.post("/api/chat", json={"message": "hi"})
    assert first.status_code == 200
    sid = first.json()["session_id"]
    assert sid.startswith("pub-")
    second = c.post("/api/chat", json={"message": "and tomorrow?", "session_id": sid})
    assert second.json()["session_id"] == sid
    assert jarvis.orchestrator_bodies()[-1]["session_id"] == sid


def test_session_id_of_another_visitor_is_not_forwarded(monkeypatch, jarvis):
    """Named (codex High): chat-embed forwards a session id only if it was
    minted for this visitor; another visitor's id never leaves chat-embed
    and never comes back as this visitor's session."""
    embed = _load_chat_embed(monkeypatch, TRUSTED_PROXY_CIDRS="198.51.100.0/24")
    sent = []
    _wire(monkeypatch, embed, capture=sent)
    c = _embed_client(embed)
    first = c.post("/api/chat", json={"message": "hi"}, headers={"X-Forwarded-For": VISITOR}).json()["session_id"]
    mine = c.post("/api/chat", json={"message": "again", "session_id": first}, headers={"X-Forwarded-For": VISITOR})
    assert json.loads(sent[-1].content)["session_id"] == first and mine.json()["session_id"] == first
    theirs = c.post("/api/chat", json={"message": "hi", "session_id": first}, headers={"X-Forwarded-For": "203.0.113.78"})
    assert "session_id" not in json.loads(sent[-1].content)
    assert theirs.json()["session_id"] != first
    events = _events(c.post("/api/chat/stream", json={"message": "hi", "session_id": first},
                            headers={"X-Forwarded-For": "203.0.113.78"}).text)
    assert "session_id" not in json.loads(sent[-1].content)
    assert events[-1]["type"] == "done" and events[-1]["session_id"] != first


def test_21_relayed_requests_end_in_rate_limited_event(monkeypatch, jarvis):
    """Named: jarvis-web's per-visitor relay limit (20) is reached through
    chat-embed; the stream ends with a terminal rate_limited event."""
    embed = _load_chat_embed(monkeypatch, RATE_LIMIT_RPM="100")
    _wire(monkeypatch, embed)
    c = _embed_client(embed)
    for _ in range(20):
        assert _events(c.post("/api/chat/stream", json={"message": "hi"}).text)[-1]["type"] == "done"
    events = _events(c.post("/api/chat/stream", json={"message": "hi"}).text)
    assert events == [{"type": "error", "reason": "rate_limited"}]


def test_mismatched_keys_end_in_error_event_and_log(monkeypatch, jarvis, caplog):
    embed = _load_chat_embed(monkeypatch, JARVIS_RELAY_KEY="a-different-relay-key-0123456789abcdef")
    _wire(monkeypatch, embed)
    events = _events(_embed_client(embed).post("/api/chat/stream", json={"message": "hi"}).text)
    assert events == [{"type": "error"}]
    assert "jarvis_relay_rejected" in caplog.text and "JARVIS_RELAY_KEY" in caplog.text
    assert jarvis.orchestrator_bodies() == []


def test_upstream_429_maps_to_429(monkeypatch, jarvis):
    embed = _load_chat_embed(monkeypatch, RATE_LIMIT_RPM="100")
    _wire(monkeypatch, embed)
    c = _embed_client(embed)
    for _ in range(20):
        c.post("/api/chat", json={"message": "hi"})
    resp = c.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 429 and resp.headers["retry-after"] == "60"


def test_own_limit_is_429_before_upstream(monkeypatch, jarvis):
    embed = _load_chat_embed(monkeypatch, RATE_LIMIT_RPM="2")
    _wire(monkeypatch, embed)
    c = _embed_client(embed)
    assert [c.post("/api/chat", json={"message": "hi"}).status_code for _ in range(3)] == [200, 200, 429]
    assert len(jarvis.orchestrator_bodies()) == 2


def test_relay_headers_sent_authorization_never(monkeypatch, jarvis):
    embed = _load_chat_embed(monkeypatch, TRUSTED_PROXY_CIDRS="198.51.100.0/24")
    sent = []
    _wire(monkeypatch, embed, capture=sent)
    _embed_client(embed).post(
        "/api/chat", json={"message": "hi"},
        headers={"Authorization": "Bearer owner-token", "Cookie": "jarvis_chat_key=x", "X-Forwarded-For": VISITOR},
    )
    request = sent[-1]
    assert request.headers["x-jarvis-relay-key"] == RELAY_KEY
    assert request.headers["x-jarvis-relay-client"] == VISITOR
    assert "authorization" not in request.headers and "cookie" not in request.headers


def test_stream_body_accepted_by_jarvis_chat_message(monkeypatch, jarvis):
    embed = _load_chat_embed(monkeypatch)
    sent = []
    _wire(monkeypatch, embed, capture=sent)
    c = _embed_client(embed)
    c.post("/api/chat/stream", json={"message": "hi"})
    c.post("/api/chat", json={"message": "hi"})
    fields = set(h.main.ChatMessage.model_fields)
    for request in sent:
        body = json.loads(request.content)
        h.main.ChatMessage.model_validate(body)
        assert set(body) <= fields
        assert not ({"mode", "caller_trust", "query"} & set(body))


def test_unresolvable_visitor_is_503_locally(monkeypatch, jarvis):
    embed = _load_chat_embed(monkeypatch)
    _wire(monkeypatch, embed)
    resp = TestClient(embed.app, client=("testclient", 1)).post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 503
    assert jarvis.orchestrator_bodies() == []


def test_cors_origins_empty_warns(monkeypatch, caplog):
    embed = _load_chat_embed(monkeypatch, CORS_ORIGINS="")
    assert embed.CORS_ORIGINS == []
    assert "embed disabled for browsers until CORS_ORIGINS is set" in caplog.text
    resp = TestClient(embed.app).options(
        "/api/chat", headers={"Origin": "https://site.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.parametrize("value", ["*", "null", "*,https://site.example"])
def test_cors_star_refused(monkeypatch, caplog, value):
    embed = _load_chat_embed(monkeypatch, CORS_ORIGINS=value)
    assert "*" not in embed.CORS_ORIGINS and "null" not in embed.CORS_ORIGINS
    assert "cors_origin_refused" in caplog.text


def test_listed_origin_without_credentials(monkeypatch):
    embed = _load_chat_embed(monkeypatch)
    resp = TestClient(embed.app).options(
        "/api/chat", headers={"Origin": "https://site.example", "Access-Control-Request-Method": "POST"},
    )
    assert resp.headers["access-control-allow-origin"] == "https://site.example"
    assert "access-control-allow-credentials" not in resp.headers


def test_chat_embed_dockerfile_builds_from_repo_root():
    lines = (CHAT_EMBED / "Dockerfile").read_text(encoding="utf-8").splitlines()
    assert "COPY src/shared/client_throttle.py ./client_throttle.py" in lines
    assert "COPY apps/chat-embed/main.py ." in lines
    defs = (h.REPO_ROOT / "scripts" / "service-defs.sh").read_text(encoding="utf-8")
    assert '"athena-chat-embed|apps/chat-embed/Dockerfile|.|0"' in defs


@pytest.mark.parametrize("url, off_cluster", [
    ("http://192.0.2.40:3001/api/chat", True),
    ("http://jarvis.example.com/api/chat", True),
    ("http://[2001:db8::5]/api/chat", True),
    ("https://jarvis.example.com/api/chat", False),
    ("http://athena-jarvis-web.athena-prod.svc/api/chat", False),
    ("http://athena-jarvis-web.athena-prod.svc.cluster.local/api/chat", False),
    ("http://athena-jarvis-web/api/chat", False),
    ("http://127.0.0.1:3001/api/chat", False),
    ("http://[::1]:3001/api/chat", False),
    ("http://localhost:3001/api/chat", False),
])
def test_plaintext_off_cluster_upstream_is_an_error(monkeypatch, caplog, url, off_cluster):
    """xander L4 (named: a plain-http LAN address): the relay key would
    cross the network in cleartext, so startup logs an ERROR naming the
    setting (never the key)."""
    _load_chat_embed(monkeypatch, ATHENA_CHAT_URL=url, STREAM_URL=url)
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "upstream_plaintext_off_cluster" in r.getMessage()]
    assert bool(errors) is off_cluster
    if off_cluster:
        assert {"ATHENA_CHAT_URL", "STREAM_URL"} <= {n for r in errors for n in ("ATHENA_CHAT_URL", "STREAM_URL") if n in r.getMessage()}
        assert RELAY_KEY not in caplog.text


class _Upstream:
    """A jarvis-web stand-in for the terminal-event rows: a 200 stream with
    the given SSE chunks, or a connect failure."""

    def __init__(self, chunks=None, connect_error=False):
        self.chunks = chunks or []
        self.connect_error = connect_error

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        outer = self

        class _Resp:
            status_code = 200
            headers = {}

            async def __aenter__(self_inner):
                if outer.connect_error:
                    raise httpx.ConnectError("connection refused")
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def aiter_text(self_inner):
                for chunk in outer.chunks:
                    yield chunk

        return _Resp()


@pytest.mark.parametrize("upstream", [
    _Upstream(chunks=['data: {"stage": "session", "session_id": "pub-x"}\n\n',
                      'data: {"stage": "answer_chunk", "content": "partial"}\n\n']),
    _Upstream(connect_error=True),
], ids=["ends_without_complete", "connect_error"])
def test_stream_always_ends_with_exactly_one_error(monkeypatch, upstream):
    """tessa C3 (named: ends_without_complete): a 200 stream that stops
    before `complete`, and an upstream that can't be reached, each end the
    visitor's stream with exactly one terminal error event and no done."""
    embed = _load_chat_embed(monkeypatch)
    monkeypatch.setattr(embed, "_http_client", upstream)
    events = _events(_embed_client(embed).post("/api/chat/stream", json={"message": "hi"}).text)
    terminal = [e for e in events if e["type"] in {"done", "error"}]
    assert terminal == [{"type": "error"}]
    assert events[-1] == {"type": "error"}
