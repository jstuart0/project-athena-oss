"""jarvis-web caller resolution: who is asking, and what may they do.

jarvis-web serves a browser without sign-in only when the request provably
comes from the home network, and requires sign-in for everything else.
Every request resolves to one caller class, from server-side evidence
only (nothing in the body counts):

  web_authenticated  a signed-in owner/operator (Bearer checked against
                     admin-backend's /api/auth/me)
  web_local          the household network (D8: the hop the trusted proxy
                     appended, or the TCP peer in direct-client mode, is in
                     JARVIS_LOCAL_NETWORKS and the Host is allowlisted)
  web_guest_net      the rental guest network (JARVIS_GUEST_NETWORKS):
                     UI and chat, mode always "guest", guest reads only
  service            a valid X-Service-Key, household-read routes only
  web_public         none of the above: 401, no upstream call

Edge mode (JARVIS_EDGE_ATTESTATION_SECRET set): an auth proxy in front of
jarvis-web (Traefik + forwardAuth, see
manifests/athena-prod/optional/jarvis-web-edge-auth.yaml) classifies each
request as home, guest or authenticated and attests it with a shared
secret. The attested class is honoured only from a trusted proxy peer, and
home/guest only when the D8 candidate and Host corroborate it; the network
alone never grants home in edge mode.

Evidence order: edge verdict, Bearer, service (allow_service routes only),
local (app mode only), public.

Hides: every setting and its startup validation, the network rules
(candidate, exclusions, gateways, Host allowlist, Cloudflare headers), the
Bearer check with its LRU cache and per-IP attempt budget, the
service-key comparison, and the CSRF header rule. main.py only sees
Caller and the require_* dependencies.

Dependency category: remote-but-owned (admin-backend /api/auth/me), with
two adapters: httpx in production, an injectable async callable in tests.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import socket
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import httpx
import structlog
from fastapi import HTTPException, Request, WebSocket

from admin_url import get_admin_url
import client_throttle as throttle

logger = structlog.get_logger()

_AUTH_CACHE_TTL_SECONDS = 60
_AUTH_CACHE_MAX_ENTRIES = 10_000
_AUTH_ME_TIMEOUT_SECONDS = 3.0
_AUTH_ATTEMPTS_PER_MINUTE = 10
_PERMITTED_ROLES = frozenset({"owner", "operator"})
_PLACEHOLDER_SERVICE_KEY = "dev-service-key-change-in-production"
_CSRF_HEADER = "x-jarvis-request"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_ROUTE_FILE = "/proc/net/route"
_DEFAULT_ROUTE6_FILE = "/proc/net/ipv6_route"
_MIN_EDGE_SECRET_LENGTH = 32
_EDGE_CLASS_HEADER = "X-Jarvis-Edge-Class"
_EDGE_ATTESTATION_HEADER = "X-Jarvis-Edge-Attestation"
_DEFAULT_IDENTITY_HEADER = "X-authentik-username"
_DEFAULT_GROUPS_HEADER = "X-authentik-groups"
_EDGE_CLASSES = frozenset({"home", "guest", "authenticated"})

# Every header the edge must strip from inbound requests before it sets its
# own (the template's named strip list is pinned to be a superset of this).
# Traefik strips by exact name only.
EDGE_STRIPPED_HEADERS = (
    _EDGE_CLASS_HEADER,
    _EDGE_ATTESTATION_HEADER,
    "X-authentik-username",
    "X-authentik-groups",
    "X-authentik-email",
    "X-authentik-name",
    "X-authentik-uid",
    "X-Service-Key",
    "X-Jarvis-Relay-Key",
    "X-Jarvis-Relay-Client",
)

CLASS_AUTHENTICATED = "web_authenticated"
CLASS_LOCAL = "web_local"
CLASS_GUEST_NET = "web_guest_net"
CLASS_SERVICE = "service"
CLASS_PUBLIC = "web_public"
CLASS_NOT_HOUSEHOLD = "not_household"  # signed in at the edge, not in a household group

BROWSER_CLASSES = frozenset({CLASS_AUTHENTICATED, CLASS_LOCAL, CLASS_GUEST_NET})

# The caller_trust value each class sends to the orchestrator. The guest
# network is a home-network browser that is never PIN-trusted.
UPSTREAM_TRUST = {
    CLASS_AUTHENTICATED: "web_authenticated",
    CLASS_LOCAL: "web_local",
    CLASS_GUEST_NET: "web_local",
    CLASS_PUBLIC: "web_public",
}

HouseholdModeResolver = Callable[[], Awaitable[str]]
AuthMeCallable = Callable[[str], Awaitable[httpx.Response]]
Network = throttle.IPNetwork


# ---------------------------------------------------------------------------
# Settings (read once; the one reader for every jarvis-web auth variable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthSettings:
    service_api_key: str = ""
    trusted_proxies: Tuple[Network, ...] = ()
    local_networks: Tuple[Network, ...] = ()
    guest_networks: Tuple[Network, ...] = ()
    local_exclude: Tuple[Network, ...] = ()
    trusted_hops: int = 1
    direct_clients: bool = False
    gateways: Tuple[object, ...] = ()
    allowed_hosts: frozenset = frozenset()
    trust_cf: bool = False
    cors_origins: Tuple[str, ...] = ()
    login_url: str = ""
    logout_url: str = ""
    signin_url: str = ""
    local_enabled: bool = False
    edge_current: str = field(default="", repr=False)
    edge_previous: str = field(default="", repr=False)
    identity_header: str = _DEFAULT_IDENTITY_HEADER
    groups_header: str = _DEFAULT_GROUPS_HEADER
    groups_separator: str = "|"
    household_groups: frozenset = frozenset()

    @property
    def edge_mode(self) -> bool:
        return bool(self.edge_current)

    @property
    def any_browser_access(self) -> bool:
        return self.local_enabled or self.edge_mode


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: Optional[str]) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _networks(env: Mapping[str, str], name: str) -> Tuple[Network, ...]:
    raw = env.get(name, "")
    for bad in throttle.invalid_network_entries(raw):
        logger.error("jarvis_invalid_network_entry", setting=name, entry=bad)
    return throttle.parse_networks(raw)


def normalize_host(value: Optional[str]) -> Optional[str]:
    """Host header -> lowercase name without port or trailing dot."""
    if not value:
        return None
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end > 0 else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    host = host.rstrip(".")
    return host or None


def _covered(addr, networks: Iterable[Network]) -> bool:
    return throttle.in_networks(addr, tuple(networks))


def _own_ips() -> Tuple[object, ...]:
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = throttle.parse_ip(info[4][0])
            if addr is not None:
                found.append(addr)
    except OSError:
        pass
    return tuple(dict.fromkeys(found))


def _hex_ipv4(value: str):
    try:
        return ipaddress.IPv4Address(int(value, 16).to_bytes(4, "little"))
    except (ValueError, OverflowError):
        return None


def read_default_gateways(route_file: str, route6_file: str) -> Optional[Tuple[object, ...]]:
    """Default gateways from /proc/net/route (+ ipv6_route, best effort).
    None when the IPv4 table can't be read."""
    gateways = []
    try:
        with open(route_file, "r", encoding="ascii") as fh:
            lines = fh.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        cols = line.split()
        if len(cols) >= 3 and cols[1] == "00000000":
            gw = _hex_ipv4(cols[2])
            if gw is not None and int(gw) != 0:
                gateways.append(gw)
    try:
        with open(route6_file, "r", encoding="ascii") as fh:
            for line in fh.read().splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[0] == "0" * 32 and cols[1] == "00" and cols[4] != "0" * 32:
                    gateways.append(ipaddress.IPv6Address(bytes.fromhex(cols[4])))
    except (OSError, ValueError):
        pass
    return tuple(dict.fromkeys(gateways))


