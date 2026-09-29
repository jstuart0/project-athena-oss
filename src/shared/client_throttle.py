"""Client-address resolution and a per-key sliding-window limiter.

One parser for the forwarded-for rules shared by the gateway, jarvis-web and
chat-embed. Stdlib-only and never raises, because jarvis-web and chat-embed
COPY this single file into their images (see their Dockerfiles) and call it
on every request.

Two questions, two functions, deliberately different answers:

- ``resolve_rate_client``: whose budget does this request spend? A deep
  right-to-left walk over ``X-Forwarded-For`` returning the nearest hop
  outside the trusted proxies. A caller can prepend anything it likes, so
  the walk never reads past the first untrusted hop.
- ``local_candidate``: which one address may be tested against the home
  networks? Only the hop the immediately trusted proxy appended. Never a
  deeper hop and never ``Cf-Connecting-Ip``, so a home verdict can't be
  borrowed from anything the caller wrote.

Hides: header joining, the length and hop caps, the walk, the Cloudflare
preconditions, IPv4-mapped IPv6 folding, the /64 rate key, the window, the
clock and LRU eviction.

It also owns the relay's public session ids (``mint_relay_session_id`` /
``relay_session_id_valid``): chat-embed and jarvis-web both hold the relay
key and both derive the same visitor key from ``rate_limit_key``, so each
side can check that a presented ``pub-`` id was minted for this visitor.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time
import uuid
from collections import OrderedDict
from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple, Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

MAX_FORWARDED_FOR_BYTES = 2048
MAX_FORWARDED_FOR_HOPS = 20
IPV6_RATE_PREFIX = 64


class ResolvedClient(NamedTuple):
    """``source`` is one of ``peer``, ``xff``, ``cf``, ``peer_unparsed``, ``unknown``."""

    ip: str
    source: str


def parse_ip(value: object) -> Optional[IPAddress]:
    """Parse one address, folding IPv4-mapped IPv6 to IPv4. None if it
    isn't one, or if it carries a zone id ("fe80::1%eth0"): a zone makes
    the same address compare unequal and never belongs in a forwarded hop."""
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _split_entries(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def parse_networks(value: Optional[str]) -> Tuple[IPNetwork, ...]:
    """Comma-separated CIDRs or single addresses; invalid entries are dropped.

    Use ``invalid_network_entries`` to report what was dropped.
    """
    networks = []
    for entry in _split_entries(value):
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def invalid_network_entries(value: Optional[str]) -> Tuple[str, ...]:
    invalid = []
    for entry in _split_entries(value):
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            invalid.append(entry)
    return tuple(invalid)


def in_networks(addr: Optional[IPAddress], networks: Sequence[IPNetwork]) -> bool:
    if addr is None:
        return False
    return any(addr.version == net.version and addr in net for net in networks)


def read_forwarded_for(headers) -> Optional[str]:
    """Every ``X-Forwarded-For`` line joined in order, or None.

    A proxy that sees a second header line appends its own value to a new
    line; reading only the first would let the caller's line stand in for
    the proxy's.
    """
    try:
        getlist = getattr(headers, "getlist", None)
        if getlist is not None:
            values = [v for v in getlist("x-forwarded-for") if v and v.strip()]
        else:
            value = None
            for key, candidate in dict(headers).items():
                if str(key).lower() == "x-forwarded-for":
                    value = candidate
            values = [value] if value and str(value).strip() else []
    except Exception:
        return None
    if not values:
        return None
    return ", ".join(str(v).strip() for v in values)


def _tail_entries(forwarded_for: Optional[str]) -> Tuple[List[str], bool]:
    """The rightmost entries within the caps, plus whether anything was cut.

    Over-length input is tail-parsed, never treated as absent: padding a
    header must not make the proxy-appended hops disappear.
    """
    if not isinstance(forwarded_for, str):
        return [], False
    truncated = False
    text = forwarded_for
    if len(text) > MAX_FORWARDED_FOR_BYTES:
        text = text[-MAX_FORWARDED_FOR_BYTES:]
        truncated = True
        # the first fragment may be a partial address
        comma = text.find(",")
        text = text[comma + 1:] if comma >= 0 else ""
    entries = _split_entries(text)
    if len(entries) > MAX_FORWARDED_FOR_HOPS:
        entries = entries[-MAX_FORWARDED_FOR_HOPS:]
        truncated = True
    return entries, truncated


def resolve_rate_client(
    peer: object,
    forwarded_for: Optional[str],
    cf_connecting_ip: Optional[str],
    trusted: Sequence[IPNetwork],
    trust_cf: bool,
) -> ResolvedClient:
    """Whose rate budget this request spends.

    - No peer → ``unknown``. An unparseable peer is returned verbatim.
    - A peer outside ``trusted`` → the peer; headers are never read.
    - Otherwise walk ``X-Forwarded-For`` right to left and return the first
      hop outside ``trusted``. An unparseable hop ends the walk at the peer.
    - Only when XFF is present, complete and every hop is trusted, and
      ``trust_cf`` is set, is ``Cf-Connecting-Ip`` read.
    """
    if not peer:
        return ResolvedClient("unknown", "unknown")
    peer_addr = parse_ip(peer)
    if peer_addr is None:
        return ResolvedClient(str(peer), "peer_unparsed")
    peer_ip = str(peer_addr)
    if not in_networks(peer_addr, trusted):
        return ResolvedClient(peer_ip, "peer")

    hops, truncated = _tail_entries(forwarded_for)
    if not hops:
        return ResolvedClient(peer_ip, "peer")
    for hop in reversed(hops):
        hop_addr = parse_ip(hop)
        if hop_addr is None:
            return ResolvedClient(peer_ip, "peer")
        if not in_networks(hop_addr, trusted):
            return ResolvedClient(str(hop_addr), "xff")

    if trust_cf and not truncated:
        cf_addr = parse_ip(cf_connecting_ip)
        if cf_addr is not None:
            return ResolvedClient(str(cf_addr), "cf")
    return ResolvedClient(peer_ip, "peer")


def local_candidate(
    peer: object,
    forwarded_for: Optional[str],
    trusted: Sequence[IPNetwork],
    trusted_hops: int = 1,
) -> Optional[IPAddress]:
    """The single address a home-network rule may test, or None.

    Only a peer inside ``trusted`` yields a candidate: the ``trusted_hops``-th
    entry from the right of ``X-Forwarded-For``, the hop the nearest trusted
    proxy appended. With more than one hop, every entry to its right must
    itself be a trusted proxy. Direct-client deployments use the peer
    themselves; that choice belongs to the caller, not this function.
    """
    peer_addr = parse_ip(peer)
    if peer_addr is None or not in_networks(peer_addr, trusted):
        return None
    try:
        hops_count = int(trusted_hops)
    except (TypeError, ValueError):
        return None
    if hops_count < 1:
        return None
    entries, _ = _tail_entries(forwarded_for)
    if len(entries) < hops_count:
        return None
    for proxy_entry in entries[len(entries) - hops_count + 1:]:
        if not in_networks(parse_ip(proxy_entry), trusted):
            return None
    return parse_ip(entries[-hops_count])


def rate_limit_key(ip: str) -> str:
    """IPv4 → the address; IPv6 → its /64, so one visitor can't rotate
    through their own prefix. Anything else is returned unchanged."""
    addr = parse_ip(ip)
    if addr is None:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{addr}/{IPV6_RATE_PREFIX}", strict=False))
    return str(addr)


RELAY_SESSION_PREFIX = "pub-"
_RELAY_SESSION_SHAPE = re.compile(r"^pub-([0-9a-f]{32})\.([0-9a-f]{24})$")


def _relay_session_mac(relay_key: str, part: str, visitor_key: str) -> str:
    derived = hmac.new(relay_key.encode("utf-8"), b"jarvis-relay-session", hashlib.sha256).digest()
    return hmac.new(derived, f"{part}|{visitor_key}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def mint_relay_session_id(relay_key: str, visitor_key: str) -> str:
    """A new public session id, ``pub-<32 hex>.<24 hex mac>``, bound to
    ``visitor_key`` (a ``rate_limit_key``) under the relay key."""
    part = uuid.uuid4().hex
    return f"{RELAY_SESSION_PREFIX}{part}.{_relay_session_mac(relay_key, part, visitor_key)}"


def relay_session_id_valid(session_id: object, relay_key: str, visitor_key: str) -> bool:
    """True only for an id ``mint_relay_session_id`` made for this visitor
    under this relay key."""
    if not relay_key or not isinstance(session_id, str):
        return False
    match = _RELAY_SESSION_SHAPE.match(session_id)
    if not match:
        return False
    part, mac = match.groups()
    return hmac.compare_digest(mac, _relay_session_mac(relay_key, part, visitor_key))


class SlidingWindowLimiter:
    """Sliding 60s per-key window, in-memory, LRU-bounded on distinct keys.

    Per process: N replicas each enforce their own window. ``allow`` never
    awaits, so it's safe on a single event loop without a lock.
    """

    def __init__(
        self,
        per_minute: int,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_minute = per_minute
        self._max_keys = max_keys
        self._clock = clock
        self._windows: "OrderedDict[str, List[float]]" = OrderedDict()

    async def allow(self, key: str) -> bool:
        """Return True and record a hit if ``key`` is under budget this minute."""
        now = self._clock()
        window_start = now - 60.0
        is_new_key = key not in self._windows

        timestamps = [t for t in self._windows.get(key, []) if t > window_start]
        allowed = len(timestamps) < self._per_minute
        if allowed:
            timestamps.append(now)

        self._windows[key] = timestamps
        self._windows.move_to_end(key)

        if is_new_key and len(self._windows) > self._max_keys:
            self._windows.popitem(last=False)

        return allowed

    def __len__(self) -> int:
        return len(self._windows)
