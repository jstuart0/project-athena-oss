"""Where Athena's Home Assistant address and token come from, in one place.

Every process that talks to Home Assistant asks this module for its
credentials. The rule is one pure function (``resolve``): the admin panel's
``home-assistant`` External API Key wins, the ``HA_URL``/``HA_TOKEN``
environment is the fallback, and the last admin value is kept while the admin
backend is unreachable. ``HACredentialResolver`` re-reads the admin key on a
cadence so a token saved in the admin panel takes effect without a restart.

Layout:

- ``ha_url_problem``: the one URL policy, returning a fixed reason word (never
  the URL). Admin-supplied URLs also pass the shared service-host rule.
- ``KeyLookup`` and the ``AdminKeySource`` port: ``found`` / ``absent`` /
  ``unavailable``. The HTTP adapter (orchestrator, gateway) is
  ``shared.admin_config``; the database adapter (admin-backend) is its own.
- ``HACredentials``: a frozen snapshot. The token is a ``_Secret`` that
  prints as ``***`` and cannot be pickled, and the snapshot's ``repr`` shows no
  URL, host or token.
- ``resolve``: pure. ``HACredentialResolver``: the cadence, the single-flight
  refresh, the runtime DNS check of an admin-supplied hostname, the current
  and previous snapshot, change callbacks.

Logs carry reason words and booleans only: never a URL, a host or a token.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

import httpx
import structlog

from shared.url_safety import is_local_host, validate_service_host

logger = structlog.get_logger(__name__)

HA_KEY_SERVICE = "home-assistant"

# External API Key services whose value must never be handed to a tool, a workflow
# or any other consumer that puts it in front of a model or a third party. The
# Home Assistant token controls the house; only shared.ha_credentials' readers
# may hold it. Every place that injects a stored key honours this set
# (tests/unit/test_ha_consolidation_drift.py: TestNonInjectableKeys).
NON_INJECTABLE_KEY_SERVICES = frozenset({HA_KEY_SERVICE})
NOT_INJECTABLE_REFUSAL = "This API key can't be attached to a tool."

SOURCE_ADMIN = "admin"
SOURCE_ENV = "env"
SOURCE_NONE = "none"

MAX_URL_LENGTH = 2048
REFRESH_INTERVAL_SECONDS = 60.0
FORCED_REFRESH_MIN_INTERVAL = 10.0
LOOKUP_TIMEOUT_SECONDS = 3.0
DNS_TIMEOUT_SECONDS = 2.0

_DIGEST_DOMAIN = "ha-cred-v2"
WS_PATH = "/api/websocket"
_IMDS_V4 = ipaddress.ip_address("169.254.169.254")
_IMDS_V6 = ipaddress.ip_address("fd00:ec2::254")
_UNSAFE_CHARACTER = re.compile(r"[\\@\s\x00-\x1f\x7f]")
_PROCESS_KEY = secrets.token_bytes(32)

_NOTE_EVENTS = {
    "plain_http": "ha_plain_http",
    "ws_url_host_mismatch": "ha_ws_url_host_mismatch",
    "ws_url_ignored": "ha_ws_url_ignored",
}

PROBLEM_WORDS = frozenset({
    "empty", "unsafe_character", "too_long", "unparseable", "bad_scheme", "query",
    "fragment", "path", "host", "port", "blocked_address", "host_not_allowed",
    "incomplete", "dns_unavailable", "host_form",
})


class _Secret(str):
    """A token that never prints. ``reveal()`` is the one way to read it.

    ``str()``, ``repr()`` and ``format()`` give ``***``; pickling and copying
    through ``__reduce__`` raise, so a stray ``pickle.dumps`` or a log of the
    object can't carry it out. (``copy`` and ``deepcopy`` return the same
    immutable object, which is what ``dataclasses.asdict`` needs.)
    """

    __slots__ = ()

    def reveal(self) -> str:
        return str.__str__(self)

    def __repr__(self) -> str:
        return "'***'"

    def __str__(self) -> str:
        return "***"

    def __format__(self, spec: str) -> str:
        return "***"

    def __reduce__(self):
        raise TypeError("a Home Assistant token can't be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("a Home Assistant token can't be serialized")

    def __getstate__(self):
        raise TypeError("a Home Assistant token can't be serialized")

    def __copy__(self) -> "_Secret":
        return self

    def __deepcopy__(self, memo: Dict[int, Any]) -> "_Secret":
        return self


@dataclass(frozen=True)
class KeyLookup:
    """The admin key row as a source saw it: ``found`` (an enabled row),
    ``absent`` (no enabled row) or ``unavailable`` (couldn't be read)."""

    status: str
    url: str = field(default="", repr=False)
    token: str = field(default="", repr=False)

    @classmethod
    def found(cls, url: Optional[str], token: Optional[str]) -> "KeyLookup":
        return cls("found", (url or "").strip(), (token or "").strip())

    @classmethod
    def absent(cls) -> "KeyLookup":
        return cls("absent")

    @classmethod
    def unavailable(cls) -> "KeyLookup":
        return cls("unavailable")


AdminKeySource = Callable[[], Awaitable[KeyLookup]]


def admin_http_source(admin_client: Any) -> AdminKeySource:
    """The admin key as an orchestrator or gateway reads it: over the admin
    backend's service-key route (``AdminConfigClient.lookup_external_api_key``)."""

    async def source() -> KeyLookup:
        status, data = await admin_client.lookup_external_api_key(HA_KEY_SERVICE)
        if status == "absent":
            return KeyLookup.absent()
        if status != "found" or not isinstance(data, dict):
            return KeyLookup.unavailable()
        url, token = data.get("endpoint_url"), data.get("api_key")
        if not (isinstance(url, str) or url is None) or not (isinstance(token, str) or token is None):
            return KeyLookup.unavailable()
        return KeyLookup.found(url, token)

    return source


@dataclass(frozen=True)
class HACredentials:
    """One immutable view of the credentials. ``configured`` is a URL and a
    token that passed the URL policy. ``digest`` keys the shared auth state:
    it is empty unless configured."""

    url: str = field(default="", repr=False)
    token: _Secret = field(default=_Secret(""), repr=False)
    ws_url: str = field(default="", repr=False)
    source: str = SOURCE_NONE
    digest: str = ""
    shared: bool = False
    problem: Optional[str] = None
    notes: Tuple[str, ...] = field(default=(), compare=False)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token.reveal()) and self.problem is None

    @property
    def origin(self) -> str:
        parsed = _inspect(self.url, SOURCE_ENV)[1] if self.url else None
        return parsed.origin if parsed else ""

    def identity(self) -> Tuple[Any, ...]:
        """What a change in ``on_change`` means; includes the token."""
        return (self.url, self.token.reveal(), self.ws_url, self.source, self.problem)

    def __repr__(self) -> str:
        return f"HACredentials(source={self.source!r}, configured={self.configured}, problem={self.problem!r})"


