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
# Every header Authentik's proxy outpost sets on an authenticated request:
# goauthentik/authentik, tag version/2025.8.1,
# internal/outpost/proxyv2/application/mode_common.go lines 42-48 and 51-55.
AUTHENTIK_HEADERS = {
    "X-authentik-username", "X-authentik-groups", "X-authentik-entitlements", "X-authentik-email",
    "X-authentik-name", "X-authentik-uid", "X-authentik-jwt", "X-authentik-meta-jwks",
    "X-authentik-meta-outpost", "X-authentik-meta-provider", "X-authentik-meta-app", "X-authentik-meta-version",
}


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


def _effective_strip_set():
    """What jarvis-web says the edge must strip, as configured by default
    (xander M1: the list plus the identity and groups header names)."""
    env = {"SERVICE_API_KEY": h.SERVICE_KEY, "JARVIS_EDGE_ATTESTATION_SECRET": "edge-attestation-current-7f3a9c2e1b8d4f6a",
           "TRUSTED_PROXY_CIDRS": "10.0.0.0/8", "JARVIS_LOCAL_NETWORKS": "192.0.2.0/24",
           "JARVIS_ALLOWED_HOSTS": "jarvis.example.com", "JARVIS_HOUSEHOLD_GROUPS": "household"}
    return {n.lower() for n in h.caller_auth.load_settings(env, own_ips=()).edge_strip_headers}


@pytest.mark.parametrize("path", [TEMPLATE, STANDALONE], ids=["template", "standalone"])
def test_strip_list_covers_everything_jarvis_web_reads(path):
    """Floor 8 names; named member X-Jarvis-Edge-Attestation. Checked
    against jarvis-web's effective strip set and every header Authentik's
    outpost can return (otto: the live set is broader than the names
    jarvis-web reads)."""
    stripped = _middlewares(path)["jarvis-edge-strip"]["headers"]["customRequestHeaders"]
    assert all(v == "" for v in stripped.values())
    required = _effective_strip_set()
    assert len(required) >= 8
    assert "x-jarvis-edge-attestation" in required
    names = {n.lower() for n in stripped}
    assert required <= names
    assert {n.lower() for n in AUTHENTIK_HEADERS} <= names


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
    """codex M (D1): the standalone example carries routes O, S, G, H and A
    with the template's priorities, Middleware order and attest classes."""
    middlewares = _middlewares(STANDALONE)
    forward = middlewares["jarvis-edge-forwardauth"]["forwardAuth"]
    assert forward["trustForwardHeader"] is False and forward["authResponseHeadersRegex"] == "(?i)^x-authentik-"
    routes = _routes(STANDALONE)
    assert sorted(routes, reverse=True) == sorted(PRIORITIES.values(), reverse=True)
    for letter, priority in PRIORITIES.items():
        assert _names(routes[priority]) == _names(_route(letter)), letter
    for attest, value in (("jarvis-edge-attest-home", "home"), ("jarvis-edge-attest-guest", "guest"),
                          ("jarvis-edge-attest-authenticated", "authenticated")):
        assert middlewares[attest]["headers"]["customRequestHeaders"]["X-Jarvis-Edge-Class"] == value
    guest = routes[PRIORITIES["G"]]["match"]
    assert "!HeaderRegexp(`Cf-Connecting-Ip`, `.+`)" in guest and "!HeaderRegexp(`Cf-Ray`, `.+`)" in guest
    assert "ClientIP(`YOUR_GUEST_CIDR`)" in guest


def _relay_exempt_block():
    """The commented relay-exempt Middleware (xander L1): uncommented and
    parsed, so it's a real Middleware the day someone enables it."""
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text.split("# apiVersion: traefik.io/v1alpha1\n# kind: Middleware\n# metadata:\n#   name: jarvis-edge-strip-except-relay", 1)
    assert len(block) == 2, "the jarvis-edge-strip-except-relay Middleware is defined (commented)"
    body = "apiVersion: traefik.io/v1alpha1\nkind: Middleware\nmetadata:\n  name: jarvis-edge-strip-except-relay" + block[1]
    return yaml.safe_load("\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in body.splitlines()))


def test_relay_exempt_middleware_defined_and_keeps_only_the_relay_headers():
    doc = _relay_exempt_block()
    assert doc["kind"] == "Middleware" and doc["metadata"]["namespace"] == "athena-prod"
    stripped = {n.lower() for n, v in doc["spec"]["headers"]["customRequestHeaders"].items() if v == ""}
    relay = {"x-jarvis-relay-key", "x-jarvis-relay-client"}
    assert stripped == (_effective_strip_set() | {n.lower() for n in AUTHENTIK_HEADERS}) - relay
    assert "x-jarvis-edge-attestation" in stripped and "x-service-key" in stripped
    text = TEMPLATE.read_text(encoding="utf-8")
    route = text.split("#     - name: jarvis-edge-strip-except-relay", 1)[1].split("\n\n", 1)[0]
    assert "attest" not in route