def _filter_home_entries(
    name: str,
    entries: Tuple[Network, ...],
    trusted: Tuple[Network, ...],
    exclude: Tuple[Network, ...],
    own_ips: Tuple[object, ...],
    gateways: Tuple[object, ...],
) -> Tuple[Network, ...]:
    """D8 L3 (+A3/A4): drop entries that would let a proxy, this pod or a
    NAT gateway read as home."""
    kept = []
    for entry in entries:
        own = [ip for ip in own_ips if _covered(ip, (entry,)) and not _covered(ip, exclude)]
        if own:
            logger.error("jarvis_local_network_contains_own_ip", setting=name, entry=str(entry))
            continue
        gws = [gw for gw in gateways if _covered(gw, (entry,)) and not _covered(gw, exclude)]
        if gws:
            logger.error("jarvis_local_network_contains_gateway", setting=name, entry=str(entry))
            continue
        overlapping = [t for t in trusted if t.version == entry.version and entry.supernet_of(t)]
        if overlapping:
            logger.error("jarvis_local_network_covers_trusted_proxy", setting=name, entry=str(entry))
            continue
        inside = [t for t in trusted if t.version == entry.version and entry.subnet_of(t)]
        if inside and entry.num_addresses != 1:
            logger.error("jarvis_local_network_inside_trusted_proxy", setting=name, entry=str(entry))
            continue
        kept.append(entry)
    return tuple(kept)


_PLACEHOLDER_PREFIXES = ("configure_me", "your_")