@dataclass(frozen=True)
class _Parsed:
    scheme: str
    host: str
    port: int
    explicit_port: Optional[int]

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.explicit_port}" if self.explicit_port is not None else ""
        return f"{self.scheme}://{host}{port}"


def _literal_ip(host: str):
    """The address a canonical dotted IPv4 or IPv6 literal names, with an
    IPv4-mapped IPv6 address unwrapped; None for anything else (a name)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def always_blocked(ip) -> bool:
    """Addresses no Home Assistant URL may name, whatever the source: cloud
    metadata, link-local, multicast and unspecified."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip == _IMDS_V4 or ip == _IMDS_V6
        or ip.is_link_local or ip.is_multicast or ip.is_unspecified
    )


_DECIMAL_LABEL = re.compile(r"[0-9]+")
_HEX_LABEL = re.compile(r"0x[0-9a-f]*")


def _odd_host_form(host: str) -> bool:
    """A host a resolver might read as a number: every label is decimal or ``0x``
    hex and there are at most four of them (``2852039166``, ``0xa9fea9fe``,
    ``169.254.43518``, ``1.2.3``), with a single trailing dot allowed on such a
    host (``10.20.30.10.``). A real name that merely contains a numeric label
    (``10.0.0.5.nip.io``, ``ha.2.lan``, ``3.example.com``), or ends in a dot
    (``ha.example.com.``), is a name: the DNS check at use judges it. Canonical IP
    literals are handled before this."""
    labels = host.lower().rstrip(".").split(".") if host.endswith(".") else host.lower().split(".")
    if not 1 <= len(labels) <= 4:
        return False
    return all(_DECIMAL_LABEL.fullmatch(label) or _HEX_LABEL.fullmatch(label) for label in labels)


def _written_as_literal(host: str) -> Optional[bool]:
    """None when ``host`` isn't an IP literal at all; otherwise whether it is
    written in its canonical text (``str(ip) == host.lower()``)."""
    try:
        return str(ipaddress.ip_address(host)) == host.lower()
    except ValueError:
        return None


