"""Client-IP resolution, home-network candidate, and the sliding-window limiter.

`shared.client_throttle` is the one parser for the forwarded-for rules the
gateway, jarvis-web and chat-embed share. Two functions with two jobs:

- `resolve_rate_client` answers "whose budget does this request spend?" with
  a deep right-to-left walk (a forged left-hand prefix never wins).
- `local_candidate` answers "which single address may be tested against the
  home networks?": only the hop the immediately trusted proxy appended.

Symbols are imported inside fixtures so each member fails on its own when
the module is absent.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import ipaddress
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "shared" / "client_throttle.py"
TRUSTED = "10.244.0.0/16"


@pytest.fixture
def ct():
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return importlib.import_module("shared.client_throttle")


@pytest.fixture
def trusted(ct):
    return ct.parse_networks(TRUSTED)


def _rate(ct, trusted, peer, xff, cf=None, trust_cf=False):
    return ct.resolve_rate_client(peer, xff, cf, trusted, trust_cf)


# ---------------------------------------------------------------------------
# parse_networks
# ---------------------------------------------------------------------------


def test_parse_networks_drops_invalid_and_reports_them(ct):
    nets = ct.parse_networks(" 10.0.0.0/8, bogus ,,192.0.2.1, 2001:db8::/32 ")
    assert [str(n) for n in nets] == ["10.0.0.0/8", "192.0.2.1/32", "2001:db8::/32"]
    assert ct.invalid_network_entries(" 10.0.0.0/8, bogus ,,300.1.1.1/8") == ("bogus", "300.1.1.1/8")


@pytest.mark.parametrize("value", [None, "", "  ,  "])
def test_parse_networks_empty(ct, value):
    assert ct.parse_networks(value) == ()


# ---------------------------------------------------------------------------
# read_forwarded_for (otto M3a)
# ---------------------------------------------------------------------------


def test_read_forwarded_for_joins_repeated_headers_in_order(ct):
    from starlette.datastructures import Headers

    headers = Headers(raw=[
        (b"x-forwarded-for", b"198.51.100.7"),
        (b"host", b"jarvis.example"),
        (b"x-forwarded-for", b"203.0.113.9, 10.244.3.37"),
    ])
    assert ct.read_forwarded_for(headers) == "198.51.100.7, 203.0.113.9, 10.244.3.37"


def test_read_forwarded_for_absent_and_plain_mapping(ct):
    from starlette.datastructures import Headers

    assert ct.read_forwarded_for(Headers(raw=[])) is None
    assert ct.read_forwarded_for({"x-forwarded-for": "192.0.2.10"}) == "192.0.2.10"
    assert ct.read_forwarded_for({}) is None


def test_two_line_xff_walks_as_one_chain(ct, trusted):
    """A caller-supplied first header line can't hide behind the proxy's line."""
    from starlette.datastructures import Headers

    headers = Headers(raw=[
        (b"x-forwarded-for", b"192.0.2.5"),
        (b"x-forwarded-for", b"203.0.113.9"),
    ])
    xff = ct.read_forwarded_for(headers)
    assert _rate(ct, trusted, "10.244.2.240", xff).ip == "203.0.113.9"
    assert str(ct.local_candidate("10.244.2.240", xff, trusted, 1)) == "203.0.113.9"


# ---------------------------------------------------------------------------
# resolve_rate_client: path table (tessa T7, r1 D8, otto M3, xander M1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "peer, xff, cf, trust_cf, expected_ip, expected_source",
    [
        # untrusted peer: headers are never read
        ("198.51.100.7", "192.0.2.5", "192.0.2.5", True, "198.51.100.7", "peer"),
        # trusted peer, single appended hop
        ("10.244.3.7", "192.0.2.55", None, False, "192.0.2.55", "xff"),
        # trusted peer, no XFF
        ("10.244.3.7", None, None, False, "10.244.3.7", "peer"),
        ("10.244.3.7", "   ", None, False, "10.244.3.7", "peer"),
        # every hop trusted, CF not trusted -> peer
        ("10.244.2.240", "10.244.3.37", "203.0.113.9", False, "10.244.2.240", "peer"),
        # every hop trusted, CF trusted -> CF (the tunnel shape)
        ("10.244.2.240", "10.244.3.37", "203.0.113.9", True, "203.0.113.9", "cf"),
        # P2: an untrusted hop exists, so a forged CF is never read
        ("10.244.3.7", "198.51.100.7", "192.0.2.5", True, "198.51.100.7", "xff"),
        # CF present but unparseable -> peer
        ("10.244.2.240", "10.244.3.37", "not-an-ip", True, "10.244.2.240", "peer"),
        # absent peer
        (None, "192.0.2.10", None, False, "unknown", "unknown"),
        ("", "192.0.2.10", None, False, "unknown", "unknown"),
        # unparseable peer is returned verbatim (gateway contract)
        ("not-an-ip", "192.0.2.55", None, False, "not-an-ip", "peer_unparsed"),
        # IPv4-mapped IPv6 peer folds to IPv4 before the trust match
        ("::ffff:10.244.3.7", "192.0.2.10", None, False, "192.0.2.10", "xff"),
        ("10.244.3.7", "::ffff:192.0.2.10", None, False, "192.0.2.10", "xff"),
    ],
)
def test_resolve_rate_client_table(ct, trusted, peer, xff, cf, trust_cf, expected_ip, expected_source):
    resolved = _rate(ct, trusted, peer, xff, cf, trust_cf)
    assert (resolved.ip, resolved.source) == (expected_ip, expected_source)


@pytest.mark.parametrize(
    "xff",
    [
        "192.0.2.5, 203.0.113.9, 10.244.3.37",
        "192.0.2.5, 10.244.9.1, 203.0.113.9",
        "10.0.0.1, 192.0.2.5, 203.0.113.9",
    ],
)
def test_forged_prefix_never_wins(ct, trusted, xff):
    """A left-hand prefix is caller-controlled: neither function returns it."""
    resolved = _rate(ct, trusted, "10.244.2.240", xff)
    assert resolved.ip == "203.0.113.9"
    assert resolved.source == "xff"
    candidate = ct.local_candidate("10.244.2.240", xff, trusted, 1)
    assert candidate is not None
    assert str(candidate) not in {"192.0.2.5", "10.0.0.1"}


def test_unparseable_hop_terminates_walk_to_peer(ct, trusted):
    """otto M3b: garbage in the chain ends the walk; it never becomes the key."""
    resolved = _rate(ct, trusted, "10.244.3.7", "203.0.113.9, garbage, 10.244.9.1")
    assert (resolved.ip, resolved.source) == ("10.244.3.7", "peer")


def test_unparseable_hop_blocks_cf(ct, trusted):
    resolved = _rate(ct, trusted, "10.244.3.7", "garbage, 10.244.9.1", cf="203.0.113.9", trust_cf=True)
    assert (resolved.ip, resolved.source) == ("10.244.3.7", "peer")


def test_overlong_xff_is_tail_parsed_not_absent(ct, trusted):
    """otto M3c / xander M1: padding can't make the header disappear."""
    padding = ", ".join(["192.0.2.5"] * 400)
    xff = f"{padding}, 203.0.113.9, 10.244.3.37"
    assert len(xff) > 2048
    resolved = _rate(ct, trusted, "10.244.2.240", xff)
    # the rightmost untrusted hop is what the nearest proxy appended
    assert (resolved.ip, resolved.source) == ("203.0.113.9", "xff")