def _edge_secret_fault(value: str, others: Dict[str, str]) -> Optional[str]:
    """Why an attestation value can't be used, or None. Never returns or
    logs the value itself."""
    lowered = value.strip().lower()
    if lowered.startswith(_PLACEHOLDER_PREFIXES) or lowered == "changeme":
        return "placeholder"
    if len(value) < _MIN_EDGE_SECRET_LENGTH:
        return "too_short"
    for name, other in others.items():
        if other and hmac.compare_digest(value.encode("utf-8"), other.encode("utf-8")):
            return f"equals_{name}"
    return None


def _edge_settings(env: Mapping[str, str], service_key: str) -> Tuple[str, str]:
    """(current, previous) attestation values. A5: any fault is fatal, so a
    rolling deploy stalls the new pod instead of serving 401 to everyone."""
    current = env.get("JARVIS_EDGE_ATTESTATION_SECRET", "")
    previous = env.get("JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS", "")
    relay_key = env.get("JARVIS_RELAY_KEY", "")
    if previous and not current:
        logger.error("jarvis_edge_attestation_rejected", which="previous", reason="previous_without_current")
        raise SystemExit("JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS is set without JARVIS_EDGE_ATTESTATION_SECRET")
    for which, value, others in (
        ("current", current, {"service_key": service_key, "relay_key": relay_key, "previous": previous}),
        ("previous", previous, {"service_key": service_key, "relay_key": relay_key, "current": current}),
    ):
        if not value:
            continue
        fault = _edge_secret_fault(value, others)
        if fault:
            logger.error("jarvis_edge_attestation_rejected", which=which, reason=fault)
            raise SystemExit(f"jarvis-web edge attestation ({which}) rejected: {fault}")
    return current, previous


