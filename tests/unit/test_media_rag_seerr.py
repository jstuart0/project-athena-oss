"""Media RAG: where the Overseerr address and key come from, and what a
caller hears when Overseerr can't be used.

The admin panel's ``overseerr`` External API Key (endpoint URL and key) wins;
``OVERSEERR_URL``/``OVERSEERR_API_KEY`` are the fallback. A failure is said as
a failure, never as "I couldn't find that title".
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
ADMIN = "http://admin.test"
ADMIN_KEY_ROUTE = f"{ADMIN}/api/external-api-keys/public/overseerr/key"

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()


def _load():
    name = "_test_media_rag_seerr"
    if name in sys.modules:
        return sys.modules[name]
    with mock.patch.dict("os.environ", {"ADMIN_API_URL": ADMIN}):
        spec = importlib.util.spec_from_file_location(name, _SRC / "rag" / "media" / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


media = _load()


class FakeNet:
    """Answers admin-backend and Overseerr; records every Overseerr request."""

    def __init__(self, admin=None, search_results=None, seerr_down=False):
        self.admin = admin  # dict body, an int status, or "down"
        self.search_results = search_results or []
        self.seerr_down = seerr_down
        self.seerr_calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(ADMIN):
            if self.admin == "down":
                raise httpx.ConnectError("admin down")
            if isinstance(self.admin, int):
                return httpx.Response(self.admin)
            return httpx.Response(200, json=self.admin)
        self.seerr_calls.append(request)
        if self.seerr_down:
            raise httpx.ConnectError("seerr down")
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(200, json={"results": self.search_results})
        if path.endswith("/request") and request.method == "POST":
            return httpx.Response(201, json={"id": 7, "status": 2})
        if path.endswith("/status"):
            return httpx.Response(200, json={"version": "test"})
        return httpx.Response(404)


@pytest.fixture
def net(monkeypatch):
    def install(**kwargs):
        fake = FakeNet(**kwargs)
        monkeypatch.setattr(media, "http_client", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
        monkeypatch.setattr(media, "_admin_settings", {})
        monkeypatch.setattr(media, "_admin_settings_read_at", None)
        monkeypatch.setattr(media, "ENV_OVERSEERR_URL", "")
        monkeypatch.setattr(media, "ENV_OVERSEERR_API_KEY", "")
        return fake

    return install


def _query(text: str) -> dict:
    return asyncio.run(media.query(q=text, request=None))


MOVIE = {"id": 27205, "mediaType": "movie", "title": "Inception", "releaseDate": "2010-07-15"}
SHOW = {"id": 1396, "mediaType": "tv", "name": "Breaking Bad", "firstAirDate": "2008-01-20"}
PERSON = {"id": 31, "mediaType": "person", "name": "Tom Hanks"}


@pytest.mark.parametrize(
    "endpoint, base",
    [
        ("http://seerr.example:5055/api/v1", "http://seerr.example:5055"),
        ("http://seerr.example:5055/api/v1/", "http://seerr.example:5055"),
        ("http://seerr.example:5055", "http://seerr.example:5055"),
        ("https://requests.example/", "https://requests.example"),
        ("", ""),
    ],
)
def test_base_url_from_endpoint(endpoint, base):
    assert media.base_url_from_endpoint(endpoint) == base


def test_admin_endpoint_and_key_win_over_environment(net, monkeypatch):
    fake = net(admin={"api_key": "admin-key", "endpoint_url": "http://seerr.example:5055/api/v1"},
               search_results=[MOVIE])
    monkeypatch.setattr(media, "ENV_OVERSEERR_URL", "http://env.example:5055")
    monkeypatch.setattr(media, "ENV_OVERSEERR_API_KEY", "env-key")

    _query("is inception available")

    call = fake.seerr_calls[0]
    assert str(call.url).startswith("http://seerr.example:5055/api/v1/search?query=inception")
    assert call.headers["X-Api-Key"] == "admin-key"


def test_environment_is_the_fallback_without_an_admin_key(net, monkeypatch):
    fake = net(admin=404, search_results=[MOVIE])
    monkeypatch.setattr(media, "ENV_OVERSEERR_URL", "http://env.example:5055")
    monkeypatch.setattr(media, "ENV_OVERSEERR_API_KEY", "env-key")

    _query("is inception available")

    assert str(fake.seerr_calls[0].url).startswith("http://env.example:5055/api/v1/search")
    assert fake.seerr_calls[0].headers["X-Api-Key"] == "env-key"


def test_nothing_configured_says_so_and_calls_nothing(net):
    fake = net(admin=404)

    result = _query("add the movie inception")

    assert result == {"success": False, "answer": media.NOT_CONFIGURED_ANSWER}
    assert fake.seerr_calls == []


@pytest.mark.parametrize(
    "text",
    ["add the movie inception", "is inception available", "what have i requested", "inception"],
)
def test_unreachable_seerr_is_reported_not_called_a_missing_title(net, text):
    net(admin={"api_key": "k", "endpoint_url": "http://seerr.example:5055/api/v1"}, seerr_down=True)

    result = _query(text)

    assert result == {"success": False, "answer": media.UNAVAILABLE_ANSWER}


def test_tv_request_asks_for_every_season(net):
    fake = net(admin={"api_key": "k", "endpoint_url": "http://seerr.example:5055/api/v1"}, search_results=[SHOW])

    result = _query("add the show breaking bad")

    post = [c for c in fake.seerr_calls if c.method == "POST"][0]
    assert json.loads(post.content) == {"mediaId": 1396, "mediaType": "tv", "seasons": "all"}
    assert result["success"] is True
    assert "Breaking Bad" in result["answer"]


def test_movie_request_sends_no_seasons(net):
    fake = net(admin={"api_key": "k", "endpoint_url": "http://seerr.example:5055/api/v1"}, search_results=[MOVIE])

    _query("add the movie inception")

    post = [c for c in fake.seerr_calls if c.method == "POST"][0]
    assert json.loads(post.content) == {"mediaId": 27205, "mediaType": "movie"}


def test_people_in_search_results_are_skipped(net):
    fake = net(admin={"api_key": "k", "endpoint_url": "http://seerr.example:5055/api/v1"},
               search_results=[PERSON, MOVIE])

    result = _query("request inception")

    post = [c for c in fake.seerr_calls if c.method == "POST"][0]
    assert json.loads(post.content)["mediaId"] == 27205
    assert "Inception" in result["answer"]


def test_last_admin_settings_are_kept_while_admin_is_down(net, monkeypatch):
    fake = net(admin={"api_key": "admin-key", "endpoint_url": "http://seerr.example:5055/api/v1"},
               search_results=[MOVIE])
    _query("is inception available")

    fake.admin = "down"
    monkeypatch.setattr(media, "_admin_settings_read_at", -10_000.0)  # stale: forces a re-read
    _query("is inception available")

    assert len(fake.seerr_calls) == 2
    assert str(fake.seerr_calls[1].url).startswith("http://seerr.example:5055/")


def test_a_changed_admin_key_is_picked_up_after_the_ttl(net, monkeypatch):
    fake = net(admin={"api_key": "old", "endpoint_url": "http://old.example/api/v1"}, search_results=[MOVIE])
    clock = [1000.0]
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    _query("is inception available")

    fake.admin = {"api_key": "new", "endpoint_url": "http://new.example/api/v1"}
    clock[0] += media.SETTINGS_TTL_SECONDS - 1
    _query("is inception available")
    clock[0] += 2
    _query("is inception available")

    hosts = [c.url.host for c in fake.seerr_calls]
    assert hosts == ["old.example", "old.example", "new.example"]
    assert fake.seerr_calls[2].headers["X-Api-Key"] == "new"


def test_health_reports_not_configured(net):
    net(admin=404)
    body = asyncio.run(media.health())
    assert body["status"] == "degraded"
    assert body["overseerr_configured"] is False
    assert body["overseerr_connected"] is False


def test_health_reports_connected(net):
    net(admin={"api_key": "k", "endpoint_url": "http://seerr.example:5055/api/v1"})
    body = asyncio.run(media.health())
    assert body["status"] == "healthy"
    assert body["overseerr_connected"] is True


@pytest.mark.parametrize(
    "text, title",
    [
        ("Add the movie Inception", "inception"),
        ("Request Dune Part 2", "dune part 2"),
        ("Is Breaking Bad available?", "breaking bad"),
        ("do we have the matrix on jellyfin", "the matrix"),
        ("Isle of Dogs", "isle of dogs"),
    ],
)
def test_extract_title(text, title):
    assert media.extract_title(text) == title
