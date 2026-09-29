"""Edge-attested mode (V4.2, D1, A1, A5, A8).

The auth proxy classifies and attests; jarvis-web honours the class only
with the right secret, from a trusted proxy peer, and (for home/guest) with
the D8 candidate and Host corroborating it.

Groups fixture source: Authentik's proxy outpost joins groups with "|":
goauthentik/authentik, tag version/2025.8.1,
internal/outpost/proxyv2/application/mode_common.go lines 42-43:
    headers.Set("X-authentik-username", c.PreferredUsername)
    headers.Set("X-authentik-groups", strings.Join(c.Groups, "|"))
Not a live capture (the cluster wasn't touched at this stage); the live
proof is rollout step 6(a).
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap

import pytest
import yaml

from . import _jarvis_web_harness as h

caller_auth = h.caller_auth
SECRET = "edge-attestation-current-7f3a9c2e1b8d4f6a"
PREVIOUS = "edge-attestation-previous-5d2e8b1c9a7f3e6d"
EDGE_ENV = {**h.HOME_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household"}
TEMPLATE = h.REPO_ROOT / "manifests" / "athena-prod" / "optional" / "jarvis-web-edge-auth.yaml"
AUTHENTIK_GROUPS = "|".join(["household", "music-assistant", "authentik Admins"])  # strings.Join(c.Groups, "|")


@pytest.fixture(autouse=True)
def _reset():
    h.configure(EDGE_ENV)
    yield
    h.configure()


@pytest.fixture
def out(monkeypatch):
    return h.install_outbound(monkeypatch)


def _edge(cls, source=h.LAN, secret=SECRET, **extra):
    headers = {"X-Forwarded-For": source, "X-Jarvis-Edge-Class": cls, "X-Jarvis-Edge-Attestation": secret}
    headers.update(extra)
    return headers


def _signed_in(user="alice", groups=AUTHENTIK_GROUPS, source=h.INTERNET, **extra):
    return _edge("authenticated", source=source, **{"X-authentik-username": user, "X-authentik-groups": groups}, **extra)


def _welcome(headers, peer=h.PROXY, host=h.HOST):
    return h.client(peer=peer, host=host).get("/api/welcome", headers=headers)


def test_edge_home_with_secret_is_local(out):
    resp = _welcome(_edge("home"))
    assert resp.status_code == 200
    assert resp.json()["capabilities"]["signed_in"] is False


def test_edge_class_without_secret_is_public(out):
    headers = {"X-Forwarded-For": h.LAN, "X-Jarvis-Edge-Class": "home"}
    assert _welcome(headers).status_code == 401


def test_edge_secret_from_untrusted_peer_is_public(out):
    """Named: the right secret, but not from a trusted proxy. The same
    headers from the trusted proxy are served (positive control)."""
    assert _welcome(_edge("home")).status_code == 200
    assert _welcome(_signed_in()).status_code == 200
    assert _welcome(_edge("home"), peer="198.51.100.77").status_code == 401
    assert _welcome(_signed_in(), peer="198.51.100.77").status_code == 401


def test_wrong_secret_same_length_is_public(out):
    wrong = SECRET[:-1] + ("0" if SECRET[-1] != "0" else "1")
    assert len(wrong) == len(SECRET)
    assert _welcome(_edge("home", secret=wrong)).status_code == 401


@pytest.mark.parametrize("value", ["owner", "HOME", "", "home ", "Authenticated"])
def test_bad_edge_class_values_are_none(out, value):
    assert _welcome(_edge(value)).status_code == 401


def test_duplicate_attestation_header_is_none(out):
    c = h.client()
    raw = [("x-forwarded-for", h.LAN), ("x-jarvis-edge-class", "home"),
           ("x-jarvis-edge-attestation", SECRET), ("x-jarvis-edge-attestation", SECRET)]
    assert c.get("/api/welcome", headers=raw).status_code == 401
    raw = [("x-forwarded-for", h.LAN), ("x-jarvis-edge-class", "home"), ("x-jarvis-edge-class", "authenticated"),
           ("x-jarvis-edge-attestation", SECRET)]
    assert c.get("/api/welcome", headers=raw).status_code == 401


def test_edge_home_requires_local_candidate(out):
    assert _welcome(_edge("home", source=h.INTERNET)).status_code == 401


def test_edge_home_requires_allowed_host(out):
    assert _welcome(_edge("home"), host="evil.example").status_code == 401


def test_edge_home_with_cf_headers_not_home(out):
    assert _welcome(_edge("home", **{"Cf-Ray": "abc"})).status_code == 401


@pytest.mark.parametrize("override, fault", [
    ({"TRUSTED_PROXY_CIDRS": ""}, "TRUSTED_PROXY_CIDRS is empty"),
    ({"JARVIS_LOCAL_NETWORKS": "", "JARVIS_GUEST_NETWORKS": ""}, "JARVIS_LOCAL_NETWORKS has no usable entry"),
    ({"JARVIS_LOCAL_NETWORKS": "10.0.0.0/24"}, "JARVIS_LOCAL_NETWORKS has no usable entry"),  # inside the proxies
    ({"JARVIS_ALLOWED_HOSTS": ""}, "JARVIS_ALLOWED_HOSTS is empty"),
])
def test_edge_misconfig_is_a_startup_failure(override, fault, captured_logs):
    """ian H2 (named): an edge pod that couldn't serve the household never
    starts, so a rolling deploy stalls instead of going Ready and 401ing."""
    with pytest.raises(SystemExit) as excinfo:
        caller_auth.load_settings({**EDGE_ENV, **override}, own_ips=())
    assert fault in str(excinfo.value)
    assert any(e["event"] == "jarvis_edge_misconfigured" and fault in e["faults"] for e in captured_logs)


def test_edge_sign_in_only_starts_without_home(out):
    h.configure({**EDGE_ENV, "JARVIS_LOCAL_NETWORKS": "", "JARVIS_GUEST_NETWORKS": "", "JARVIS_EDGE_SIGN_IN_ONLY": "true"})
    assert _welcome(_edge("home")).status_code == 401
    assert _welcome(_signed_in()).status_code == 200
    with pytest.raises(SystemExit):
        caller_auth.load_settings({**EDGE_ENV, "TRUSTED_PROXY_CIDRS": "", "JARVIS_EDGE_SIGN_IN_ONLY": "true"}, own_ips=())


def test_custom_edge_header_names_need_the_strip_ack(captured_logs):
    """xander M1 (named): a configured identity/groups header outside the
    documented strip list is fatal unless acknowledged; the effective strip
    set always includes it, and is logged."""
    custom = {**EDGE_ENV, "JARVIS_EDGE_IDENTITY_HEADER": "X-Remote-User", "JARVIS_EDGE_GROUPS_HEADER": "X-Remote-Groups"}
    with pytest.raises(SystemExit) as excinfo:
        caller_auth.load_settings(custom, own_ips=())
    assert "X-Remote-User" in str(excinfo.value) and "X-Remote-Groups" in str(excinfo.value)
    assert any(e["event"] == "jarvis_edge_header_not_in_strip_list" for e in captured_logs)
    s = caller_auth.load_settings({**custom, "JARVIS_EDGE_HEADERS_ACK_STRIPPED": "x-remote-groups, X-Remote-User"},
                                  own_ips=())
    assert {"X-Remote-User", "X-Remote-Groups"} <= set(s.edge_strip_headers)
    assert set(caller_auth.EDGE_STRIPPED_HEADERS) <= set(s.edge_strip_headers)
    logged = [e for e in captured_logs if e["event"] == "jarvis_edge_strip_headers"]
    assert logged and "X-Remote-User" in logged[-1]["headers"]


@pytest.mark.parametrize("ack", ["true", "1", "X-Remote-User", "X-Remote-User,X-Remote-Groups,X-Other", "X-Old-User,X-Old-Groups"])
def test_edge_header_ack_is_bound_to_the_names(ack):
    """xander (named: "true"): the ack must list exactly the custom header
    names in use; a boolean, a partial list, an extra name or an ack left
    from another configuration doesn't pass."""
    custom = {**EDGE_ENV, "JARVIS_EDGE_IDENTITY_HEADER": "X-Remote-User", "JARVIS_EDGE_GROUPS_HEADER": "X-Remote-Groups",
              "JARVIS_EDGE_HEADERS_ACK_STRIPPED": ack}
    with pytest.raises(SystemExit):
        caller_auth.load_settings(custom, own_ips=())