def load_settings(
    env: Mapping[str, str],
    *,
    own_ips: Optional[Tuple[object, ...]] = None,
    route_file: str = _DEFAULT_ROUTE_FILE,
    route6_file: str = _DEFAULT_ROUTE6_FILE,
) -> AuthSettings:
    """Build and validate the settings. Raises SystemExit for a posture
    that can't be served safely; logs ERROR and disables the affected
    feature for anything else."""
    public_mode = env.get("JARVIS_PUBLIC_MODE", "").strip().lower()
    if public_mode == "household":
        logger.error(
            "jarvis_public_mode_removed",
            hint="set JARVIS_LOCAL_NETWORKS (behind a proxy) or JARVIS_DIRECT_CLIENTS=true; see docs/CONFIGURATION.md",
        )
        raise SystemExit("JARVIS_PUBLIC_MODE=household was removed; configure home networks instead")
    if public_mode:
        logger.warning("jarvis_public_mode_deprecated", value=public_mode, effect="ignored")

    service_key = env.get("SERVICE_API_KEY", "")
    edge_current, edge_previous = _edge_settings(env, service_key)
    trusted = _networks(env, "TRUSTED_PROXY_CIDRS")
    local = _networks(env, "JARVIS_LOCAL_NETWORKS")
    guest = _networks(env, "JARVIS_GUEST_NETWORKS")
    exclude = _networks(env, "JARVIS_LOCAL_EXCLUDE")
    direct = _flag(env.get("JARVIS_DIRECT_CLIENTS"))
    allowed_hosts = frozenset(h for h in (normalize_host(v) for v in _csv(env.get("JARVIS_ALLOWED_HOSTS"))) if h)
    try:
        hops = max(1, int(env.get("JARVIS_LOCAL_TRUSTED_HOPS", "1") or "1"))
    except ValueError:
        logger.error("jarvis_local_trusted_hops_invalid", value=env.get("JARVIS_LOCAL_TRUSTED_HOPS"))
        hops = 1

    if direct and edge_current:
        logger.error("jarvis_direct_clients_with_edge_mode")
        raise SystemExit("JARVIS_DIRECT_CLIENTS can't be combined with edge attestation")
    if edge_current and not trusted:
        logger.error("jarvis_edge_without_trusted_proxy", effect="no request can be attested; every browser gets 401")
    if edge_current and not (local and allowed_hosts):
        logger.error(
            "jarvis_edge_home_disabled",
            reason="JARVIS_LOCAL_NETWORKS and JARVIS_ALLOWED_HOSTS are required to corroborate an attested home",
            effect="every browser must sign in",
        )

    household_groups = frozenset(_csv(env.get("JARVIS_HOUSEHOLD_GROUPS")))
    if edge_current and not household_groups:
        logger.error("jarvis_household_groups_empty", effect="no signed-in identity is honoured")

    if direct and env.get("KUBERNETES_SERVICE_HOST") and not _flag(env.get("JARVIS_DIRECT_CLIENTS_ACK_SOURCE_PRESERVED")):
        logger.error(
            "jarvis_direct_clients_in_kubernetes",
            hint="behind a Service with externalTrafficPolicy Cluster or SNAT every internet caller looks like a node; "
                 "set JARVIS_DIRECT_CLIENTS_ACK_SOURCE_PRESERVED=true only with ETP Local",
        )
        raise SystemExit("JARVIS_DIRECT_CLIENTS in Kubernetes needs JARVIS_DIRECT_CLIENTS_ACK_SOURCE_PRESERVED=true")

    wants_home = bool(local or guest)
    local_enabled = wants_home
    gateways: Tuple[object, ...] = ()
    if wants_home:
        if direct and trusted:
            logger.error("jarvis_direct_clients_with_trusted_proxies", effect="home networks disabled")
            local_enabled = False
        elif not direct and not trusted:
            logger.error("jarvis_local_without_trusted_proxy", effect="home networks disabled")
            local_enabled = False
        if not allowed_hosts:
            logger.error("jarvis_local_without_allowed_hosts", effect="home networks disabled")
            local_enabled = False
        if direct and local_enabled:
            read = read_default_gateways(route_file, route6_file)
            if read is None:
                logger.error("jarvis_direct_clients_route_table_unreadable", effect="home networks disabled")
                local_enabled = False
            else:
                gateways = read

    own = _own_ips() if own_ips is None else tuple(own_ips)
    if local_enabled:
        local = _filter_home_entries("JARVIS_LOCAL_NETWORKS", local, trusted, exclude, own, gateways)
        guest = _filter_home_entries("JARVIS_GUEST_NETWORKS", guest, trusted, exclude, own, gateways)
        overlaps = [str(g) for g in guest for n in local if g.version == n.version and g.overlaps(n)]
        if overlaps:
            logger.warning("jarvis_guest_network_overlaps_local", entries=overlaps, effect="guest wins")
        local_enabled = bool(local or guest)
    else:
        local, guest = (), ()

    cors = []
    for origin in _csv(env.get("JARVIS_CORS_ORIGINS")):
        if origin in {"*", "null"}:
            logger.error("jarvis_cors_origin_refused", origin=origin)
            continue
        cors.append(origin.rstrip("/"))

    settings = AuthSettings(
        service_api_key=service_key,
        trusted_proxies=trusted,
        local_networks=local,
        guest_networks=guest,
        local_exclude=exclude,
        trusted_hops=hops,
        direct_clients=direct,
        gateways=gateways,
        allowed_hosts=allowed_hosts,
        trust_cf=_flag(env.get("TRUST_CF_CONNECTING_IP")),
        cors_origins=tuple(cors),
        login_url=env.get("JARVIS_LOGIN_URL", "").strip(),
        logout_url=env.get("JARVIS_LOGOUT_URL", "").strip(),
        signin_url=env.get("JARVIS_SIGNIN_URL", "").strip(),
        local_enabled=local_enabled,
        edge_current=edge_current,
        edge_previous=edge_previous,
        identity_header=env.get("JARVIS_EDGE_IDENTITY_HEADER", "").strip() or _DEFAULT_IDENTITY_HEADER,
        groups_header=env.get("JARVIS_EDGE_GROUPS_HEADER", "").strip() or _DEFAULT_GROUPS_HEADER,
        groups_separator=env.get("JARVIS_EDGE_GROUPS_SEPARATOR", "") or "|",
        household_groups=household_groups,
    )
    if not settings.any_browser_access:
        logger.error(
            "jarvis_no_browser_access_configured",
            effect="every browser caller gets 401",
            hint="set JARVIS_LOCAL_NETWORKS or an auth proxy: see docs/CONFIGURATION.md",
        )
    logger.info(
        "jarvis_caller_posture",
        local_networks=len(settings.local_networks),
        guest_networks=len(settings.guest_networks),
        excluded=len(settings.local_exclude),
        trusted_proxies=len(settings.trusted_proxies),
        direct_clients=settings.direct_clients,
        allowed_hosts=len(settings.allowed_hosts),
        cors_origins=len(settings.cors_origins),
        edge_mode=settings.edge_mode,
        edge_previous_set=bool(settings.edge_previous),
        household_groups=len(settings.household_groups),
    )
    return settings


SETTINGS: AuthSettings = load_settings(os.environ)


def settings() -> AuthSettings:
    return SETTINGS


