"""Edge/app home-rule parity (V4.7).

The template's route H predicate and jarvis-web's own home classification
must agree on every row: both are evaluated here, the edge one from the
match string parsed out of the template. This proves the template's
agreement only; a house's live route is covered by its rollout evidence.
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


def app_is_home(source, headers, host, positive, negative):
    s = h.caller_auth.load_settings({
        "TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
        "JARVIS_LOCAL_NETWORKS": ",".join(positive),
        "JARVIS_LOCAL_EXCLUDE": ",".join(negative),
        "JARVIS_ALLOWED_HOSTS": "jarvis.example.com,chat.example.com",
    }, own_ips=())
    from starlette.datastructures import Headers

    request_headers = Headers(headers={"x-forwarded-for": source, "host": host, **headers})
    cls, _ = h.caller_auth.classify_network(h.PROXY, request_headers, s)
    return cls == h.caller_auth.CLASS_LOCAL


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


@pytest.mark.parametrize("name, source, headers, host, expected", ROWS, ids=[r[0] for r in ROWS])
def test_edge_and_app_agree(name, source, headers, host, expected):
    """Named positive member lan_no_cf; named negative lan_cf_ray."""
    match = _route(900)["match"]
    positive, negative, _, _ = _parse(match)
    assert edge_is_home(match, source, headers, host) is expected
    assert app_is_home(source, headers, host, positive, negative) is expected


def test_row_population():
    assert sum(1 for r in ROWS if r[4]) >= 1 and sum(1 for r in ROWS if not r[4]) >= 1
    assert len(ROWS) >= 10
