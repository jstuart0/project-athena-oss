"""Per-source new-conversation rate limiter (ATHENA-88 / F88 D4, F39).

The gateway is the only hop that sees the real client address for HA
traffic (the orchestrator sees the gateway pod IP for everything). This
limiter bounds how many *new* conversations (first-turn requests with no
explicit session_id) a single source can start per minute, independent of
the existing global_rate_limiter (which is a single global bucket, not
per-source, and isn't consulted on /v1/responses).

ATHENA-88 / F39 (codex r2 Medium, reconciliation round 1): behind a
reverse proxy (Traefik), every caller's immediate TCP peer is the proxy's
own pod IP -- keying on raw_request.client.host alone collapses the whole
house onto one shared bucket. resolve_client_key trusts X-Forwarded-For
only when the immediate peer is inside a configured trusted-proxy CIDR
list, so an untrusted (non-proxy) caller can't spoof its key via that
header. Two in-memory gateway replicas also each enforce their own
window inconsistently; RedisNewConversationLimiter backs the same sliding-
window contract with a shared Redis sorted set so replicas agree.

Hides: the sliding window, the clock, the LRU key bound (in-memory), and
the Redis key scheme (Redis-backed). Callers only see
`await limiter.allow(key) -> bool`.
"""
from __future__ import annotations

import ipaddress
import time
import uuid
from collections import OrderedDict
from typing import Callable, Dict, List, Optional


class NewConversationLimiter:
    """Sliding 60s per-key window, in-memory, LRU-bounded on distinct keys.

    Single-process only -- two replicas each enforce their own window. Used
    as the fallback when no Redis connection is available.
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
        """Return True and record a hit if `key` is under budget this minute."""
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


# ATHENA-88 / F43 (codex r2b Medium, reconciliation round 2): trim, count,
# conditional add, and expire all run as ONE atomic EVAL. KEYS[1]=redis key,
# ARGV[1]=window_start (trim threshold), ARGV[2]=now (score for the new
# member), ARGV[3]=per_minute (limit), ARGV[4]=member (unique id),
# ARGV[5]=ttl. Returns 1 if allowed (and recorded), 0 if over budget.
_ALLOW_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[3]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""


class RedisNewConversationLimiter:
    """Same sliding-60s-window contract, counters shared across replicas via
    a Redis sorted set per key (score = hit timestamp).

    ATHENA-88 / F43 (codex r2b Medium): trim + count + conditional add +
    expire run as one atomic EVAL, not four separate round-trips. Separate
    awaits let two concurrent first-turn requests (different replicas, or
    even the same event loop under real network latency) both observe the
    same below-limit ZCARD before either ZADDs, letting both pass a
    per_minute=1 budget. A single EVAL call is atomic on the Redis server —
    no other client's commands can interleave with it.
    """

    def __init__(
        self,
        redis_client,
        per_minute: int,
        key_prefix: str = "athena:gw:newconv:",
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis_client
        self._per_minute = per_minute
        self._key_prefix = key_prefix
        self._window_seconds = window_seconds
        self._clock = clock

    async def allow(self, key: str) -> bool:
        redis_key = f"{self._key_prefix}{key}"
        now = self._clock()
        window_start = now - self._window_seconds
        member = f"{now}:{uuid.uuid4().hex}"
        ttl = int(self._window_seconds) + 5

        result = await self._redis.eval(
            _ALLOW_SCRIPT,
            1,
            redis_key,
            window_start,
            now,
            self._per_minute,
            member,
            ttl,
        )
        return bool(int(result))


def resolve_client_key(
    client_host: Optional[str],
    forwarded_for: Optional[str],
    trusted_proxy_cidrs: str,
) -> str:
    """Resolve the rate-limiter key for a request.

    Trusts X-Forwarded-For's first (left-most, originating) address only
    when the immediate TCP peer (`client_host`) falls inside one of the
    comma-separated CIDRs in `trusted_proxy_cidrs`. An untrusted caller's
    own X-Forwarded-For header is ignored -- it can't spoof another
    source's key. Falls back to `client_host` (or "unknown") whenever the
    peer isn't trusted, the header is absent, or either fails to parse.
    """
    if not client_host:
        return "unknown"

    if not forwarded_for or not forwarded_for.strip():
        return client_host

    try:
        peer = ipaddress.ip_address(client_host)
    except ValueError:
        return client_host

    for cidr in trusted_proxy_cidrs.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if peer in network:
            original = forwarded_for.split(",")[0].strip()
            return original or client_host

    return client_host