# ---------------------------------------------------------------------------
# Caller
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Caller:
    caller_class: str
    mode: str  # "owner" or "guest"
    authenticated: bool = False
    role: Optional[str] = None
    reason: str = ""
    source: str = "none"  # edge | bearer | local | guest_network | service | none
    matched_network: Optional[str] = None
    identity: Optional[str] = None
    edge_attestation: str = "none"  # current | previous | none

    @property
    def trust(self) -> str:
        """The caller_trust value sent to the orchestrator."""
        return UPSTREAM_TRUST.get(self.caller_class, "web_public")

    @property
    def is_browser(self) -> bool:
        return self.caller_class in BROWSER_CLASSES

    @property
    def can_read_household(self) -> bool:
        return self.caller_class in {CLASS_AUTHENTICATED, CLASS_LOCAL, CLASS_SERVICE}

    @property
    def gets_guest_context(self) -> bool:
        """Chat context may carry the current guest's identity."""
        return self.caller_class in {CLASS_AUTHENTICATED, CLASS_LOCAL, CLASS_GUEST_NET}

    @property
    def owner_permitted(self) -> bool:
        if self.caller_class == CLASS_AUTHENTICATED:
            return True
        return self.caller_class == CLASS_LOCAL and self.mode == "owner"

    @property
    def control_reason(self) -> Optional[str]:
        if self.owner_permitted:
            return None
        if self.caller_class == CLASS_LOCAL:
            return "guest_stay"
        if self.caller_class == CLASS_GUEST_NET:
            return "guest_network"
        return "sign_in_required"


# ---------------------------------------------------------------------------
# Bearer (admin-backend /api/auth/me), with an LRU cache and attempt budget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _AuthDecision:
    """Cached portion of a Bearer check: everything except mode, which is
    re-derived on every call."""
    authenticated: bool
    role: Optional[str]
    reason: str


_auth_cache: "OrderedDict[str, Tuple[_AuthDecision, float]]" = OrderedDict()
_auth_me_override: Optional[AuthMeCallable] = None
_auth_attempts = throttle.SlidingWindowLimiter(per_minute=_AUTH_ATTEMPTS_PER_MINUTE)


def _reset_for_tests() -> None:
    """PRIVATE -- test isolation only: caches, limiters, the auth adapter."""
    global _auth_me_override, _auth_attempts
    _auth_cache.clear()
    _auth_me_override = None
    _auth_attempts = throttle.SlidingWindowLimiter(per_minute=_AUTH_ATTEMPTS_PER_MINUTE)


def _configure_for_tests(env: Mapping[str, str], **kwargs) -> AuthSettings:
    """PRIVATE -- replace SETTINGS from an env mapping (own_ips defaults to
    none, so the test machine's addresses never matter)."""
    global SETTINGS
    kwargs.setdefault("own_ips", ())
    SETTINGS = load_settings(env, **kwargs)
    return SETTINGS


def _set_auth_me_callable_for_tests(fn: Optional[AuthMeCallable]) -> None:
    """PRIVATE -- injects a fake GET /api/auth/me for tests."""
    global _auth_me_override
    _auth_me_override = fn


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_get(key: str, now: float) -> Optional[_AuthDecision]:
    cached = _auth_cache.get(key)
    if cached is None or (now - cached[1]) >= _AUTH_CACHE_TTL_SECONDS:
        return None
    _auth_cache.move_to_end(key)
    return cached[0]


def _cache_put(key: str, decision: _AuthDecision, now: float) -> _AuthDecision:
    _auth_cache[key] = (decision, now)
    _auth_cache.move_to_end(key)
    while len(_auth_cache) > _AUTH_CACHE_MAX_ENTRIES:
        _auth_cache.popitem(last=False)
    return decision


async def _call_auth_me(token: str) -> httpx.Response:
    if _auth_me_override is not None:
        return await _auth_me_override(token)
    admin_url = get_admin_url()
    async with httpx.AsyncClient(timeout=_AUTH_ME_TIMEOUT_SECONDS) as client:
        return await client.get(f"{admin_url}/api/auth/me", headers={"Authorization": f"Bearer {token}"})


async def _resolve_auth_decision(token: Optional[str], rate_key: str) -> _AuthDecision:
    if not token:
        return _AuthDecision(False, None, "no_bearer_token")
    key = _digest(token)
    now = time.monotonic()
    cached = _cache_get(key, now)
    if cached is not None:
        return cached
    if not await _auth_attempts.allow(rate_key):
        logger.warning("jarvis_auth_attempts_throttled", key_hash=_digest(rate_key)[:12])
        return _AuthDecision(False, None, "auth_throttled")
    if not get_admin_url():
        return _cache_put(key, _AuthDecision(False, None, "admin_url_unset"), now)
    try:
        response = await _call_auth_me(token)
    except Exception as exc:  # never a 500
        logger.warning("jarvis_auth_me_unreachable", error=str(exc))
        return _cache_put(key, _AuthDecision(False, None, "admin_unreachable"), now)
    if response.status_code != 200:
        return _cache_put(key, _AuthDecision(False, None, "token_rejected"), now)
    try:
        body = response.json()
    except Exception:
        return _cache_put(key, _AuthDecision(False, None, "token_rejected"), now)
    role = body.get("role") if isinstance(body, dict) else None
    if role not in _PERMITTED_ROLES:
        return _cache_put(key, _AuthDecision(False, role, "role_not_permitted"), now)
    return _cache_put(key, _AuthDecision(True, role, "authenticated"), now)


