"""Edge/app home-rule parity (V4.7, tessa C1).

The template's route H and G predicates and jarvis-web's own home and
guest-network classification must agree on every row: both are evaluated
here, the edge one from the match string parsed out of the template. The
parser reads lists, not boolean structure, so each full match string is
also pinned to its expected literal: a change to how the terms combine
fails the pin even when every list stays the same. This proves the
template's agreement only; a house's live routes are covered by its
rollout evidence.
"""
from __future__ import annotations

import ipaddress
import re

import pytest
import yaml

from . import _jarvis_web_harness as h

TEMPLATE = h.REPO_ROOT / "manifests" / "athena-prod" / "optional" / "jarvis-web-edge-auth.yaml"
EXPECTED_HOME = ["192.0.2.0/24", "2001:db8:1::/64"]
EXPECTED_EXCLUDED = ["192.0.2.1/32", "192.0.2.14/32"]
EXPECTED_GUEST = ["198.51.100.0/24"]
EXPECTED_GUEST_EXCLUDED = ["198.51.100.1/32"]
HOSTS = "(Host(`jarvis.example.com`) || Host(`chat.example.com`))"
NO_CF = "!HeaderRegexp(`Cf-Connecting-Ip`, `.+`) && !HeaderRegexp(`Cf-Ray`, `.+`)"
H_MATCH = (f"{HOSTS} && {NO_CF} && (ClientIP(`192.0.2.0/24`) || ClientIP(`2001:db8:1::/64`))"
           " && !ClientIP(`192.0.2.1/32`) && !ClientIP(`192.0.2.14/32`)")
G_MATCH = f"{HOSTS} && {NO_CF} && (ClientIP(`198.51.100.0/24`)) && !ClientIP(`198.51.100.1/32`)"


def _route(priority):
    docs = list(yaml.safe_load_all(TEMPLATE.read_text(encoding="utf-8")))
    ingress = next(d for d in docs if d and d["kind"] == "IngressRoute")
    return next(r for r in ingress["spec"]["routes"] if r["priority"] == priority)


def _parse(match):
    positive = re.findall(r"(?<!!)ClientIP\(`([^`]+)`\)", match)
    negative = re.findall(r"!ClientIP\(`([^`]+)`\)", match)
    absent_headers = re.findall(r"!HeaderRegexp\(`([^`]+)`, `\.\+`\)", match)
    hosts = re.findall(r"Host\(`([^`]+)`\)", match)
    return positive, negative, absent_headers, hosts


def edge_is_home(match, source, headers, host):
    """Traefik's route-H predicate: Host, every negated HeaderRegexp (header
    names are case-insensitive; `.+` needs a non-empty value), a positive
    ClientIP, and no excluded ClientIP."""
    positive, negative, absent, hosts = _parse(match)
    if host not in hosts:
        return False
    lowered = {k.lower(): v for k, v in headers.items()}
    if any(re.search(".+", lowered.get(name.lower(), "") or "") for name in absent):
        return False
    addr = ipaddress.ip_address(source)
    inside = lambda cidr: addr.version == ipaddress.ip_network(cidr).version and addr in ipaddress.ip_network(cidr)
    return any(inside(c) for c in positive) and not any(inside(c) for c in negative)


def _app_class(source, headers, host, env):
    s = h.caller_auth.load_settings({
        "TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
        "JARVIS_ALLOWED_HOSTS": "jarvis.example.com,chat.example.com",
        **env,
    }, own_ips=())
    from starlette.datastructures import Headers

    request_headers = Headers(headers={"x-forwarded-for": source, "host": host, **headers})
    cls, _ = h.caller_auth.classify_network(h.PROXY, request_headers, s)
    return cls


def app_is_home(source, headers, host, positive, negative):
    env = {"JARVIS_LOCAL_NETWORKS": ",".join(positive), "JARVIS_LOCAL_EXCLUDE": ",".join(negative)}
    return _app_class(source, headers, host, env) == h.caller_auth.CLASS_LOCAL


def app_is_guest(source, headers, host, positive, negative):
    env = {"JARVIS_GUEST_NETWORKS": ",".join(positive), "JARVIS_LOCAL_EXCLUDE": ",".join(negative)}
    return _app_class(source, headers, host, env) == h.caller_auth.CLASS_GUEST_NET