def _inspect(url: Any, source: str) -> Tuple[Optional[str], Optional[_Parsed]]:
    """``(problem word, parsed parts)``; exactly one of them is None."""
    if not isinstance(url, str) or not url:
        return "empty", None
    if _UNSAFE_CHARACTER.search(url):
        return "unsafe_character", None
    if len(url) > MAX_URL_LENGTH:
        return "too_long", None
    try:
        parsed = httpx.URL(url)
    except Exception:
        return "unparseable", None
    if parsed.scheme not in ("http", "https"):
        return "bad_scheme", None
    if "?" in url:
        return "query", None
    if "#" in url:
        return "fragment", None
    if parsed.path not in ("", "/"):
        return "path", None
    host = parsed.host
    if not host:
        return "host", None
    explicit = parsed.port
    if explicit is not None and not 1 <= explicit <= 65535:
        return "port", None
    effective = explicit if explicit is not None else (443 if parsed.scheme == "https" else 80)
    ip = _literal_ip(host)
    if ip is not None and always_blocked(ip):
        return "blocked_address", None
    written = _written_as_literal(host)
    if written is False or (written is None and _odd_host_form(host)):
        return "host_form", None
    if source == SOURCE_ADMIN:
        checked = str(ip) if ip is not None else host
        if not (checked in ("localhost", "127.0.0.1", "::1") and is_local_host(checked)):
            try:
                validate_service_host(checked)
            except ValueError:
                return "host_not_allowed", None
    return None, _Parsed(parsed.scheme, host, effective, explicit)


def ha_url_problem(url: Any, *, source: str) -> Optional[str]:
    """None when ``url`` may be a Home Assistant address for ``source``
    (``"admin"`` or ``"env"``), else a fixed reason word.

    Every source: no backslash, ``@``, whitespace or control character; at
    most 2048 characters; parsed by ``httpx.URL`` (the parser that sends the
    request); http or https; no query or fragment; the path empty or ``/``; a
    valid port; a host that isn't written as an odd numeric form (``host_form``: at
    most four labels that are all decimal or ``0x`` numbers, such as ``2852039166``,
    ``0xa9fea9fe`` or ``169.254.43518``, with one trailing dot allowed only on such a
    host; an IP literal must also be in its canonical text, so ``010.1.1.1`` is
    refused); and a host that isn't cloud metadata, link-local, multicast or
    unspecified (an IPv4-mapped IPv6 literal is judged as its IPv4 address). Any
    other host is a name, including one that merely contains a numeric label
    (``10.0.0.5.nip.io``, ``ha.2.lan``) or ends in a dot, and the resolver judges
    what it resolves to.

    An admin-supplied URL must also pass the shared service-host rule (no
    loopback, ``.svc`` or ``.cluster.local``), with loopback allowed outside a
    Kubernetes pod. Environment URLs keep both, because a deployer's in-cluster
    Home Assistant is a working setup.
    """
    return _inspect(url, source)[0]


def url_origin(url: Any) -> str:
    """The origin (scheme, host, effective port) of an admin-style address, or ""
    for one that isn't a valid address. A token is bound to this: a changed
    origin means the stored token is no longer the right one to send there."""
    parsed = _inspect(url, SOURCE_ADMIN)[1]
    return parsed.origin if parsed else ""


def websocket_url(ha_url: str) -> str:
    base = ha_url.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + WS_PATH


def legacy_marker_digest(token: str, service_api_key: Optional[str]) -> Optional[str]:
    """The suffix of the area registry's ``athena:ha_registry:refused:`` marker:
    an HMAC of the token keyed on the service key, or None without either.
    Old registries still read that key, so the new latch keeps writing it."""
    if not token or not service_api_key:
        return None
    return hmac.new(service_api_key.encode(), token.encode(), hashlib.sha256).hexdigest()[:16]


def credential_digest(origin: str, token: str, service_api_key: Optional[str]) -> Tuple[str, bool]:
    """``(digest, shared)``. The digest binds the origin and the token, so a
    corrected URL is a new digest. Without a service key the key is random
    per process and the digest can't be shared."""
    key = service_api_key.encode() if service_api_key else _PROCESS_KEY
    message = f"{_DIGEST_DOMAIN}\n{origin}\n{token}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32], bool(service_api_key)