def _extract_bearer_token(headers) -> Optional[str]:
    auth_header = headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    return auth_header[len("bearer "):].strip() or None


# ---------------------------------------------------------------------------
# Network evidence (D8)
# ---------------------------------------------------------------------------

def _has_cf_headers(headers) -> bool:
    """L6: a non-empty Cf-Connecting-Ip or Cf-Ray means the request came
    through Cloudflare, so it isn't home. Empty counts as absent, the same
    as the edge's HeaderRegexp(..., `.+`)."""
    for name in ("cf-connecting-ip", "cf-ray"):
        values = headers.getlist(name) if hasattr(headers, "getlist") else [headers.get(name)]
        if any(v and v.strip() for v in values):
            return True
    return False


def _peer(scope_client) -> Optional[str]:
    return scope_client.host if scope_client else None


def home_candidate(peer: Optional[str], headers, s: AuthSettings):
    """The single address the home rules may test, or None."""
    if s.direct_clients:
        addr = throttle.parse_ip(peer)
        if addr is None or any(addr == gw for gw in s.gateways):
            return None
        return addr
    return throttle.local_candidate(peer, throttle.read_forwarded_for(headers), s.trusted_proxies, s.trusted_hops)


def _network_match(addr, networks: Tuple[Network, ...]) -> Optional[Network]:
    for net in networks:
        if throttle.in_networks(addr, (net,)):
            return net
    return None


def classify_network(peer: Optional[str], headers, s: AuthSettings) -> Tuple[Optional[str], Optional[str]]:
    """(CLASS_LOCAL | CLASS_GUEST_NET | None, the matched CIDR)."""
    if not s.local_enabled or _has_cf_headers(headers):
        return None, None
    host = normalize_host(headers.get("host"))
    if host is None or host not in s.allowed_hosts:
        return None, None
    addr = home_candidate(peer, headers, s)
    if addr is None or throttle.in_networks(addr, s.local_exclude):
        return None, None
    guest = _network_match(addr, s.guest_networks)
    if guest is not None:
        return CLASS_GUEST_NET, str(guest)
    local = _network_match(addr, s.local_networks)
    if local is not None:
        return CLASS_LOCAL, str(local)
    return None, None


def _valid_service_key(value: Optional[str], s: AuthSettings) -> bool:
    key = s.service_api_key
    if not value or not key or key == _PLACEHOLDER_SERVICE_KEY:
        return False
    return hmac.compare_digest(value.encode("utf-8"), key.encode("utf-8"))


def rate_client(peer: Optional[str], headers, s: AuthSettings) -> str:
    resolved = throttle.resolve_rate_client(
        peer, throttle.read_forwarded_for(headers), headers.get("cf-connecting-ip"), s.trusted_proxies, s.trust_cf
    )
    return throttle.rate_limit_key(resolved.ip)


# ---------------------------------------------------------------------------
# Edge attestation (D1)
# ---------------------------------------------------------------------------

def _single(headers, name: str) -> Optional[str]:
    """The header's value when it's present exactly once (a duplicated
    header is ambiguous, so it counts as absent)."""
    values = headers.getlist(name) if hasattr(headers, "getlist") else (
        [headers.get(name)] if headers.get(name) is not None else []
    )
    if len(values) != 1:
        return None
    return values[0]


def _attestation(headers, s: AuthSettings) -> str:
    presented = _single(headers, _EDGE_ATTESTATION_HEADER)
    if not presented:
        return "none"
    raw = presented.encode("utf-8")
    matched = "none"
    # compare against both, always, so timing doesn't reveal which matched
    if hmac.compare_digest(raw, s.edge_current.encode("utf-8")):
        matched = "current"
    if s.edge_previous and hmac.compare_digest(raw, s.edge_previous.encode("utf-8")):
        matched = "current" if matched == "current" else "previous"
    return matched


def _household_member(groups_value: Optional[str], s: AuthSettings) -> bool:
    if not groups_value or not s.household_groups:
        return False
    groups = {g.strip() for g in groups_value.split(s.groups_separator)}
    return bool(groups & s.household_groups)