def test_more_than_twenty_hops_uses_rightmost_twenty(ct, trusted):
    hops = ["10.244.1.1"] * 25
    hops[2] = "198.51.100.7"  # beyond the 20-entry tail
    xff = ", ".join(hops)
    resolved = _rate(ct, trusted, "10.244.2.240", xff, cf="203.0.113.9", trust_cf=True)
    # every hop in the parsed tail is trusted, but the chain was truncated,
    # so the whole path isn't proven trusted: CF is not read.
    assert (resolved.ip, resolved.source) == ("10.244.2.240", "peer")


def test_overlong_trusted_chain_never_reads_cf(ct, trusted):
    xff = ", ".join(["10.244.1.1"] * 300)
    assert len(xff) > 2048
    resolved = _rate(ct, trusted, "10.244.2.240", xff, cf="203.0.113.9", trust_cf=True)
    assert resolved.source == "peer"


def test_resolve_rate_client_never_raises(ct, trusted):
    for peer, xff, cf in [
        ("10.244.3.7", ",,,", "x"),
        ("10.244.3.7", "\x00\xff", None),
        ("10.244.3.7", "[::1]:443", None),
        (object(), None, None),
    ]:
        resolved = _rate(ct, trusted, peer, xff, cf, True)
        assert isinstance(resolved.ip, str) and resolved.ip


