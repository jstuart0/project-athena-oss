"""The Home Assistant card's feed: both legs, over httpx.MockTransport.

Nothing here reaches a network. The orchestrator leg sends the service key
per request; the gateway leg sends nothing. Whatever comes back is untrusted
and must never be echoed.
"""
import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.auth.oidc import get_current_user
from app.services import ha_status
from main import app
from shared.config import get_config

ORCH = "http://orch.test:8001"
GATEWAY = "http://gw.test:8000"
NOW = datetime(2026, 10, 11, 12, 5, 0, tzinfo=timezone.utc)
GOLDEN = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ha_link" / "admin_ha_status_v1.json"
GOOD = json.loads(GOLDEN.read_text(encoding="utf-8"))
HOSTILE = "<img src=x>"


@pytest.fixture(autouse=True)
def _urls(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", ORCH)
    monkeypatch.setenv("GATEWAY_URL", GATEWAY)
    monkeypatch.setattr(ha_status, "check_ssrf_safe", AsyncMock(return_value=(True, "")))


class Legs:
    """A MockTransport that answers each leg by host and records requests."""

    def __init__(self, orch=None, gateway=None):
        self.orch = orch if orch is not None else (lambda req: httpx.Response(200, json=GOOD))
        self.gateway = gateway if gateway is not None else (lambda req: httpx.Response(200, json={"ha_registry": "ok"}))
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        leg = self.orch if request.url.host == "orch.test" else self.gateway
        result = leg(request)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def to(self, host):
        return [r for r in self.requests if r.url.host == host]


def fetch(legs):
    return asyncio.run(ha_status.fetch_ha_link(transport=legs.transport, now=NOW))


def json_response(body, status=200):
    return lambda req: httpx.Response(status, json=body)


def broken(**changes):
    body = copy.deepcopy(GOOD)
    for dotted, value in changes.items():
        node = body
        *path, last = dotted.split("__")
        for part in path:
            node = node[part]
        if value is KeyError:
            node.pop(last)
        else:
            node[last] = value
    return body


# -- the orchestrator leg ----------------------------------------------------------------

def test_a_good_answer_is_connected():
    status = fetch(Legs())
    assert (status.state, status.reason) == ("connected", "ok")
    assert status.areas == GOOD["registry"]["areas"] and status.coverage == "cluster"
    assert status.gateway == "ok" and status.gateway_sampled is True


LEG_MATRIX = [
    ("refused", lambda req: httpx.ConnectError("refused"), "orchestrator_unreachable"),
    ("read-timeout", lambda req: httpx.ReadTimeout("slow"), "orchestrator_unreachable"),
    ("401", lambda req: httpx.Response(401), "orchestrator_refused"),
    ("403", lambda req: httpx.Response(403), "orchestrator_refused"),
    ("404", lambda req: httpx.Response(404), "orchestrator_outdated"),
    ("500", lambda req: httpx.Response(500, text="boom"), "orchestrator_unreachable"),
    ("502", lambda req: httpx.Response(502), "orchestrator_unreachable"),
    ("302", lambda req: httpx.Response(302, headers={"location": "http://evil.test/"}), "orchestrator_refused"),
    ("non-json-200", lambda req: httpx.Response(200, text="<html>nope</html>"), "orchestrator_unreachable"),
    ("list", json_response([1, 2]), "orchestrator_unreachable"),
    ("null", lambda req: httpx.Response(200, content=b"null"), "orchestrator_unreachable"),
    ("unknown-enum", json_response(broken(rest__last_outcome="zzz")), "orchestrator_outdated"),
    ("one-mib-field", lambda req: httpx.Response(200, content=json.dumps(broken(coverage="x" * 1_048_576)).encode()),
     "orchestrator_unreachable"),
    ("missing-registry", json_response(broken(registry=KeyError)), "orchestrator_outdated"),
    ("missing-rest", json_response(broken(rest=KeyError)), "orchestrator_outdated"),
]


@pytest.mark.parametrize("name,leg,reason", LEG_MATRIX, ids=[m[0] for m in LEG_MATRIX])
def test_orchestrator_leg_failures_read_unknown_with_an_orchestrator_reason(name, leg, reason):
    legs = Legs(orch=leg)
    status = fetch(legs)
    assert (status.state, status.reason) == ("unknown", reason)
    assert status.gateway == "ok"  # the other leg is unaffected
    assert len(legs.to("orch.test")) == 1  # a redirect is not followed


def test_malformed_numbers_and_times_degrade_to_none_not_a_500():
    legs = Legs(orch=json_response(broken(registry__areas="7", rest__consecutive_failures=-1, replicas_reporting=-2)))
    status = fetch(legs)
    assert status.areas is None and status.replicas_reporting is None
    assert status.state in {"connected", "attention", "failing"}


def test_an_ssrf_blocked_orchestrator_makes_no_orchestrator_request(monkeypatch):
    async def blocked(url):
        return (False, "blocked") if "orch.test" in url else (True, "")

    monkeypatch.setattr(ha_status, "check_ssrf_safe", blocked)
    legs = Legs()
    status = fetch(legs)
    assert (status.state, status.reason) == ("unknown", "orchestrator_blocked")
    assert legs.to("orch.test") == []


def test_an_ssrf_blocked_gateway_is_not_checked_and_makes_no_request(monkeypatch):
    async def blocked(url):
        return (False, "blocked") if "gw.test" in url else (True, "")

    monkeypatch.setattr(ha_status, "check_ssrf_safe", blocked)
    legs = Legs()
    status = fetch(legs)
    assert status.state == "connected" and status.gateway == "not_checked"
    assert legs.to("gw.test") == []


# -- the gateway leg and leg independence ---------------------------------------------

@pytest.mark.parametrize("leg,expected", [
    (json_response({"ha_registry": "refused"}), "refused"),
    (json_response({"ha_registry": "unavailable"}), "unavailable"),
    (json_response({"ha_registry": "disabled"}), "disabled"),
    (json_response({"ha_registry": "zzz"}), "unknown"),
    (json_response({"ha_registry": HOSTILE}), "unknown"),
    (json_response({"status": "healthy"}), "not_checked"),
    (lambda req: httpx.Response(200, text="not json"), "not_checked"),
    (lambda req: httpx.Response(500), "not_checked"),
    (lambda req: httpx.ConnectError("down"), "not_checked"),
    (lambda req: httpx.Response(200, content=b"[1]"), "not_checked"),
])
def test_the_gateway_leg_is_a_closed_word_and_never_changes_the_state(leg, expected):
    status = fetch(Legs(gateway=leg))
    assert status.gateway == expected
    assert (status.state, status.reason) == ("connected", "ok")


def test_both_legs_failing_is_unknown_with_the_gateway_not_checked():
    status = fetch(Legs(orch=lambda req: httpx.ConnectError("x"), gateway=lambda req: httpx.ConnectError("y")))
    assert (status.state, status.reason, status.gateway) == ("unknown", "orchestrator_unreachable", "not_checked")


def test_the_orchestrator_failing_alone_leaves_the_gateway_note():
    status = fetch(Legs(orch=lambda req: httpx.Response(401), gateway=json_response({"ha_registry": "refused"})))
    assert (status.reason, status.gateway) == ("orchestrator_refused", "refused")


# -- requests: key per request, caps, concurrency ---------------------------------------

def test_the_service_key_is_sent_to_the_orchestrator_only():
    legs = Legs()
    fetch(legs)
    (orch,) = legs.to("orch.test")
    (gateway,) = legs.to("gw.test")
    assert orch.headers["X-Service-Key"] == get_config().service_api_key
    assert orch.url.path == "/admin/ha-status" and orch.method == "GET"
    assert "x-service-key" not in gateway.headers and "authorization" not in gateway.headers
    assert gateway.url.path == "/health"


def test_the_clients_do_not_follow_redirects_or_turn_tls_verification_off():
    import inspect

    source = inspect.getsource(ha_status)
    assert "verify=False" not in source
    assert "follow_redirects=False" in source
    assert "X-Service-Key" in source and "default_headers" not in source


@pytest.mark.parametrize("proxy_var", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
def test_both_legs_ignore_proxy_variables_so_the_key_never_rides_a_proxy(monkeypatch, proxy_var):
    seen = []
    real = httpx.AsyncClient

    def recording(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ha_status.httpx, "AsyncClient", recording)
    monkeypatch.setenv(proxy_var, "http://proxy.invalid:3128")
    fetch(Legs())
    assert len(seen) == 2
    assert all(kwargs.get("trust_env") is False for kwargs in seen), seen


def test_the_body_cap_is_64_kib():
    assert ha_status.MAX_BODY_BYTES == 64 * 1024


def test_both_legs_run_concurrently():
    async def go():
        started = []
        both = asyncio.Event()

        async def handler(request):
            started.append(request.url.host)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)  # only released once the other leg has also started
            body = GOOD if request.url.host == "orch.test" else {"ha_registry": "ok"}
            return httpx.Response(200, json=body)

        return await ha_status.fetch_ha_link(transport=httpx.MockTransport(handler), now=NOW)

    assert asyncio.run(go()).state == "connected"


# -- nothing hostile comes back --------------------------------------------------------

def test_no_hostile_text_url_or_area_name_is_returned():
    hostile = broken(coverage=HOSTILE)
    hostile["note"] = HOSTILE
    hostile["registry"]["area_names"] = [HOSTILE, "Living Room", "http://evil.test/"]
    status = fetch(Legs(orch=json_response(hostile), gateway=json_response({"ha_registry": HOSTILE})))
    blob = status.model_dump_json()
    for forbidden in (HOSTILE, "Living Room", "://", "orch.test", "gw.test"):
        assert forbidden not in blob


def test_logs_carry_a_reason_and_a_status_code_only(capsys):
    fetch(Legs(orch=lambda req: httpx.Response(403, text=f"secret {HOSTILE}")))
    out = capsys.readouterr().out
    assert "orchestrator_refused" in out and "403" in out
    assert HOSTILE not in out and "secret" not in out and "orch.test" not in out


# -- the seam: the orchestrator's golden file ---------------------------------------------

def test_the_orchestrators_golden_file_parses_into_connected():
    status = ha_status.classify(GOOD, None, "ok", NOW)
    assert (status.state, status.reason) == ("connected", "ok")
    assert status.areas == 9 and status.coverage == "cluster" and status.replicas_reporting == 1


@pytest.mark.parametrize("removed", ["rest", "registry"])
def test_a_golden_copy_with_a_section_removed_is_outdated_not_a_500(removed):
    older = {k: v for k, v in GOOD.items() if k != removed}
    status = ha_status.classify(older, None, "ok", NOW)
    assert (status.state, status.reason) == ("unknown", "orchestrator_outdated")


# -- the route -----------------------------------------------------------------------------

@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user

    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


def test_the_route_returns_the_classified_status(owner_client, monkeypatch):
    legs = Legs()
    real = ha_status.fetch_ha_link

    async def with_mock_transport():
        return await real(transport=legs.transport, now=NOW)

    monkeypatch.setattr(ha_status, "fetch_ha_link", with_mock_transport)
    response = owner_client.get("/api/service-registry/home-assistant")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "state", "reason", "areas", "last_outcome_at", "last_success_at", "coverage",
        "replicas_reporting", "checked_at", "gateway", "gateway_sampled",
    }
    assert (body["state"], body["reason"]) == ("connected", "ok")


def test_the_route_answers_with_an_orchestrator_reason_when_nothing_is_reachable(owner_client, monkeypatch):
    legs = Legs(orch=lambda req: httpx.ConnectError("down"), gateway=lambda req: httpx.ConnectError("down"))
    real = ha_status.fetch_ha_link

    async def with_mock_transport():
        return await real(transport=legs.transport, now=NOW)

    monkeypatch.setattr(ha_status, "fetch_ha_link", with_mock_transport)
    response = owner_client.get("/api/service-registry/home-assistant")
    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["reason"], body["gateway"]) == ("unknown", "orchestrator_unreachable", "not_checked")