def edge_verdict(peer: Optional[str], headers, s: AuthSettings) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    """(caller class or None, attestation, identity, matched CIDR).

    The class header counts only when the attestation matches and the TCP
    peer is a trusted proxy. home/guest must also be corroborated by the D8
    candidate and the Host; identity headers are read only for an
    authenticated verdict.
    """
    if not s.edge_mode:
        return None, "none", None, None
    attestation = _attestation(headers, s)
    if attestation == "none":
        return None, "none", None, None
    peer_addr = throttle.parse_ip(peer)
    if not throttle.in_networks(peer_addr, s.trusted_proxies):
        logger.warning("jarvis_edge_attestation_from_untrusted_peer", edge_attestation=attestation)
        return None, attestation, None, None
    edge_class = _single(headers, _EDGE_CLASS_HEADER)
    if edge_class not in _EDGE_CLASSES:
        return None, attestation, None, None
    if edge_class == "authenticated":
        identity = _single(headers, s.identity_header)
        identity = identity.strip() if identity else None
        if identity and _household_member(_single(headers, s.groups_header), s):
            return CLASS_AUTHENTICATED, attestation, identity, None
        return CLASS_NOT_HOUSEHOLD, attestation, None, None
    network_class, matched = classify_network(peer, headers, s)
    wanted = CLASS_LOCAL if edge_class == "home" else CLASS_GUEST_NET
    if network_class == wanted:
        return wanted, attestation, None, matched
    logger.warning("jarvis_edge_class_not_corroborated", edge_class=edge_class, edge_attestation=attestation)
    return None, attestation, None, None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

async def _mode_for(caller_class: str, resolver: Optional[HouseholdModeResolver]) -> str:
    if caller_class not in {CLASS_AUTHENTICATED, CLASS_LOCAL}:
        return "guest"
    if resolver is None:
        logger.error("jarvis_caller_mode_resolver_missing")
        return "guest"
    return await resolver()


async def _resolve(
    peer: Optional[str],
    headers,
    resolver: Optional[HouseholdModeResolver],
    *,
    allow_service: bool,
) -> Caller:
    s = SETTINGS
    rate_key = rate_client(peer, headers, s)

    edge_class, attestation, identity, edge_matched = edge_verdict(peer, headers, s)
    caller: Optional[Caller] = None
    if edge_class is not None:
        mode = await _mode_for(edge_class, resolver)
        caller = Caller(
            edge_class, mode, edge_class == CLASS_AUTHENTICATED, None, "edge", "edge",
            edge_matched, identity, attestation,
        )
    if caller is None or caller.caller_class == CLASS_NOT_HOUSEHOLD:
        decision = await _resolve_auth_decision(_extract_bearer_token(headers), rate_key)
        if decision.authenticated:
            mode = await _mode_for(CLASS_AUTHENTICATED, resolver)
            caller = Caller(CLASS_AUTHENTICATED, mode, True, decision.role, decision.reason, "bearer",
                            edge_attestation=attestation)
        elif caller is None and allow_service and _valid_service_key(headers.get("x-service-key"), s):
            caller = Caller(CLASS_SERVICE, "guest", False, None, "service_key", "service")
        elif caller is None:
            cls, matched = (None, None) if s.edge_mode else classify_network(peer, headers, s)
            if cls is not None:
                mode = await _mode_for(cls, resolver)
                source = "local" if cls == CLASS_LOCAL else "guest_network"
                caller = Caller(cls, mode, False, decision.role, decision.reason, source, matched)
            else:
                caller = Caller(CLASS_PUBLIC, "guest", False, decision.role, decision.reason, "none",
                                edge_attestation=attestation)

    logger.info(
        "jarvis_caller_resolved",
        caller_class=caller.caller_class,
        trust=caller.trust,
        source=caller.source,
        mode=caller.mode,
        role=caller.role,
        reason=caller.reason,
        matched_local_network=caller.matched_network,
        edge_attestation=caller.edge_attestation,
        key_hash=_digest(rate_key)[:12],
    )
    return caller


async def resolve_caller(
    request: Request,
    household_mode_resolver: Optional[HouseholdModeResolver] = None,
    *,
    allow_service: bool = False,
) -> Caller:
    """Resolve the caller for an HTTP request (never raises for auth)."""
    return await _resolve(_peer(request.client), request.headers, household_mode_resolver, allow_service=allow_service)


async def resolve_caller_ws(
    websocket: WebSocket,
    household_mode_resolver: Optional[HouseholdModeResolver] = None,
) -> Caller:
    """Resolve the caller for a WebSocket upgrade. The handler must
    close(1008) before accept() when it isn't permitted."""
    return await _resolve(_peer(websocket.client), websocket.headers, household_mode_resolver, allow_service=False)