def test_sign_in_only_without_allowed_hosts_logs_websockets_error(captured_logs):
    """xander: sign-in-only mode can start with JARVIS_ALLOWED_HOSTS empty;
    then every WebSocket is refused, and that is an ERROR, not a warning."""
    caller_auth.load_settings({**EDGE_ENV, "JARVIS_LOCAL_NETWORKS": "", "JARVIS_GUEST_NETWORKS": "",
                               "JARVIS_ALLOWED_HOSTS": "", "JARVIS_EDGE_SIGN_IN_ONLY": "true"}, own_ips=())
    events = [e for e in captured_logs if e["event"] == "jarvis_websockets_disabled"]
    assert events and events[-1]["log_level"] == "error"


def test_default_edge_header_names_need_no_ack():
    s = caller_auth.load_settings({**EDGE_ENV, "JARVIS_EDGE_IDENTITY_HEADER": "x-AUTHENTIK-username"}, own_ips=())
    assert len(s.edge_strip_headers) == len(caller_auth.EDGE_STRIPPED_HEADERS)


def test_edge_mode_ignores_xff_for_classification(out):
    """In edge mode the network alone never grants home: no attestation,
    a home-looking hop from a trusted peer -> 401."""
    assert _welcome({"X-Forwarded-For": h.LAN}).status_code == 401