def _ws_override(raw: str, http_parsed: _Parsed) -> Tuple[str, List[str]]:
    """The ``HA_WS_URL`` override as ``(ws url or "", notes)``. A value that
    isn't a clean ws/wss URL is ignored; its host differing from the REST
    host is noted, never logged."""
    if _UNSAFE_CHARACTER.search(raw) or len(raw) > MAX_URL_LENGTH or "?" in raw or "#" in raw:
        return "", ["ws_url_ignored"]
    try:
        parsed = httpx.URL(raw.replace("ws://", "http://", 1).replace("wss://", "https://", 1))
    except Exception:
        return "", ["ws_url_ignored"]
    if not (raw.startswith("ws://") or raw.startswith("wss://")) or not parsed.host:
        return "", ["ws_url_ignored"]
    if parsed.path not in ("", "/", WS_PATH):
        return "", ["ws_url_ignored"]
    ip = _literal_ip(parsed.host)
    if ip is not None and always_blocked(ip):
        return "", ["ws_url_ignored"]
    notes = [] if parsed.host == http_parsed.host else ["ws_url_host_mismatch"]
    scheme = "wss" if raw.startswith("wss://") else "ws"
    host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{scheme}://{host}{port}{WS_PATH}", notes


def _build(url: str, token: str, source: str, ws_override: str, service_api_key: Optional[str]) -> HACredentials:
    if not url and not token:
        return HACredentials(source=SOURCE_NONE if source == SOURCE_ENV else source,
                             problem="incomplete" if source == SOURCE_ADMIN else None)
    if not url or not token:
        return HACredentials(source=source, problem="incomplete")
    problem, parsed = _inspect(url, source)
    if parsed is None:
        return HACredentials(source=source, problem=problem)
    notes: List[str] = []
    ws_url = websocket_url(parsed.url)
    if source == SOURCE_ENV and ws_override:
        override, ws_notes = _ws_override(ws_override, parsed)
        notes.extend(ws_notes)
        ws_url = override or ws_url
    elif source == SOURCE_ADMIN and ws_override:
        notes.append("ws_url_ignored")
    if parsed.scheme == "http":
        notes.append("plain_http")
    digest, shared = credential_digest(parsed.origin, token, service_api_key)
    return HACredentials(
        url=parsed.url, token=_Secret(token), ws_url=ws_url, source=source,
        digest=digest, shared=shared, problem=None, notes=tuple(notes),
    )


def resolve(
    lookup: KeyLookup,
    env: Mapping[str, str],
    *,
    previous: Optional[HACredentials],
    service_api_key: Optional[str],
) -> HACredentials:
    """The credentials for one admin lookup and one environment. Pure.

    - ``found``: the admin row decides, alone. A row that is half-filled or
      whose URL fails the policy is ``configured=False`` with a ``problem`` and
      never falls through to the environment.
    - ``absent``: the environment decides.
    - ``unavailable``: the previous admin value stays; with none, the
      environment decides.
    """
    ws_override = (env.get("HA_WS_URL") or "").strip()
    if lookup.status == "found":
        return _build(lookup.url, lookup.token, SOURCE_ADMIN, ws_override, service_api_key)
    if lookup.status == "unavailable" and previous is not None and previous.source == SOURCE_ADMIN:
        return previous
    return _build((env.get("HA_URL") or "").strip(), (env.get("HA_TOKEN") or "").strip(),
                  SOURCE_ENV, ws_override, service_api_key)


class StaticResolver:
    """A resolver over one fixed snapshot, for tools and tests. It never
    refreshes."""

    def __init__(self, credentials: HACredentials):
        self._credentials = credentials

    def current(self) -> HACredentials:
        return self._credentials

    def snapshot_for(self, digest: str) -> Optional[HACredentials]:
        return self._credentials if digest and digest == self._credentials.digest else None

    async def refresh(self, force: bool = False) -> None:
        return None


GetAddrInfo = Callable[..., Awaitable[List[Tuple[Any, ...]]]]