def ws_origin_allowed(websocket: WebSocket) -> bool:
    """D21: a browser WebSocket must come from an allowlisted origin (or
    the request's own Host). A missing Origin is refused."""
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    try:
        origin_host = normalize_host(httpx.URL(origin).host)
    except Exception:
        return False
    own = normalize_host(websocket.headers.get("host"))
    return bool(origin_host) and (origin_host in SETTINGS.allowed_hosts or origin_host == own)


# ---------------------------------------------------------------------------
# Dependencies (FastAPI). Auth first, then the CSRF header, so an
# unauthenticated POST is 401 whatever it carries.
# ---------------------------------------------------------------------------

def unauthenticated() -> HTTPException:
    return HTTPException(status_code=401, detail="sign_in_required", headers={"WWW-Authenticate": "Jarvis"})


def refuse(caller: Caller) -> HTTPException:
    """Signed in at the edge but not in a household group: 403, not 401
    (signing in again won't help)."""
    if caller.caller_class == CLASS_NOT_HOUSEHOLD:
        return HTTPException(status_code=403, detail="not_household")
    return unauthenticated()


def _require_csrf_header(request: Request, caller: Caller) -> None:
    if request.method.upper() in _SAFE_METHODS or caller.caller_class == CLASS_SERVICE:
        return
    if request.headers.get(_CSRF_HEADER) != "1":
        logger.warning("jarvis_csrf_header_missing", path=request.url.path, caller_class=caller.caller_class)
        raise HTTPException(status_code=403, detail="reload_required")


def _store(request: Request, caller: Caller) -> Caller:
    request.state.caller = caller
    return caller


async def require_browser_caller(request: Request, household_mode_resolver: Optional[HouseholdModeResolver] = None) -> Caller:
    """browser routes: any home-network or signed-in browser."""
    caller = await resolve_caller(request, household_mode_resolver)
    if not caller.is_browser:
        raise refuse(caller)
    _require_csrf_header(request, caller)
    return _store(request, caller)


async def require_relay_chat(request: Request, household_mode_resolver: Optional[HouseholdModeResolver] = None) -> Caller:
    """relay_chat routes (chat): browsers today; the embed relay joins later."""
    return await require_browser_caller(request, household_mode_resolver)


async def require_guest_reader(request: Request, household_mode_resolver: Optional[HouseholdModeResolver] = None) -> Caller:
    """guest_read routes: household readers and the guest network."""
    caller = await resolve_caller(request, household_mode_resolver)
    if not caller.is_browser:
        raise refuse(caller)
    _require_csrf_header(request, caller)
    return _store(request, caller)


async def require_household_reader(
    request: Request,
    household_mode_resolver: Optional[HouseholdModeResolver] = None,
    allow_service: bool = True,
) -> Caller:
    """household_read routes: household browsers and (GETs) the service
    caller. The guest network may not read these (403 guest_network)."""
    caller = await resolve_caller(request, household_mode_resolver, allow_service=allow_service)
    if caller.caller_class == CLASS_GUEST_NET:
        raise HTTPException(status_code=403, detail="guest_network")
    if not caller.can_read_household:
        raise refuse(caller)
    _require_csrf_header(request, caller)
    return _store(request, caller)


async def require_owner_caller(request: Request, household_mode_resolver: Optional[HouseholdModeResolver] = None) -> Caller:
    """owner_only routes: a signed-in owner/operator, or the household
    network while the house is in owner mode (403 guest_stay_active
    otherwise, never 401)."""
    caller = await resolve_caller(request, household_mode_resolver)
    if not caller.is_browser:
        logger.warning("jarvis_owner_only_route_refused", path=request.url.path, reason=caller.reason)
        raise refuse(caller)
    if not caller.owner_permitted:
        logger.warning("jarvis_owner_only_route_refused", path=request.url.path, reason=caller.control_reason)
        raise HTTPException(status_code=403, detail="guest_stay_active")
    _require_csrf_header(request, caller)
    return _store(request, caller)


def is_owner_permitted(caller: Caller) -> bool:
    return caller.owner_permitted


# Route classes (the census vocabulary) -> their gate.
ROUTE_GATES = {
    "owner_only": require_owner_caller,
    "household_read": require_household_reader,
    "guest_read": require_guest_reader,
    "browser": require_browser_caller,
    "relay_chat": require_relay_chat,
}


def route_dependency(route_class: str, household_mode_resolver: HouseholdModeResolver):
    """A FastAPI dependency taking only the request, bound to one route
    class and the app's household-mode resolver. (A functools.partial with
    extra keywords would surface those keywords as query parameters.)"""
    gate = ROUTE_GATES[route_class]

    async def dependency(request: Request) -> Caller:
        return await gate(request, household_mode_resolver)

    dependency.__name__ = f"require_{route_class}"
    dependency.route_class = route_class
    return dependency
