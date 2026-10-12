"""ha_credentials: one rule for Home Assistant's address and token."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import hmac
import json
import logging
import pathlib
import pickle

import pytest
import structlog

from shared import ha_credentials as hc
from shared.ha_credentials import HACredentialResolver, KeyLookup, ha_url_problem, resolve

ROOT = pathlib.Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "tests" / "fixtures" / "ha_url_cases.json").read_text())["cases"]
TOKEN = "sekret-token-123"
HOST = "ha-fixture.invalid"
URL = f"https://{HOST}"
KEY = "service-key-for-tests"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def outside_a_pod(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("IN_CLUSTER", raising=False)


def creds(lookup, env=None, previous=None, key=KEY):
    return resolve(lookup, env or {}, previous=previous, service_api_key=key)


# -- the URL policy ------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["url"][:40] or "empty")
def test_the_url_table(case):
    assert ha_url_problem(case["url"], source="env") == case["env"]
    assert ha_url_problem(case["url"], source="admin") == case["admin"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8123", "http://[::1]:8123", "http://[::ffff:127.0.0.1]:8123"])
def test_a_loopback_literal_is_an_admin_problem_inside_a_pod_only(monkeypatch, url):
    assert ha_url_problem(url, source="admin") is None
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    assert ha_url_problem(url, source="admin") == "host_not_allowed"
    assert ha_url_problem(url, source="env") is None, "a deployer's in-cluster setup keeps working"


def test_the_problem_is_a_fixed_word_never_the_url():
    for case in CASES:
        for source in ("env", "admin"):
            word = ha_url_problem(case["url"], source=source)
            assert word is None or word in hc.PROBLEM_WORDS
            if word and case["url"]:
                assert case["url"] not in word


@pytest.mark.parametrize("value", [None, 3, b"http://h", ["http://h"]])
def test_a_non_string_is_empty(value):
    assert ha_url_problem(value, source="env") == "empty"


# -- the precedence rule -------------------------------------------------------------------------------------


def test_the_admin_key_wins_over_the_environment():
    c = creds(KeyLookup.found(URL, TOKEN), {"HA_URL": "http://env.example.invalid:8123", "HA_TOKEN": "env-token"})
    assert (c.source, c.url, c.token.reveal(), c.configured) == ("admin", URL, TOKEN, True)
    assert c.ws_url == f"wss://{HOST}/api/websocket"


def test_an_absent_admin_key_falls_back_to_the_environment():
    c = creds(KeyLookup.absent(), {"HA_URL": "http://env.example.invalid:8123/", "HA_TOKEN": "env-token"})
    assert (c.source, c.url, c.token.reveal(), c.configured) == ("env", "http://env.example.invalid:8123", "env-token", True)
    assert c.ws_url == "ws://env.example.invalid:8123/api/websocket"


def test_nothing_anywhere_is_not_configured():
    c = creds(KeyLookup.absent())
    assert (c.source, c.configured, c.problem, c.digest) == ("none", False, None, "")


@pytest.mark.parametrize("lookup", [
    KeyLookup.found(URL, ""), KeyLookup.found("", TOKEN), KeyLookup.found("", ""), KeyLookup.found(None, None),
])
def test_a_half_filled_admin_row_never_mixes_with_the_environment(lookup):
    c = creds(lookup, {"HA_URL": "http://env.example.invalid:8123", "HA_TOKEN": "env-token"})
    assert (c.source, c.configured, c.problem) == ("admin", False, "incomplete")


@pytest.mark.parametrize("bad,word", [
    ("http://169.254.169.254/", "blocked_address"), ("http://homeassistant.svc", "host_not_allowed"),
    ("ftp://x", "bad_scheme"), ("http://a@b", "unsafe_character"), ("http://h/p", "path"),
])
def test_an_invalid_admin_url_is_rejected_without_falling_through_to_env(bad, word):
    c = creds(KeyLookup.found(bad, TOKEN), {"HA_URL": "http://env.example.invalid:8123", "HA_TOKEN": "env-token"})
    assert (c.source, c.configured, c.problem) == ("admin", False, word)
    assert c.token.reveal() == "" and c.url == ""


def test_a_half_filled_environment_is_not_configured():
    assert creds(KeyLookup.absent(), {"HA_URL": "http://h.example.invalid"}).problem == "incomplete"
    assert creds(KeyLookup.absent(), {"HA_TOKEN": "t"}).configured is False


def test_an_unreachable_admin_keeps_the_last_admin_value_and_otherwise_reads_the_environment():
    admin = creds(KeyLookup.found(URL, TOKEN))
    assert creds(KeyLookup.unavailable(), {"HA_URL": "http://env.example.invalid", "HA_TOKEN": "e"}, previous=admin) is admin
    env_only = creds(KeyLookup.unavailable(), {"HA_URL": "http://env.example.invalid", "HA_TOKEN": "e"})
    assert (env_only.source, env_only.configured) == ("env", True)
    after_env = creds(KeyLookup.unavailable(), {"HA_URL": "http://env.example.invalid", "HA_TOKEN": "e"}, previous=env_only)
    assert after_env.source == "env"


def test_values_are_stripped():
    c = creds(KeyLookup.found(f"  {URL}  ", f"  {TOKEN}\n"))
    assert (c.url, c.token.reveal()) == (URL, TOKEN)
    e = creds(KeyLookup.absent(), {"HA_URL": " http://env.example.invalid ", "HA_TOKEN": " t "})
    assert (e.url, e.token.reveal()) == ("http://env.example.invalid", "t")


# -- HA_WS_URL -------------------------------------------------------------------------------------------------


def env_with_ws(value):
    return {"HA_URL": "https://ha.example.invalid", "HA_TOKEN": "t", "HA_WS_URL": value}


def test_ws_url_is_honoured_only_with_env_credentials():
    c = creds(KeyLookup.absent(), env_with_ws("wss://ha.example.invalid:9000/api/websocket"))
    assert c.ws_url == "wss://ha.example.invalid:9000/api/websocket" and c.notes == ()
    appended = creds(KeyLookup.absent(), env_with_ws("wss://ha.example.invalid:9000"))
    assert appended.ws_url == "wss://ha.example.invalid:9000/api/websocket"
    admin = creds(KeyLookup.found(URL, TOKEN), env_with_ws("wss://other.example.invalid/api/websocket"))
    assert admin.ws_url == f"wss://{HOST}/api/websocket" and "ws_url_ignored" in admin.notes


@pytest.mark.parametrize("raw", [
    "http://ha.example.invalid/api/websocket", "wss://ha.example.invalid/other", "wss://ha.example.invalid/?x=1",
    "wss://a@ha.example.invalid", "wss://[fd00:ec2::254]/api/websocket", "ws://169.254.169.254", "not a url", "wss://",
])
def test_a_bad_ws_url_is_ignored_and_the_derived_one_stands(raw):
    c = creds(KeyLookup.absent(), env_with_ws(raw))
    assert c.ws_url == "wss://ha.example.invalid/api/websocket" and "ws_url_ignored" in c.notes


def test_a_ws_url_on_another_host_is_noted_without_naming_it():
    c = creds(KeyLookup.absent(), env_with_ws("wss://elsewhere.example.invalid/api/websocket"))
    assert c.ws_url == "wss://elsewhere.example.invalid/api/websocket" and c.notes == ("ws_url_host_mismatch",)


def test_plain_http_is_noted():
    assert creds(KeyLookup.found("http://ha.example.invalid:8123", TOKEN)).notes == ("plain_http",)
    assert creds(KeyLookup.found(URL, TOKEN)).notes == ()


# -- the digest ----------------------------------------------------------------------------------------------------


def test_the_digest_binds_the_origin_and_the_token():
    base = creds(KeyLookup.found(URL, TOKEN))
    assert base.digest and len(base.digest) == 32
    assert creds(KeyLookup.found(URL, TOKEN)).digest == base.digest
    assert creds(KeyLookup.found(URL + "/", TOKEN)).digest == base.digest, "a trailing slash is the same origin"
    assert creds(KeyLookup.found("https://other.example.invalid", TOKEN)).digest != base.digest
    assert creds(KeyLookup.found("http://" + HOST, TOKEN)).digest != base.digest
    assert creds(KeyLookup.found(URL, TOKEN + "x")).digest != base.digest
    assert creds(KeyLookup.found(URL, TOKEN), key="another-key").digest != base.digest


def test_the_digest_is_keyed_and_is_the_documented_hmac():
    c = creds(KeyLookup.found(URL, TOKEN))
    expected = hmac.new(KEY.encode(), f"ha-cred-v2\nhttps://{HOST}:443\n{TOKEN}".encode(), hashlib.sha256).hexdigest()[:32]
    assert c.digest == expected and c.shared is True


def test_without_a_service_key_the_digest_is_per_process_and_unshared():
    a = creds(KeyLookup.found(URL, TOKEN), key=None)
    b = creds(KeyLookup.found(URL, TOKEN), key=None)
    assert a.shared is False and a.digest == b.digest and a.digest != creds(KeyLookup.found(URL, TOKEN)).digest


def test_legacy_marker_digest_is_the_registrys_old_token_marker():
    assert hc.legacy_marker_digest(TOKEN, KEY) == hmac.new(KEY.encode(), TOKEN.encode(), hashlib.sha256).hexdigest()[:16]
    assert hc.legacy_marker_digest(TOKEN, None) is None and hc.legacy_marker_digest("", KEY) is None
    from shared import ha_registry

    assert ha_registry.token_marker_key is hc.legacy_marker_digest
    assert ha_registry.websocket_url is hc.websocket_url


# -- the token never prints ---------------------------------------------------------------------------------------------


def test_the_secret_shows_nowhere_but_reveal():
    c = creds(KeyLookup.found(URL, TOKEN))
    for shown in (repr(c), str(c), f"{c}", repr(c.token), str(c.token), f"{c.token}", "%s" % c.token, repr(dataclasses.asdict(c)),
                  str(dataclasses.astuple(c)), repr(copy.deepcopy(c)), repr(copy.copy(c))):
        assert TOKEN not in shown, shown
    assert c.token.reveal() == TOKEN
    with pytest.raises(TypeError):
        pickle.dumps(c)
    with pytest.raises(TypeError):
        pickle.dumps(c.token)
    assert HOST not in repr(c), "neither does the repr carry the host"


def test_credentials_are_frozen_and_not_configured_ones_say_why():
    c = creds(KeyLookup.found(URL, TOKEN))
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.url = "x"
    assert "problem='incomplete'" in repr(creds(KeyLookup.found(URL, "")))


# -- the resolver ----------------------------------------------------------------------------------------------------------


class Source:
    def __init__(self, lookup=None):
        self.lookup = lookup or KeyLookup.found(URL, TOKEN)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup


def public_dns(addresses=("192.168.1.9",)):
    calls = []

    async def lookup(host, port, **kw):
        calls.append(host)
        return [(2, 1, 6, "", (a, 0)) for a in addresses]

    lookup.calls = calls
    return lookup


def resolver(source, *, env=None, dns=None, **kw):
    return HACredentialResolver(source, service_api_key=KEY, env=env if env is not None else {}, getaddrinfo=dns or public_dns(), **kw)


def test_before_start_the_resolver_is_the_environment_only_result():
    r = resolver(Source(), env={"HA_URL": "http://env.example.invalid", "HA_TOKEN": "e"})
    assert r.current().source == "env" and r.current().configured
    assert resolver(Source()).current().configured is False


def test_start_applies_the_admin_key_and_a_change_fires_the_callbacks_once():
    async def scenario():
        source = Source()
        r = resolver(source, env={"HA_URL": "http://env.example.invalid", "HA_TOKEN": "e"})
        changes = []
        r.on_change(lambda new, old: changes.append((new.source, old.source)))
        await r.start()
        await r.refresh()
        source.lookup = KeyLookup.found(URL, "rotated")
        await r.refresh()
        await r.refresh()
        return r, changes, source

    r, changes, source = run(scenario())
    assert changes == [("admin", "env"), ("admin", "admin")]
    assert r.current().token.reveal() == "rotated"


def test_an_admin_outage_keeps_the_last_admin_value():
    async def scenario():
        source = Source()
        r = resolver(source)
        await r.start()
        first = r.current()
        source.lookup = KeyLookup.unavailable()
        await r.refresh()
        source.lookup = RuntimeError("admin is down")
        await r.refresh()
        return first, r.current()

    first, after = run(scenario())
    assert after.identity() == first.identity() and after.configured


def test_a_slow_admin_is_bounded():
    async def scenario():
        async def slow():
            await asyncio.sleep(30)

        r = resolver(slow, lookup_timeout=0.05)
        await r.start()
        return r.current()

    assert run(scenario()).configured is False


def test_a_forced_refresh_runs_at_most_once_per_ten_seconds():
    async def scenario():
        now = [100.0]
        source = Source()
        r = resolver(source, monotonic=lambda: now[0])
        await r.refresh(force=True)
        await r.refresh(force=True)
        now[0] += 9
        await r.refresh(force=True)
        now[0] += 2
        await r.refresh(force=True)
        return source.calls

    assert run(scenario()) == 2


def test_concurrent_refreshes_share_one_admin_read():
    async def scenario():
        source = Source()
        r = resolver(source)
        await asyncio.gather(*[r.refresh() for _ in range(10)])
        return source.calls

    assert run(scenario()) <= 2


def test_run_survives_a_raising_source_and_a_raising_callback():
    async def scenario():
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            await asyncio.sleep(0.005)

        source = Source(RuntimeError("boom"))
        r = HACredentialResolver(source, service_api_key=KEY, env={}, getaddrinfo=public_dns(), sleep=sleep)
        r.on_change(lambda new, old: 1 / 0)
        task = asyncio.ensure_future(r.run())
        await asyncio.sleep(0.05)
        assert not task.done() and not r.current().configured
        source.lookup = KeyLookup.found(URL, TOKEN)
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        return sleeps, r.current(), alive

    sleeps, current, alive = run(scenario())
    assert set(sleeps) == {60.0} and current.configured and alive


def test_snapshot_for_returns_the_current_and_the_previous_snapshot_only():
    async def scenario():
        source = Source()
        r = resolver(source)
        await r.start()
        digests = [r.current().digest]
        for token in ("two", "three"):
            source.lookup = KeyLookup.found(URL, token)
            await r.refresh()
            digests.append(r.current().digest)
        return r, digests

    r, (d1, d2, d3) = run(scenario())
    assert r.snapshot_for(d3).digest == d3 and r.snapshot_for(d2).digest == d2
    assert r.snapshot_for(d1) is None and r.snapshot_for("") is None and r.snapshot_for("unknown") is None


# -- the runtime DNS check -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("address,blocked", [
    ("169.254.169.254", True), ("::ffff:169.254.169.254", True), ("fd00:ec2::254", True), ("fe80::1", True),
    ("224.0.0.1", True), ("0.0.0.0", True), ("192.168.1.9", False), ("10.0.0.5", False), ("93.184.216.34", False),
])
def test_a_name_resolving_to_a_blocked_address_is_rejected(address, blocked):
    async def scenario():
        r = resolver(Source(), dns=public_dns((address,)))
        await r.start()
        return r.current()

    c = run(scenario())
    assert (c.configured, c.problem) == ((False, "blocked_address") if blocked else (True, None))


def test_any_blocked_address_among_several_rejects_the_name():
    async def scenario():
        r = resolver(Source(), dns=public_dns(("192.168.1.9", "169.254.169.254")))
        await r.start()
        return r.current().problem

    assert run(scenario()) == "blocked_address"


@pytest.mark.parametrize("url", ["http://2852039166/", "http://0xa9fea9fe/", "http://169.254.43518/"])
def test_numeric_hosts_are_names_the_resolver_judges_by_what_they_resolve_to(url):
    async def scenario(address):
        dns = public_dns((address,))
        r = resolver(Source(KeyLookup.found(url, TOKEN)), dns=dns)
        await r.start()
        return r.current(), dns.calls

    c, calls = run(scenario("169.254.169.254"))
    assert (c.configured, c.problem) == (False, "blocked_address") and len(calls) == 1
    c, _ = run(scenario("192.168.7.7"))
    assert c.configured is True


def test_a_name_resolving_to_loopback_is_blocked_only_inside_a_pod(monkeypatch):
    async def scenario():
        r = resolver(Source(), dns=public_dns(("127.0.0.1",)))
        await r.start()
        return r.current()

    assert run(scenario()).configured is True
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    assert run(scenario()).problem == "blocked_address"


def test_a_literal_ip_and_an_environment_url_skip_the_lookup():
    async def scenario():
        dns = public_dns()
        r = resolver(Source(KeyLookup.found("http://192.168.1.10:8123", TOKEN)), dns=dns)
        await r.start()
        e = resolver(Source(KeyLookup.absent()), env={"HA_URL": "http://ha.example.invalid", "HA_TOKEN": "t"}, dns=dns)
        await e.start()
        return dns.calls, r.current().configured, e.current().configured

    calls, a, b = run(scenario())
    assert calls == [] and a and b


def test_a_failing_lookup_keeps_the_previous_snapshot_and_logs_once():
    async def scenario():
        source = Source()
        state = {"fail": False}

        async def dns(host, port, **kw):
            if state["fail"]:
                raise OSError("name resolution failed")
            return [(2, 1, 6, "", ("192.168.1.9", 0))]

        r = resolver(source, dns=dns)
        await r.start()
        before = r.current()
        state["fail"] = True
        source.lookup = KeyLookup.found(URL, "rotated-while-dns-is-down")
        with structlog.testing.capture_logs() as logs:
            await r.refresh()
            await r.refresh()
        state["fail"] = False
        await r.refresh()
        return before, r, [e["event"] for e in logs]

    before, r, events = run(scenario())
    assert events.count("ha_credentials_dns_unavailable") == 1
    assert r.current().token.reveal() == "rotated-while-dns-is-down", "applied once the lookup works again"


def test_a_cold_start_with_a_failing_lookup_is_dns_unavailable_until_it_recovers():
    async def scenario():
        state = {"fail": True}

        async def dns(host, port, **kw):
            if state["fail"]:
                raise asyncio.TimeoutError()
            return [(2, 1, 6, "", ("192.168.1.9", 0))]

        r = resolver(Source(), dns=dns)
        await r.start()
        cold = r.current()
        state["fail"] = False
        await r.refresh()
        return cold, r.current()

    cold, recovered = run(scenario())
    assert (cold.configured, cold.problem) == (False, "dns_unavailable") and recovered.configured


# -- logs ---------------------------------------------------------------------------------------------------------------------------


def test_logs_carry_reason_words_and_no_url_host_or_token(caplog):
    async def scenario():
        with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
            for lookup, env in (
                (KeyLookup.found("http://" + HOST + ":8123", TOKEN), {"HA_WS_URL": "wss://other.example.invalid"}),
                (KeyLookup.found("http://169.254.169.254", TOKEN), {}),
                (KeyLookup.found(URL, ""), {}),
            ):
                r = HACredentialResolver(Source(lookup), service_api_key=None, env=env, getaddrinfo=public_dns())
                await r.start()
        return logs

    logs = run(scenario())
    blob = json.dumps(logs, default=str) + caplog.text
    for forbidden in (TOKEN, HOST, "169.254", "other.example.invalid", "://"):
        assert forbidden not in blob, forbidden
    events = [e["event"] for e in logs]
    assert "ha_credentials_rejected" in events and "ha_plain_http" in events and "ha_auth_unshared" in events


def test_a_snapshot_is_announced_once_per_change():
    async def scenario():
        r = resolver(Source())
        with structlog.testing.capture_logs() as logs:
            for _ in range(4):
                await r.refresh()
        return [e["event"] for e in logs]

    assert run(scenario()).count("ha_credentials_resolved") == 1


def test_a_static_resolver_serves_one_snapshot():
    c = creds(KeyLookup.found(URL, TOKEN))
    static = hc.StaticResolver(c)
    assert static.current() is c and static.snapshot_for(c.digest) is c and static.snapshot_for("x") is None
    run(static.refresh(force=True))