def test_edge_guest_requires_guest_candidate(out):
    assert _welcome(_edge("guest", source=h.GUEST_WIFI)).status_code == 200
    caps = _welcome(_edge("guest", source=h.GUEST_WIFI)).json()["capabilities"]
    assert caps["control_reason"] == "guest_network"
    assert _welcome(_edge("guest", source=h.LAN)).status_code == 401
    assert _welcome(_edge("home", source=h.GUEST_WIFI)).status_code == 401


def test_edge_authenticated_requires_household_group(out):
    resp = _welcome(_signed_in())
    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["signed_in"] is True and caps["control"] is True and caps["display_name"] == "alice"


@pytest.mark.parametrize("groups, allowed", [
    (AUTHENTIK_GROUPS, True),
    ("admins|not-household", False),
    ("household-evil", False),
    ("Household", False),
    (" household | admins ", True),
    ("admins||household", True),
    ("", False),
])
def test_groups_rows(out, groups, allowed):
    resp = _welcome(_signed_in(groups=groups))
    if allowed:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 403 and resp.json() == {"detail": "not_household"}


@pytest.mark.parametrize("identity", ["", "   ", "\t"])
def test_blank_identity_is_not_household(out, identity):
    """tessa ED-g (as read here: a blank identity must not pass as a
    household member even with a household group)."""
    resp = _welcome(_signed_in(user=identity))
    assert resp.status_code == 403 and resp.json()["detail"] == "not_household"


def test_authenticated_without_identity_is_403_not_household(out):
    headers = _edge("authenticated", source=h.INTERNET, **{"X-authentik-groups": "household"})
    resp = _welcome(headers)
    assert resp.status_code == 403 and resp.json()["detail"] == "not_household"


def test_household_groups_empty_honours_no_identity(out, captured_logs):
    h.configure({**EDGE_ENV, "JARVIS_HOUSEHOLD_GROUPS": ""})
    assert any(e["event"] == "jarvis_household_groups_empty" for e in captured_logs)
    assert _welcome(_signed_in()).status_code == 403


def test_identity_ignored_under_home(out):
    """otto H1: identity headers are read only for an authenticated
    verdict; under home a pre-seeded identity is ignored."""
    resp = _welcome(_edge("home", **{"X-authentik-username": "mallory", "X-authentik-groups": "household"}))
    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["signed_in"] is False and "display_name" not in caps


def test_edge_identity_headers_only_with_attestation(out):
    headers = {"X-Forwarded-For": h.INTERNET, "X-authentik-username": "alice", "X-authentik-groups": "household"}
    assert _welcome(headers).status_code == 401


def test_authenticated_verdict_not_downgraded_by_home_candidate(out):
    resp = _welcome(_signed_in(source=h.LAN))
    assert resp.status_code == 200 and resp.json()["capabilities"]["signed_in"] is True


def test_bearer_still_applies_in_edge_mode(out):
    h.install_role("owner")
    resp = h.client(peer=h.INTERNET).get("/api/welcome", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200


def test_signed_in_owner_has_control_during_stay(monkeypatch):
    h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "G", "id": 1})
    resp = h.client().post("/api/climate/mode/heat", headers={**_signed_in(), **h.CSRF})
    assert resp.status_code == 200