class HACredentialResolver:
    """The live credentials of one process.

    ``current()`` is synchronous, O(1) and never raises; before ``start()`` it
    is the environment-only result. ``refresh()`` does one admin read and, for
    an admin-supplied hostname, one DNS lookup, then swaps the snapshot and
    runs the ``on_change`` callbacks. Nothing here raises to a caller.
    """

    def __init__(
        self,
        admin_source: AdminKeySource,
        *,
        service_api_key: Optional[str],
        env: Mapping[str, str] = os.environ,
        interval: float = REFRESH_INTERVAL_SECONDS,
        lookup_timeout: float = LOOKUP_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        getaddrinfo: Optional[GetAddrInfo] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self._source = admin_source
        self._service_api_key = service_api_key or None
        self._env = env
        self._interval = interval
        self._lookup_timeout = lookup_timeout
        self._monotonic = monotonic
        self._getaddrinfo = getaddrinfo
        self._sleep = sleep
        self._current = resolve(KeyLookup.absent(), env, previous=None, service_api_key=self._service_api_key)
        self._previous: Optional[HACredentials] = None
        self._callbacks: List[Callable[[HACredentials, HACredentials], None]] = []
        self._lock = asyncio.Lock()
        self._last_forced: Optional[float] = None
        self._closed = False
        self._task: Optional["asyncio.Task[Any]"] = None
        self._logged_unshared = False
        self._dns_logged: Optional[str] = None
        self._announced: Optional[Tuple[Any, ...]] = None

    # -- reads ------------------------------------------------------------

    def current(self) -> HACredentials:
        return self._current

    def snapshot_for(self, digest: str) -> Optional[HACredentials]:
        """The current snapshot or the immediately previous one whose digest
        is ``digest``; None for anything else (so a request admitted under a
        token that was since replaced twice can't be sent)."""
        if not digest:
            return None
        for snapshot in (self._current, self._previous):
            if snapshot is not None and snapshot.digest == digest:
                return snapshot
        return None

    def on_change(self, callback: Callable[[HACredentials, HACredentials], None]) -> None:
        self._callbacks.append(callback)

    # -- refreshing -------------------------------------------------------

    async def refresh(self, force: bool = False) -> None:
        """One refresh; concurrent calls share the one in flight. A forced
        refresh runs at most once per ``FORCED_REFRESH_MIN_INTERVAL``."""
        if self._closed:
            return
        if force:
            now = self._monotonic()
            if self._last_forced is not None and now - self._last_forced < FORCED_REFRESH_MIN_INTERVAL:
                return
            self._last_forced = now
        if self._lock.locked():
            async with self._lock:
                return
        async with self._lock:
            try:
                await self._refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ha_credentials_refresh_failed", error_class=type(exc).__name__)

    async def _lookup(self) -> KeyLookup:
        try:
            return await asyncio.wait_for(self._source(), self._lookup_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            return KeyLookup.unavailable()

    async def _refresh_once(self) -> None:
        old = self._current
        candidate = resolve(await self._lookup(), self._env, previous=old, service_api_key=self._service_api_key)
        candidate = await self._check_dns(candidate, old)
        changed = candidate.identity() != old.identity()
        if changed:
            self._previous = old
            self._current = candidate
        self._announce(self._current, changed)
        if changed:
            for callback in list(self._callbacks):
                try:
                    callback(candidate, old)
                except Exception as exc:
                    logger.warning("ha_credentials_callback_failed", error_class=type(exc).__name__)

    async def _check_dns(self, candidate: HACredentials, old: HACredentials) -> HACredentials:
        """The runtime half of the URL policy for an admin-supplied name: no
        resolved address may be blocked. A lookup that fails rejects nothing;
        it keeps the previous snapshot (and with none, reads as unavailable)."""
        if candidate.source != SOURCE_ADMIN or not candidate.configured:
            self._dns_logged = None
            return candidate
        host = _inspect(candidate.url, SOURCE_ADMIN)[1].host
        if _literal_ip(host) is not None:
            return candidate
        try:
            lookup = self._getaddrinfo or asyncio.get_running_loop().getaddrinfo
            infos = await asyncio.wait_for(lookup(host, None, type=socket.SOCK_STREAM), DNS_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._dns_logged != "unavailable":
                logger.warning("ha_credentials_dns_unavailable")
                self._dns_logged = "unavailable"
            if old.source == SOURCE_ADMIN:
                return old
            return dataclasses.replace(candidate, problem="dns_unavailable")
        self._dns_logged = None
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            except (ValueError, IndexError, AttributeError):
                continue
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if always_blocked(ip) or (ip.is_loopback and not is_local_host(str(ip))):
                return dataclasses.replace(candidate, problem="blocked_address")
        return candidate

    def _announce(self, creds: HACredentials, changed: bool) -> None:
        fingerprint = creds.identity()
        if fingerprint == self._announced:
            return
        self._announced = fingerprint
        logger.info(
            "ha_credentials_resolved", source=creds.source, configured=creds.configured, changed=changed,
        )
        if creds.problem is not None and creds.source != SOURCE_NONE:
            logger.warning("ha_credentials_rejected", reason=creds.problem)
        for note in creds.notes:
            logger.warning(_NOTE_EVENTS[note])
        if creds.configured and not creds.shared and not self._logged_unshared:
            self._logged_unshared = True
            logger.warning("ha_auth_unshared")

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """One bounded refresh, so a service starts with the admin key
        applied when the admin backend answers."""
        await self.refresh()

    async def run(self) -> None:
        self._task = asyncio.current_task()
        while not self._closed:
            await self._sleep(self._interval)
            await self.refresh()

    async def close(self) -> None:
        self._closed = True
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._task = None