# ---------------------------------------------------------------------------
# local_candidate (D8 L1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "peer, xff, hops, expected",
    [
        # trusted peer: the rightmost entry is the candidate
        ("10.244.2.240", "192.0.2.50", 1, "192.0.2.50"),
        ("10.244.2.240", "192.0.2.5, 192.0.2.50", 1, "192.0.2.50"),
        # untrusted peer: no candidate (direct mode is the caller's concern)
        ("198.51.100.7", "192.0.2.50", 1, None),
        ("192.0.2.50", None, 1, None),
        # no XFF behind a trusted peer: no candidate
        ("10.244.2.240", None, 1, None),
        ("10.244.2.240", "  ", 1, None),
        # unparseable candidate
        ("10.244.2.240", "192.0.2.50, garbage", 1, None),
        # two trusted hops: the second from the right, only if the
        # rightmost is itself a trusted proxy
        ("10.244.2.240", "192.0.2.50, 10.244.3.37", 2, "192.0.2.50"),
        ("10.244.2.240", "192.0.2.50, 203.0.113.9", 2, None),
        # fewer entries than hops
        ("10.244.2.240", "192.0.2.50", 2, None),
        # IPv4-mapped IPv6 normalised
        ("10.244.2.240", "::ffff:192.0.2.50", 1, "192.0.2.50"),
        # IPv6 candidate
        ("10.244.2.240", "2001:db8::5", 1, "2001:db8::5"),
        # unparseable / absent peer
        ("not-an-ip", "192.0.2.50", 1, None),
        (None, "192.0.2.50", 1, None),
    ],
)
def test_local_candidate_table(ct, trusted, peer, xff, hops, expected):
    candidate = ct.local_candidate(peer, xff, trusted, hops)
    assert (None if candidate is None else str(candidate)) == expected


def test_local_candidate_never_reads_cf(ct):
    import inspect

    params = inspect.signature(ct.local_candidate).parameters
    assert not any("cf" in name.lower() for name in params)


def test_local_candidate_tail_parses_overlong_header(ct, trusted):
    xff = ", ".join(["192.0.2.5"] * 400) + ", 203.0.113.9"
    assert str(ct.local_candidate("10.244.2.240", xff, trusted, 1)) == "203.0.113.9"


def test_app_mode_r3_exposure_documented(ct, trusted):
    """codex High 1 / xander H2: R3's documented app-mode exposure.

    A trusted peer carrying a single XFF hop makes that hop both the rate
    key and the home candidate. That's correct only while R3 holds (every
    trusted-range peer is a proxy that appends what it saw). On a flat pod
    network any pod can send this exact request, which is why thor runs
    edge-attested mode, where the candidate only corroborates. Fix the
    deployment, not this test.
    """
    resolved = _rate(ct, trusted, "10.244.4.9", "192.0.2.5")
    assert (resolved.ip, resolved.source) == ("192.0.2.5", "xff")
    assert str(ct.local_candidate("10.244.4.9", "192.0.2.5", trusted, 1)) == "192.0.2.5"


# ---------------------------------------------------------------------------
# rate_limit_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip, expected",
    [
        ("192.0.2.10", "192.0.2.10"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("::ffff:192.0.2.10", "192.0.2.10"),
        ("unknown", "unknown"),
        ("not-an-ip", "not-an-ip"),
    ],
)
def test_rate_limit_key(ct, ip, expected):
    assert ct.rate_limit_key(ip) == expected


def test_ipv6_folds_to_64(ct):
    assert ct.rate_limit_key("2001:db8:0:1::1") == ct.rate_limit_key("2001:db8:0:1:ffff:ffff:ffff:ffff")
    assert ct.rate_limit_key("2001:db8:0:1::1") != ct.rate_limit_key("2001:db8:0:2::1")