def test_previous_secret_accepted_and_logged_as_previous(out, captured_logs):
    h.configure({**EDGE_ENV, "JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS": PREVIOUS})
    assert _welcome(_edge("home", secret=PREVIOUS)).status_code == 200
    resolved = [e for e in captured_logs if e["event"] == "jarvis_caller_resolved"]
    assert resolved[-1]["edge_attestation"] == "previous"
    assert _welcome(_edge("home")).status_code == 200
    assert [e for e in captured_logs if e["event"] == "jarvis_caller_resolved"][-1]["edge_attestation"] == "current"


def test_previous_unset_rejects_previous_shaped_value(out):
    assert _welcome(_edge("home", secret=PREVIOUS)).status_code == 401


def test_attestation_values_never_logged(out, captured_logs, caplog):
    h.configure({**EDGE_ENV, "JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS": PREVIOUS})
    _welcome(_edge("home"))
    _welcome(_edge("home", secret=PREVIOUS))
    _welcome(_edge("home", secret="x" * 40))
    rendered = repr(captured_logs) + caplog.text
    assert SECRET not in rendered and PREVIOUS not in rendered
    values = {e.get("edge_attestation") for e in captured_logs if e["event"] == "jarvis_caller_resolved"}
    assert {"current", "previous", "none"} <= values
    assert SECRET not in repr(caller_auth.SETTINGS) and PREVIOUS not in repr(caller_auth.SETTINGS)


def _run_settings(env):
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(h.BACKEND)!r})
        os.environ.update({env!r})
        import caller_auth
        print("STARTED")
    """)
    base = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(h.REPO_ROOT / "src")}
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=base, timeout=60)


@pytest.mark.parametrize("which", ["current", "previous"])
@pytest.mark.parametrize("fault", ["placeholder", "short", "service_collision", "relay_collision"])
def test_bad_edge_secret_exits(which, fault):
    bad = {
        "placeholder": "CONFIGURE_ME_EDGE_ATTESTATION_VALUE_PADDING",
        "short": "too-short-secret",
        "service_collision": h.SERVICE_KEY + "-padding-to-32-characters",
        "relay_collision": "relay-key-that-is-long-enough-0123456789",
    }[fault]
    env = {"SERVICE_API_KEY": h.SERVICE_KEY + "-padding-to-32-characters" if fault == "service_collision" else h.SERVICE_KEY,
           "JARVIS_RELAY_KEY": "relay-key-that-is-long-enough-0123456789",
           "JARVIS_EDGE_ATTESTATION_SECRET": SECRET}
    env["JARVIS_EDGE_ATTESTATION_SECRET" if which == "current" else "JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS"] = bad
    proc = _run_settings(env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "STARTED" not in output
    assert "jarvis_edge_attestation_rejected" in output
    assert bad not in output


def test_good_edge_secrets_start():
    proc = _run_settings({**EDGE_ENV, "JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS": PREVIOUS})
    assert proc.returncode == 0 and "STARTED" in proc.stdout


def test_secret_equal_to_service_or_relay_key_refused():
    with pytest.raises(SystemExit):
        caller_auth.load_settings({"SERVICE_API_KEY": SECRET, "JARVIS_EDGE_ATTESTATION_SECRET": SECRET}, own_ips=())
    with pytest.raises(SystemExit):
        caller_auth.load_settings({"JARVIS_RELAY_KEY": SECRET, "JARVIS_EDGE_ATTESTATION_SECRET": SECRET}, own_ips=())
    with pytest.raises(SystemExit):
        caller_auth.load_settings({"JARVIS_EDGE_ATTESTATION_SECRET": SECRET,
                                   "JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS": SECRET}, own_ips=())


def test_direct_mode_with_edge_exits():
    with pytest.raises(SystemExit):
        caller_auth.load_settings({**EDGE_ENV, "JARVIS_DIRECT_CLIENTS": "true", "TRUSTED_PROXY_CIDRS": ""}, own_ips=())


def test_default_header_names_match_template():
    """The identity/groups header names jarvis-web reads by default are
    exactly what the template's forwardAuth copies back."""
    docs = list(yaml.safe_load_all(TEMPLATE.read_text(encoding="utf-8")))
    forward = next(d for d in docs if d and d["kind"] == "Middleware" and "forwardAuth" in d["spec"])
    pattern = re.compile(forward["spec"]["forwardAuth"]["authResponseHeadersRegex"])
    s = caller_auth.load_settings({}, own_ips=())
    for name in (s.identity_header, s.groups_header, "X-Authentik-Groups"):
        assert pattern.search(name), name
    assert s.groups_separator == "|"