ROWS = [
    # (name, source, extra headers, host, expected home)
    ("lan_no_cf", "192.0.2.50", {}, "jarvis.example.com", True),
    ("lan_cf_ray", "192.0.2.50", {"Cf-Ray": "8a1b2c"}, "jarvis.example.com", False),
    ("lan_cf_connecting_ip", "192.0.2.50", {"Cf-Connecting-Ip": "203.0.113.9"}, "jarvis.example.com", False),
    ("lan_cf_empty_value", "192.0.2.50", {"Cf-Ray": ""}, "jarvis.example.com", True),
    ("lan_lowercase_cf_ray", "192.0.2.50", {"cf-ray": "8a1b2c"}, "jarvis.example.com", False),
    ("lan_ipv6", "2001:db8:1::42", {}, "jarvis.example.com", True),
    ("excluded_gateway", "192.0.2.1", {}, "jarvis.example.com", False),
    ("excluded_node", "192.0.2.14", {}, "jarvis.example.com", False),
    ("public", "203.0.113.9", {}, "jarvis.example.com", False),
    ("guest_wifi_not_home", "198.51.100.20", {}, "jarvis.example.com", False),
    ("chat_host", "192.0.2.50", {}, "chat.example.com", True),
    ("other_host", "192.0.2.50", {}, "evil.example", False),
]


def test_template_home_set_is_the_expected_literal():
    positive, negative, absent, _ = _parse(_route(900)["match"])
    assert positive == EXPECTED_HOME
    assert negative == EXPECTED_EXCLUDED
    assert sorted(absent) == ["Cf-Connecting-Ip", "Cf-Ray"]


@pytest.mark.parametrize("priority, expected", [(900, H_MATCH), (910, G_MATCH)], ids=["H", "G"])
def test_route_match_is_the_expected_literal(priority, expected):
    """Named (tessa C1): the whole H and G match strings, so the way the
    terms combine is pinned, not only which terms appear."""
    assert _route(priority)["match"] == expected


GUEST_ROWS = [
    # (name, source, extra headers, host, expected guest)
    ("guest_wifi", "198.51.100.20", {}, "jarvis.example.com", True),
    ("guest_wifi_chat_host", "198.51.100.20", {}, "chat.example.com", True),
    ("guest_cf_empty_value", "198.51.100.20", {"Cf-Ray": ""}, "jarvis.example.com", True),
    ("lan_not_guest", "192.0.2.50", {}, "jarvis.example.com", False),
    ("guest_gateway_excluded", "198.51.100.1", {}, "jarvis.example.com", False),
    ("guest_cf_ray", "198.51.100.20", {"Cf-Ray": "8a1b2c"}, "jarvis.example.com", False),
    ("guest_cf_connecting_ip", "198.51.100.20", {"Cf-Connecting-Ip": "203.0.113.9"}, "jarvis.example.com", False),
    ("public", "203.0.113.9", {}, "jarvis.example.com", False),
    ("guest_other_host", "198.51.100.20", {}, "evil.example", False),
]


@pytest.mark.parametrize("name, source, headers, host, expected", GUEST_ROWS, ids=[r[0] for r in GUEST_ROWS])
def test_guest_edge_and_app_agree(name, source, headers, host, expected):
    """Named (tessa C1): a guest-network source is route G and jarvis-web's
    guest network; a LAN source, the guest gateway /32 and a Cloudflare
    header are neither."""
    match = _route(910)["match"]
    positive, negative, _, _ = _parse(match)
    assert positive == EXPECTED_GUEST and negative == EXPECTED_GUEST_EXCLUDED
    assert edge_is_home(match, source, headers, host) is expected
    assert app_is_guest(source, headers, host, positive, negative) is expected


@pytest.mark.parametrize("name, source, headers, host, expected", ROWS, ids=[r[0] for r in ROWS])
def test_edge_and_app_agree(name, source, headers, host, expected):
    """Named positive member lan_no_cf; named negative lan_cf_ray."""
    match = _route(900)["match"]
    positive, negative, _, _ = _parse(match)
    assert edge_is_home(match, source, headers, host) is expected
    assert app_is_home(source, headers, host, positive, negative) is expected


def test_row_population():
    for rows in (ROWS, GUEST_ROWS):
        assert sum(1 for r in rows if r[4]) >= 1 and sum(1 for r in rows if not r[4]) >= 1
    assert len(ROWS) >= 10 and len(GUEST_ROWS) >= 8
