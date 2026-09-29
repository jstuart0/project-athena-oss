"""The OSS edge template's contract (V4.8, D1, A1, A7).

Parsed, not grepped: routes, priorities, Middleware order, the strip list
against caller_auth.EDGE_STRIPPED_HEADERS, forwardAuth hardening, and the
Deployment snippet's Secret references.
"""
from __future__ import annotations

import re

import pytest
import yaml

from . import _jarvis_web_harness as h

TEMPLATE = h.REPO_ROOT / "manifests" / "athena-prod" / "optional" / "jarvis-web-edge-auth.yaml"
STANDALONE = h.REPO_ROOT / "apps" / "jarvis-web" / "k8s" / "deployment.yaml"
PRIORITIES = {"O": 3000, "S": 2000, "G": 910, "H": 900, "A": 100}


def _docs(path=TEMPLATE):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _middlewares(path=TEMPLATE):
    return {d["metadata"]["name"]: d["spec"] for d in _docs(path) if d["kind"] == "Middleware"}


def _routes(path=TEMPLATE):
    ingress = next(d for d in _docs(path) if d["kind"] == "IngressRoute" and d["spec"]["entryPoints"] == ["websecure"])
    return {r["priority"]: r for r in ingress["spec"]["routes"]}


def _route(letter):
    return _routes()[PRIORITIES[letter]]


def _names(route):
    return [m["name"] for m in route.get("middlewares", [])]


def test_every_route_has_explicit_priority_in_d1_order():
    routes = _routes()
    assert sorted(routes, reverse=True) == [3000, 2000, 910, 900, 100]
    ingress = next(d for d in _docs() if d["kind"] == "IngressRoute")
    assert all("priority" in r for r in ingress["spec"]["routes"])


def test_strip_list_covers_everything_jarvis_web_reads():
    """Floor 8 names; named member X-Jarvis-Edge-Attestation."""
    stripped = _middlewares()["jarvis-edge-strip"]["headers"]["customRequestHeaders"]
    assert all(v == "" for v in stripped.values())
    required = set(h.caller_auth.EDGE_STRIPPED_HEADERS)
    assert len(required) >= 8
    assert "X-Jarvis-Edge-Attestation" in required
    assert required <= set(stripped)


def test_forward_auth_hardening():
    forward = _middlewares()["jarvis-edge-forwardauth"]["forwardAuth"]
    assert forward["trustForwardHeader"] is False
    assert forward["authResponseHeadersRegex"] == "(?i)^x-authentik-"
    pattern = re.compile(forward["authResponseHeadersRegex"])
    assert pattern.search("X-authentik-groups") and pattern.search("X-Authentik-Username")
    assert "authResponseHeaders" not in forward


@pytest.mark.parametrize("letter", ["A", "S"])
def test_signed_in_routes_strip_then_forwardauth_then_attest(letter):
    assert _names(_route(letter)) == ["jarvis-edge-strip", "jarvis-edge-forwardauth", "jarvis-edge-attest-authenticated"]


@pytest.mark.parametrize("letter, attest, value", [("H", "jarvis-edge-attest-home", "home"),
                                                     ("G", "jarvis-edge-attest-guest", "guest")])
def test_network_routes_strip_then_attest(letter, attest, value):
    assert _names(_route(letter)) == ["jarvis-edge-strip", attest]
    headers = _middlewares()[attest]["headers"]["customRequestHeaders"]
    assert headers["X-Jarvis-Edge-Class"] == value


def test_outpost_route_carries_no_middlewares():
    """A7: the attestation never reaches the outpost."""
    outpost = _route("O")
    assert "middlewares" not in outpost
    assert "PathPrefix(`/outpost.goauthentik.io/`)" in outpost["match"]


def test_attestation_is_a_placeholder_everywhere():
    for name, spec in _middlewares().items():
        if name.startswith("jarvis-edge-attest-"):
            assert spec["headers"]["customRequestHeaders"]["X-Jarvis-Edge-Attestation"] == "CONFIGURE_ME_EDGE_ATTESTATION"


@pytest.mark.parametrize("letter", ["H", "G"])
def test_network_routes_negate_each_cf_header_separately(letter):
    match = _route(letter)["match"]
    assert "!HeaderRegexp(`Cf-Connecting-Ip`, `.+`)" in match
    assert "!HeaderRegexp(`Cf-Ray`, `.+`)" in match
    assert "||HeaderRegexp" not in match.replace(" ", "")


def test_guest_route_outranks_home():
    assert PRIORITIES["G"] > PRIORITIES["H"]
    assert "ClientIP(`198.51.100.0/24`)" in _route("G")["match"]


def _snippet():
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text.split("# DEPLOYMENT-SNIPPET-BEGIN", 1)[1].split("# DEPLOYMENT-SNIPPET-END", 1)[0]
    lines = [line[1:] if line.startswith("#") else line for line in block.splitlines()]
    return yaml.safe_load("\n".join(lines))


def test_deployment_snippet_secret_refs():
    env = {e["name"]: e for e in _snippet()}
    current = env["JARVIS_EDGE_ATTESTATION_SECRET"]["valueFrom"]["secretKeyRef"]
    assert current["name"] == "jarvis-edge-attestation" and current["key"] == "current"
    assert current.get("optional") is not True
    previous = env["JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS"]["valueFrom"]["secretKeyRef"]
    assert previous["key"] == "previous" and previous["optional"] is True
    assert {"JARVIS_LOCAL_NETWORKS", "JARVIS_GUEST_NETWORKS", "JARVIS_ALLOWED_HOSTS", "JARVIS_HOUSEHOLD_GROUPS"} <= set(env)


def test_snippet_home_set_matches_route_h():
    env = {e["name"]: e.get("value") for e in _snippet()}
    local = env["JARVIS_LOCAL_NETWORKS"].split(",")
    assert all(f"ClientIP(`{cidr}`)" in _route("H")["match"] for cidr in local)
    for cidr in env["JARVIS_LOCAL_EXCLUDE"].split(","):
        assert f"!ClientIP(`{cidr}`)" in _route("H")["match"]


def test_standalone_example_has_the_same_shape():
    middlewares = _middlewares(STANDALONE)
    stripped = middlewares["jarvis-edge-strip"]["headers"]["customRequestHeaders"]
    assert set(h.caller_auth.EDGE_STRIPPED_HEADERS) <= set(stripped)
    forward = middlewares["jarvis-edge-forwardauth"]["forwardAuth"]
    assert forward["trustForwardHeader"] is False and forward["authResponseHeadersRegex"] == "(?i)^x-authentik-"
    routes = _routes(STANDALONE)
    assert _names(routes[100]) == ["jarvis-edge-strip", "jarvis-edge-forwardauth", "jarvis-edge-attest-authenticated"]
    assert _names(routes[900]) == ["jarvis-edge-strip", "jarvis-edge-attest-home"]
    assert "middlewares" not in routes[3000]
