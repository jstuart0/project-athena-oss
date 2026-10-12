"""The /api/status fallback probe: registry vocabulary only, no ``(auth required)``."""
import asyncio
import pathlib

import httpx
import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def probe(handler):
    from app.services import core_probe

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await core_probe.probe_health(client, "gateway", "http://gw.example.invalid")

    return asyncio.run(go())


@pytest.mark.parametrize("status,expected", [
    (200, (True, "healthy", None)),
    (401, (False, "error: HTTP 401", "Unexpected status code: 401")),
    (403, (False, "error: HTTP 403", "Unexpected status code: 403")),
    (500, (False, "error: HTTP 500", "Unexpected status code: 500")),
])
def test_status_codes(status, expected):
    assert probe(lambda request: httpx.Response(status)) == expected


def test_a_connection_failure_is_unreachable():
    def refuse(request):
        raise httpx.ConnectError("secret detail", request=request)

    assert probe(refuse) == (False, "unreachable", "gateway not accessible")


def test_a_timeout_is_a_timeout():
    def slow(request):
        raise httpx.ReadTimeout("secret detail", request=request)

    assert probe(slow) == (False, "timeout", "Service did not respond within timeout")


def test_the_status_route_helper_uses_the_probe(monkeypatch):
    import main

    async def go():
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        async with httpx.AsyncClient(transport=transport) as client:
            return await main._check_http_service_status(client, "orchestrator", "http://o.example.invalid")

    status = asyncio.run(go())
    assert (status.name, status.healthy, status.status) == ("orchestrator", False, "error: HTTP 401")


def test_the_literal_is_gone_from_the_backend():
    hits = []
    for path in [BACKEND / "main.py"] + sorted((BACKEND / "app").rglob("*.py")):
        if "healthy (auth required)" in path.read_text():
            hits.append(path.name)
    assert hits == []