def test_parse_ip_normalises_mapped(ct):
    assert ct.parse_ip("::ffff:192.0.2.1") == ipaddress.ip_address("192.0.2.1")
    assert ct.parse_ip(" 192.0.2.1 ") == ipaddress.ip_address("192.0.2.1")
    assert ct.parse_ip("nope") is None
    assert ct.parse_ip(None) is None


# ---------------------------------------------------------------------------
# SlidingWindowLimiter (injected clock)
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_window(ct):
    clock = _Clock()
    limiter = ct.SlidingWindowLimiter(per_minute=20, max_keys=100, clock=clock)

    async def run():
        results = [await limiter.allow("a") for _ in range(20)]
        assert all(results)
        assert await limiter.allow("a") is False  # the 21st
        clock.now += 59.9
        assert await limiter.allow("a") is False
        clock.now += 0.2  # t = 60.1
        assert await limiter.allow("a") is True
        assert await limiter.allow("b") is True  # independent key

    asyncio.run(run())


def test_limiter_evicts_least_recent_key(ct):
    clock = _Clock()
    limiter = ct.SlidingWindowLimiter(per_minute=1, max_keys=2, clock=clock)

    async def run():
        assert await limiter.allow("a")
        assert await limiter.allow("b")
        assert await limiter.allow("c")  # evicts "a"
        assert len(limiter) == 2
        assert await limiter.allow("a")  # fresh window after eviction

    asyncio.run(run())


def test_gateway_limiter_is_the_shared_limiter(ct):
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    limiter_module = importlib.import_module("gateway.conversation_limiter")
    assert limiter_module.NewConversationLimiter is ct.SlidingWindowLimiter
    assert not hasattr(limiter_module, "_parse_trusted_networks")
    assert not hasattr(limiter_module, "_is_trusted_hop")


def test_gateway_unparseable_hop_falls_back_to_peer(ct):
    """Deliberate gateway delta: a garbage hop is no longer the key."""
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    limiter_module = importlib.import_module("gateway.conversation_limiter")
    assert limiter_module.resolve_client_key("10.244.3.7", "garbage", TRUSTED) == "10.244.3.7"


# ---------------------------------------------------------------------------
# Packaging: stdlib-only, shims, Dockerfile
# ---------------------------------------------------------------------------


def test_module_is_stdlib_only(ct):
    tree = ast.parse(MODULE_PATH.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would break the single-file COPY"
            imported.add((node.module or "").split(".")[0])
    imported.discard("__future__")
    assert imported, "floor: the module imports something"
    assert "ipaddress" in imported
    non_stdlib = sorted(name for name in imported if name not in sys.stdlib_module_names)
    assert non_stdlib == []


@pytest.mark.parametrize(
    "shim",
    ["apps/jarvis-web/backend/client_throttle.py", "apps/chat-embed/client_throttle.py"],
)
def test_local_dev_shim_reexports_shared_module(ct, shim):
    path = REPO_ROOT / shim
    source = path.read_text()
    assert "DO NOT add" in source
    spec = importlib.util.spec_from_file_location(f"_shim_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("resolve_rate_client", "local_candidate", "SlidingWindowLimiter", "read_forwarded_for", "rate_limit_key", "parse_networks"):
        assert getattr(module, name) is getattr(ct, name)
    tree = ast.parse(source)
    assert not [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def test_jarvis_web_dockerfile_copies_module_after_backend(ct):
    lines = (REPO_ROOT / "apps" / "jarvis-web" / "Dockerfile").read_text().splitlines()
    backend = next(i for i, line in enumerate(lines) if line.strip().startswith("COPY apps/jarvis-web/backend/"))
    shared = [
        i for i, line in enumerate(lines)
        if line.strip() == "COPY src/shared/client_throttle.py /app/backend/client_throttle.py"
    ]
    assert len(shared) == 1
    assert shared[0] > backend, "the shared module must overwrite the local-dev shim"


# ---------------------------------------------------------------------------
# Fix round: zone ids, window boundary, refused hits, mid-address cut,
# gateway joins header lines and folds IPv6 to /64
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["fe80::1%eth0", "fe80::1%1", "2001:db8::5%en0"])
def test_parse_ip_rejects_zone_ids(ct, trusted, value):
    assert ct.parse_ip(value) is None
    assert ct.local_candidate("10.244.2.240", value, trusted, 1) is None
    resolved = _rate(ct, trusted, "10.244.2.240", f"203.0.113.9, {value}")
    assert (resolved.ip, resolved.source) == ("10.244.2.240", "peer")


def test_limiter_window_boundary_exactly_sixty(ct):
    clock = _Clock()
    limiter = ct.SlidingWindowLimiter(per_minute=1, max_keys=10, clock=clock)

    async def run():
        assert await limiter.allow("a") is True
        clock.now += 60.0  # a hit exactly 60 s old is outside the window
        assert await limiter.allow("a") is True

    asyncio.run(run())


def test_limiter_refused_hits_not_recorded(ct):
    clock = _Clock()
    limiter = ct.SlidingWindowLimiter(per_minute=20, max_keys=10, clock=clock)

    async def run():
        for _ in range(20):
            assert await limiter.allow("a")
        clock.now += 30
        for _ in range(5):
            assert await limiter.allow("a") is False
        clock.now += 30.5  # the first 20 expired; the 5 refused never counted
        results = [await limiter.allow("a") for _ in range(20)]
        assert results == [True] * 20

    asyncio.run(run())


def test_overlong_cut_mid_address_drops_the_fragment(ct, trusted):
    """The 2 KB cut lands inside '198.51.100.77', leaving '8.51.100.77',
    itself a valid address. It must be discarded, never walked."""
    prefix = "198.51.100.77"
    hops = ["10.244.1.1"] * 19
    base = ", ".join([prefix] + hops)
    # pad the trusted hops with spaces (stripped when parsed) until the cut
    # lands two characters into the prefix
    pad = ct.MAX_FORWARDED_FOR_BYTES + 2 - len(base)
    assert pad > 0
    hops[0] = hops[0] + " " * pad
    xff = ", ".join([prefix] + hops)
    assert len(xff) - ct.MAX_FORWARDED_FOR_BYTES == 2
    assert xff[len(xff) - ct.MAX_FORWARDED_FOR_BYTES:].startswith("8.51.100.77")
    resolved = _rate(ct, trusted, "10.244.2.240", xff)
    assert resolved.ip != "8.51.100.77"
    assert (resolved.ip, resolved.source) == ("10.244.2.240", "peer")


def test_gateway_key_folds_ipv6_to_64(ct):
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    limiter_module = importlib.import_module("gateway.conversation_limiter")
    a = limiter_module.resolve_client_key("10.244.3.7", "2001:db8:1:2::1", TRUSTED)
    b = limiter_module.resolve_client_key("10.244.3.7", "2001:db8:1:2:ffff::9", TRUSTED)
    assert a == b == "2001:db8:1:2::/64"
    assert limiter_module.resolve_client_key("10.244.3.7", "192.0.2.55", TRUSTED) == "192.0.2.55"


# ---------------------------------------------------------------------------
# Relay session ids (codex High): bound to the visitor under the relay key
# ---------------------------------------------------------------------------

RELAY = "relay-key-for-tests-0123456789abcdef"


def test_relay_session_id_valid_only_for_its_visitor_and_key():
    from shared.client_throttle import mint_relay_session_id, rate_limit_key, relay_session_id_valid

    visitor = rate_limit_key("203.0.113.5")
    sid = mint_relay_session_id(RELAY, visitor)
    assert sid.startswith("pub-") and len(sid) == 4 + 32 + 1 + 24
    assert relay_session_id_valid(sid, RELAY, visitor)
    assert not relay_session_id_valid(sid, RELAY, rate_limit_key("203.0.113.6"))
    assert not relay_session_id_valid(sid, RELAY + "x", visitor)
    assert not relay_session_id_valid(sid, "", visitor)
    part, mac = sid.rsplit(".", 1)
    assert not relay_session_id_valid(f"{part}.{'0' * 24}", RELAY, visitor)
    assert not relay_session_id_valid(sid.upper(), RELAY, visitor)
    assert not relay_session_id_valid(None, RELAY, visitor)
