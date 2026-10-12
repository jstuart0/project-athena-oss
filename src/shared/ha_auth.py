"""The one gate every Home Assistant request passes before it is sent.

Home Assistant counts every failed sign-in per source address, never decays the
count, and only a WebSocket ``auth_ok`` resets it. With IP banning on, a rejected
token that Athena keeps using bans the address. This module keeps the number of
failed sign-ins that automatic traffic can cause, summed over every caller and
replica, at ``BUDGET`` until a sign-in succeeds or an operator saves the key.

How a request is admitted (``HAAuthLatch.admit``, first match wins):

1. a **latch** (a rejected token) withholds everything except a probe, which is
   a dedicated token proof at +2 min and +6 h, never a user request;
2. a request key that got a 401 on a proven token is **admin_only** (a valid
   token that isn't allowed to call that service) and is withheld;
3. a digest with no live **proof** goes through the **gate**: one lease and one
   reservation against the budget, taken together in one script, then a
   handshake (``ha_ws.prove_token``) that either clears the counters or trips
   the latch;
4. an unseen route sends **one canary** first, from a smaller allowance, so
   the proof always keeps its slot;
5. a proof older than ``CANARY_AFTER`` sends a stale canary first;
6. otherwise the request is ``OPEN``.

The decision state lives in Redis: ``budget:v2`` (no TTL: Home Assistant's own
count has none), the signed ``latch`` and ``proven`` values, the ``gate`` lease,
and the ``admin_only`` and ``seen`` sets, all under ``athena:ha_auth:``. Redis
write access is inside the trust boundary: ``budget:v2`` and ``seen`` are not
signed. Without Redis (or a service key to sign with) the same rules run on
in-process state, ``MemoryRedis``, which is also what the tests' fakes extend.

"At most 3" counts automatic traffic only. It excludes the documented
exceptions, in particular (b): sends already admitted when Home Assistant revokes
a proven token all fail (up to ``INFLIGHT_CAP`` per process, 1 when the proof is
older than ``CANARY_AFTER``), and each operator save, a Redis outage and a roll
across versions have their own bounds (docs/CONFIGURATION.md, "How Athena avoids
getting banned").

Reservations are per holder: a reservation is a field ``res:<holder>`` in the
budget hash with an expiry (``RESERVATION_TTL``), so a holder that died or timed
out after reserving stops counting in ``r`` by itself, and a save drops them at
once. The budget's scripts touch keys that must share a hash slot: use a
single-node Redis (or one hash tag); on a Redis Cluster they fail and the gate
fails closed.

Nothing here logs a URL, a host or a token.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import structlog

from shared.ha_credentials import legacy_marker_digest

logger = structlog.get_logger(__name__)

PREFIX = "athena:ha_auth:"
LEGACY_MARKER_PREFIX = "athena:ha_registry:refused:"

BUDGET = 3
REQUEST_ALLOWANCE = BUDGET - 1
PROOF_TIMEOUT = 8.0
GATE_LEASE = 10.0
GATE_WAIT = 5.0
PROVEN_TTL = 600
CANARY_AFTER = 10.0
INFLIGHT_CAP = 18
PROBE_SCHEDULE_SECONDS = (120, 21600)
RESERVATION_TTL = 120.0
ADMIN_ONLY_TTL = 86400
SEEN_TTL = 2_592_000
LATCH_TTL = 86400
COUNT_CAP = 1_000_000
POLL_INTERVAL = 0.1
REFRESH_MIN_GAP = 1.0
MAX_NEXT_AHEAD_SECONDS = 7 * 86400
MAX_REQUEST_KEY = 512

OPEN = "open"
CANARY = "canary"
PROVE = "prove"
WITHHELD = "withheld"
GATED = "gated"

REASON_LATCHED = "latched"
REASON_ADMIN_ONLY = "admin_only"
REASON_BUDGET = "budget"

RESULTS = frozenset({
    "proof_ok_ws", "proof_ok_rest", "passed", "auth_rejected", "banned",
    "unreachable_unsent", "unreachable_sent", "other",
})

_ENTITY_PATH = re.compile(r"/api/states/[a-z0-9_]+\.[a-z0-9_]+")
_INSTANCE_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def is_token_proof(method: str, path: str) -> bool:
    """A request any valid token may make: the reads whose 401 can only mean the
    token is bad. (The WebSocket handshake is the other kind of proof.)"""
    if method.upper() != "GET":
        return False
    return path in ("/api", "/api/", "/api/states") or bool(_ENTITY_PATH.fullmatch(path))


def route_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def request_key(method: str, path: str, body: Any = None) -> str:
    """The identity of one request for ``admin_only``: method, path and the
    sorted ``entity_id`` values of a JSON body, capped at 512 characters."""
    ids: List[str] = []
    if isinstance(body, Mapping):
        raw = body.get("entity_id")
        values = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
        ids = sorted(str(v) for v in values)
    key = route_key(method, path) + (" " + ",".join(ids) if ids else "")
    if len(key) > MAX_REQUEST_KEY:
        suffix = hashlib.sha256(key.encode()).hexdigest()[:16]
        key = key[: MAX_REQUEST_KEY - 17] + "#" + suffix
    return key


def instance_id() -> str:
    """This process's name for lease ownership and logs; never for counting."""
    raw = os.getenv("HOSTNAME") or socket.gethostname() or "unknown"
    return _INSTANCE_UNSAFE.sub("_", raw)[:64] or "unknown"


# ---------------------------------------------------------------------------
# The scripts. Each touches only the keys passed in KEYS. ``MemoryRedis`` runs a
# Python mirror of each by identity, and the real-Redis tier runs one scenario
# table against both.
# ---------------------------------------------------------------------------

_LUA_HELPERS = """
local function num(v)
  local x = tonumber(v)
  if x == nil or x < 0 then return 0 end
  return math.floor(x)
end
-- A reservation is a field 'res:<holder>' in the budget hash holding its expiry
-- (ms). 'r' is the number of live ones; an expired one (a process died or timed
-- out after reserving) stops counting, so r can't stay stuck.
local function sweep(budget_key, now)
  local all = redis.call('HGETALL', budget_key)
  local live = 0
  for i = 1, #all, 2 do
    if string.sub(all[i], 1, 4) == 'res:' then
      if num(all[i + 1]) <= now then redis.call('HDEL', budget_key, all[i]) else live = live + 1 end
    end
  end
  redis.call('HSET', budget_key, 'r', live)
  return live
end
local function clear_reservations(budget_key)
  local gone = 0
  local fields = redis.call('HKEYS', budget_key)
  for i = 1, #fields do
    if string.sub(fields[i], 1, 4) == 'res:' then
      redis.call('HDEL', budget_key, fields[i])
      gone = gone + 1
    end
  end
  redis.call('HSET', budget_key, 'r', 0)
  return gone
end
"""

RESERVE_PROOF = _LUA_HELPERS + """
-- KEYS: budget, gate, latch, proven.
-- ARGV: holder, lease_ms, budget, now_iso, latch_as_read, proven_as_read, now_ms, reservation_ttl_ms.
-- Returns {1, epoch} reserved, {0, 0} lease held, {2, epoch} budget spent, {3, 0}
-- the latch or the proof changed since the caller read them (read again).
local latch = redis.call('GET', KEYS[3])
if not latch then latch = '' end
local proven = redis.call('GET', KEYS[4])
if not proven then proven = '' end
if latch ~= ARGV[5] or proven ~= ARGV[6] then
  return {3, 0}
end
if not redis.call('SET', KEYS[2], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return {0, 0}
end
local live = sweep(KEYS[1], tonumber(ARGV[7]))
local n = num(redis.call('HGET', KEYS[1], 'n'))
local epoch = num(redis.call('HGET', KEYS[1], 'epoch'))
local budget = tonumber(ARGV[3])
if n >= budget then
  redis.call('DEL', KEYS[2])
  return {2, epoch}
end
redis.call('HSET', KEYS[1], 'n', n + 1)
redis.call('HSET', KEYS[1], 'res:' .. ARGV[1], tonumber(ARGV[7]) + tonumber(ARGV[8]))
redis.call('HSET', KEYS[1], 'r', live + 1)
if n + 1 >= budget and redis.call('HEXISTS', KEYS[1], 'spent_at') == 0 then
  redis.call('HSET', KEYS[1], 'spent_at', ARGV[4])
end
return {1, epoch}
"""

RESERVE_REQUEST = _LUA_HELPERS + """
-- KEYS: budget. ARGV: allowance, holder, now_ms, reservation_ttl_ms.
local r = sweep(KEYS[1], tonumber(ARGV[3]))
local n = num(redis.call('HGET', KEYS[1], 'n'))
local epoch = num(redis.call('HGET', KEYS[1], 'epoch'))
local allowance = tonumber(ARGV[1])
if n >= allowance then
  if n - r >= allowance then return {2, epoch} end
  return {0, epoch}
end
redis.call('HSET', KEYS[1], 'n', n + 1)
redis.call('HSET', KEYS[1], 'res:' .. ARGV[2], tonumber(ARGV[3]) + tonumber(ARGV[4]))
redis.call('HSET', KEYS[1], 'r', r + 1)
return {1, epoch}
"""

SETTLE = _LUA_HELPERS + """
-- KEYS: budget, proven, seen, admin_only, latch, gate, callers, legacy.
-- ARGV: op, epoch, reserved, holder, caller, now_iso, route, request_key,
--       admitted_gen, new_proven, proven_ttl, seen_ttl, admin_only_ttl,
--       latch_value, latch_ttl, legacy_ttl, count_cap, budget, add_seen,
--       reservation, now_ms.
local op = ARGV[1]
local budget = tonumber(ARGV[18])
local same = (num(redis.call('HGET', KEYS[1], 'epoch')) == tonumber(ARGV[2]))
local reserved = (ARGV[3] == '1')

local function drop_spent()
  if num(redis.call('HGET', KEYS[1], 'n')) < budget then
    redis.call('HDEL', KEYS[1], 'spent_at')
  end
end
local function dec_r()
  if ARGV[20] ~= '' then redis.call('HDEL', KEYS[1], 'res:' .. ARGV[20]) end
  sweep(KEYS[1], tonumber(ARGV[21]))
end
local function refund()
  if reserved and same then
    local n = num(redis.call('HGET', KEYS[1], 'n'))
    if n > 0 then redis.call('HSET', KEYS[1], 'n', n - 1) end
    dec_r()
    drop_spent()
  end
end
local function keep()
  if reserved and same then dec_r() end
end
local function fail(is_reserved)
  if is_reserved and same then
    dec_r()
  else
    local n = num(redis.call('HGET', KEYS[1], 'n')) + 1
    redis.call('HSET', KEYS[1], 'n', n)
    if n >= budget and redis.call('HEXISTS', KEYS[1], 'spent_at') == 0 then
      redis.call('HSET', KEYS[1], 'spent_at', ARGV[6])
    end
  end
end
local function release()
  if ARGV[4] ~= '' and redis.call('GET', KEYS[6]) == ARGV[4] then
    redis.call('DEL', KEYS[6])
  end
end

if op == 'passed' then
  refund()
  if ARGV[19] == '1' then
    redis.call('SADD', KEYS[3], ARGV[7])
    redis.call('EXPIRE', KEYS[3], ARGV[12])
  end
  if ARGV[10] ~= '' then
    local current = redis.call('GET', KEYS[2])
    if current then
      local ok, doc = pcall(cjson.decode, current)
      if ok and type(doc) == 'table' and doc['gen'] == ARGV[9] then
        redis.call('SET', KEYS[2], ARGV[10], 'EX', ARGV[11])
      end
    end
  end
elseif op == 'refund' then
  refund()
  release()
elseif op == 'keep' then
  keep()
  release()
elseif op == 'banned' then
  refund()
  release()
  redis.call('HSET', KEYS[7], ARGV[5] .. '|ban_at', ARGV[6])
elseif op == 'rejected' then
  fail(reserved)
  redis.call('DEL', KEYS[2])
  local count = num(redis.call('HGET', KEYS[7], ARGV[5] .. '|count'))
  if count < tonumber(ARGV[17]) then
    redis.call('HSET', KEYS[7], ARGV[5] .. '|count', count + 1)
  end
  redis.call('HSET', KEYS[7], ARGV[5] .. '|last_at', ARGV[6])
  if ARGV[14] ~= '' then
    redis.call('SET', KEYS[5], ARGV[14], 'EX', ARGV[15])
    redis.call('SET', KEYS[8], '1', 'EX', ARGV[16])
  else
    redis.call('SADD', KEYS[4], ARGV[8])
    redis.call('EXPIRE', KEYS[4], ARGV[13])
  end
  release()
elseif op == 'proof_rest' then
  refund()
  redis.call('SET', KEYS[2], ARGV[10], 'EX', ARGV[11])
  redis.call('DEL', KEYS[5], KEYS[8])
  release()
elseif op == 'adopt' then
  if redis.call('SET', KEYS[5], ARGV[14], 'NX', 'EX', ARGV[15]) then
    fail(false)
    local count = num(redis.call('HGET', KEYS[7], ARGV[5] .. '|count'))
    if count < tonumber(ARGV[17]) then
      redis.call('HSET', KEYS[7], ARGV[5] .. '|count', count + 1)
    end
    redis.call('HSET', KEYS[7], ARGV[5] .. '|last_at', ARGV[6])
  end
end
return 1
"""

CLEAR_ON_AUTH_OK = _LUA_HELPERS + """
-- KEYS: budget, callers, proven, latch, gate, legacy. ARGV: new_proven, proven_ttl.
redis.call('HSET', KEYS[1], 'n', 0)
clear_reservations(KEYS[1])
redis.call('HINCRBY', KEYS[1], 'epoch', 1)
redis.call('HDEL', KEYS[1], 'spent_at')
redis.call('DEL', KEYS[2], KEYS[4], KEYS[5], KEYS[6])
redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[2])
return num(redis.call('HGET', KEYS[1], 'epoch'))
"""

REOPEN = _LUA_HELPERS + """
-- KEYS: budget, gate. ARGV: budget, now_ms.
-- A save deletes the gate lease. With no lease held, no proof is running for this
-- digest, so reservations still on the books are leaked (a pod died, a call timed
-- out): drop them and bump the epoch so a late settle counts as unreserved.
local budget = tonumber(ARGV[1])
if redis.call('EXISTS', KEYS[2]) == 0 then
  if clear_reservations(KEYS[1]) > 0 then redis.call('HINCRBY', KEYS[1], 'epoch', 1) end
end
local r = sweep(KEYS[1], tonumber(ARGV[2]))
local n = num(redis.call('HGET', KEYS[1], 'n'))
local lowered = n
if lowered > budget - 1 then lowered = budget - 1 end
if lowered < r then lowered = r end
redis.call('HSET', KEYS[1], 'n', lowered)
if lowered < budget then redis.call('HDEL', KEYS[1], 'spent_at') end
return lowered
"""


# ---------------------------------------------------------------------------
# In-process Redis: the no-Redis state, and the base of the tests' fakes
# ---------------------------------------------------------------------------


def _num(value: Any) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


class MemoryRedis:
    """The few Redis commands this module uses, over a dict, with a Python
    mirror of each script. ``decode=False`` returns bytes like a client with
    ``decode_responses=False`` (``bytes_view`` shares the data)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, decode: bool = True, _share: Optional["MemoryRedis"] = None):
        self._clock = clock
        self._decode = decode
        self._data: Dict[str, Any] = _share._data if _share else {}
        self._expiry: Dict[str, float] = _share._expiry if _share else {}
        self.calls: List[str] = []

    def bytes_view(self) -> "MemoryRedis":
        return MemoryRedis(clock=self._clock, decode=False, _share=self)

    # -- storage helpers --------------------------------------------------

    def _purge(self, key: str) -> None:
        deadline = self._expiry.get(key)
        if deadline is not None and self._clock() >= deadline:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    def _out(self, value: Any) -> Any:
        if isinstance(value, str) and not self._decode:
            return value.encode()
        return value

    def _set_expiry(self, key: str, *, ex: Any = None, px: Any = None) -> None:
        if ex is not None:
            self._expiry[key] = self._clock() + float(ex)
        elif px is not None:
            self._expiry[key] = self._clock() + float(px) / 1000.0

    # -- commands (sync cores; the async client API wraps them) ----------

    def _raw_get(self, key):
        self._purge(key)
        value = self._data.get(key)
        return value if isinstance(value, str) else None

    def _get(self, key):
        value = self._raw_get(key)
        return self._out(value) if value is not None else None

    def _set(self, key, value, nx=False, ex=None, px=None):
        self._purge(key)
        if nx and key in self._data:
            return None
        self._data[key] = str(value)
        self._expiry.pop(key, None)
        self._set_expiry(key, ex=ex, px=px)
        return True

    def _delete(self, *keys):
        removed = 0
        for key in keys:
            self._purge(key)
            if self._data.pop(key, None) is not None:
                removed += 1
            self._expiry.pop(key, None)
        return removed

    def _exists(self, *keys):
        count = 0
        for key in keys:
            self._purge(key)
            count += key in self._data
        return count

    def _hash(self, key, create=False):
        self._purge(key)
        if key not in self._data and create:
            self._data[key] = {}
        value = self._data.get(key)
        return value if isinstance(value, dict) else None

    def _hget(self, key, field_name):
        table = self._hash(key)
        return self._out(table.get(field_name)) if table and field_name in table else None

    def _hmget(self, key, fields, *rest):
        names = list(fields) if isinstance(fields, (list, tuple)) else [fields, *rest]
        return [self._hget(key, name) for name in names]

    def _hgetall(self, key):
        table = self._hash(key) or {}
        return {(self._out(k)): self._out(v) for k, v in table.items()}

    def _hset(self, key, field_name=None, value=None, mapping=None):
        table = self._hash(key, create=True)
        pairs = dict(mapping or {})
        if field_name is not None:
            pairs[field_name] = value
        for name, item in pairs.items():
            table[name] = str(item)
        return len(pairs)

    def _hincrby(self, key, field_name, amount=1):
        table = self._hash(key, create=True)
        table[field_name] = str(int(table.get(field_name, "0")) + int(amount))
        return int(table[field_name])

    def _hdel(self, key, *fields):
        table = self._hash(key)
        removed = 0
        for name in fields:
            if table is not None and table.pop(name, None) is not None:
                removed += 1
        if table is not None and not table:
            self._data.pop(key, None)
            self._expiry.pop(key, None)
        return removed

    def _set_of(self, key, create=False):
        self._purge(key)
        if key not in self._data and create:
            self._data[key] = set()
        value = self._data.get(key)
        return value if isinstance(value, set) else None

    def _sadd(self, key, *members):
        members_set = self._set_of(key, create=True)
        before = len(members_set)
        members_set.update(str(m) for m in members)
        return len(members_set) - before

    def _sismember(self, key, member):
        members_set = self._set_of(key)
        return bool(members_set) and str(member) in members_set

    def _smembers(self, key):
        return {self._out(m) for m in (self._set_of(key) or set())}

    def _expire(self, key, seconds):
        self._purge(key)
        if key not in self._data:
            return False
        self._expiry[key] = self._clock() + float(seconds)
        return True

    def _ttl(self, key):
        self._purge(key)
        if key not in self._data:
            return -2
        deadline = self._expiry.get(key)
        return -1 if deadline is None else max(0, int(round(deadline - self._clock())))

    # -- client API -------------------------------------------------------

    async def get(self, key):
        self.calls.append("get")
        return self._get(key)

    async def set(self, key, value, nx=False, ex=None, px=None):
        self.calls.append("set")
        return self._set(key, value, nx, ex, px)

    async def delete(self, *keys):
        self.calls.append("delete")
        return self._delete(*keys)

    async def exists(self, *keys):
        self.calls.append("exists")
        return self._exists(*keys)

    async def hget(self, key, name):
        self.calls.append("hget")
        return self._hget(key, name)

    async def hmget(self, key, fields, *rest):
        self.calls.append("hmget")
        return self._hmget(key, fields, *rest)

    async def hgetall(self, key):
        self.calls.append("hgetall")
        return self._hgetall(key)

    async def ttl(self, key):
        return self._ttl(key)

    async def smembers(self, key):
        return self._smembers(key)

    async def hset(self, key, name=None, value=None, mapping=None):
        return self._hset(key, name, value, mapping)

    def pipeline(self, transaction: bool = True):
        return _MemoryPipeline(self)

    def register_script(self, source: str):
        mirror = _MIRRORS[source]

        async def run(keys: Sequence[str] = (), args: Sequence[Any] = ()):
            self.calls.append("script")
            return mirror(self, list(keys), [str(a) for a in args])

        return run

    # -- script mirrors ----------------------------------------------------

    def _count(self, budget_key, name):
        return _num(self._hash(budget_key).get(name)) if self._hash(budget_key) else 0

    def _budget_set(self, budget_key, name, value):
        self._hset(budget_key, name, value)


class _MemoryPipeline:
    def __init__(self, store: MemoryRedis):
        self._store = store
        self._queue: List[Tuple[str, tuple]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _enqueue(self, name: str, *args):
        self._queue.append((name, args))
        return self

    def get(self, *a):
        return self._enqueue("_get", *a)

    def hmget(self, *a):
        return self._enqueue("_hmget", *a)

    def hgetall(self, *a):
        return self._enqueue("_hgetall", *a)

    def sismember(self, *a):
        return self._enqueue("_sismember", *a)

    def exists(self, *a):
        return self._enqueue("_exists", *a)

    async def execute(self):
        self._store.calls.append("pipeline")
        out = [getattr(self._store, name)(*args) for name, args in self._queue]
        self._queue.clear()
        return out


def _mirror_sweep(store: MemoryRedis, budget_key, now) -> int:
    """Drop expired ``res:<holder>`` reservations; ``r`` becomes the live count."""
    table = store._hash(budget_key, create=True)
    live = 0
    for name in [f for f in table if str(f).startswith("res:")]:
        if _num(table[name]) <= int(now):
            del table[name]
        else:
            live += 1
    store._budget_set(budget_key, "r", live)
    return live


def _mirror_clear_reservations(store: MemoryRedis, budget_key) -> int:
    table = store._hash(budget_key, create=True)
    gone = [f for f in table if str(f).startswith("res:")]
    for name in gone:
        del table[name]
    store._budget_set(budget_key, "r", 0)
    return len(gone)


def _mirror_reserve_proof(store: MemoryRedis, keys, args):
    budget_key, gate, latch_key, proven_key = keys
    holder, lease_ms, budget, now_iso, latch_as_read, proven_as_read, now_ms, ttl_ms = args
    if (store._raw_get(latch_key) or "") != latch_as_read or (store._raw_get(proven_key) or "") != proven_as_read:
        return [3, 0]
    if not store._set(gate, holder, nx=True, px=lease_ms):
        return [0, 0]
    live = _mirror_sweep(store, budget_key, now_ms)
    n, epoch = store._count(budget_key, "n"), store._count(budget_key, "epoch")
    if n >= int(budget):
        store._delete(gate)
        return [2, epoch]
    store._budget_set(budget_key, "n", n + 1)
    store._budget_set(budget_key, f"res:{holder}", int(now_ms) + int(ttl_ms))
    store._budget_set(budget_key, "r", live + 1)
    table = store._hash(budget_key)
    if n + 1 >= int(budget) and "spent_at" not in table:
        store._hset(budget_key, "spent_at", now_iso)
    return [1, epoch]


def _mirror_reserve_request(store: MemoryRedis, keys, args):
    (budget_key,), allowance, holder, now_ms, ttl_ms = keys, int(args[0]), args[1], int(args[2]), int(args[3])
    r = _mirror_sweep(store, budget_key, now_ms)
    n, epoch = store._count(budget_key, "n"), store._count(budget_key, "epoch")
    if n >= allowance:
        return [2, epoch] if n - r >= allowance else [0, epoch]
    store._budget_set(budget_key, "n", n + 1)
    store._budget_set(budget_key, f"res:{holder}", now_ms + ttl_ms)
    store._budget_set(budget_key, "r", r + 1)
    return [1, epoch]


def _mirror_settle(store: MemoryRedis, keys, args):
    budget_k, proven_k, seen_k, admin_k, latch_k, gate_k, callers_k, legacy_k = keys
    (op, epoch, reserved_flag, holder, caller, now_iso, route, req_key, admitted_gen, new_proven,
     proven_ttl, seen_ttl, admin_ttl, latch_value, latch_ttl, legacy_ttl, count_cap, budget, add_seen,
     reservation, now_ms) = args
    budget = int(budget)
    same = store._count(budget_k, "epoch") == int(epoch)
    reserved = reserved_flag == "1"

    def count(name):
        return store._count(budget_k, name)

    def drop_spent():
        if count("n") < budget and store._hash(budget_k) is not None:
            store._hdel(budget_k, "spent_at")

    def dec_r():
        if reservation != "":
            table = store._hash(budget_k)
            if table is not None:
                table.pop(f"res:{reservation}", None)
        _mirror_sweep(store, budget_k, now_ms)

    def refund():
        if reserved and same:
            n = count("n")
            if n > 0:
                store._budget_set(budget_k, "n", n - 1)
            dec_r()
            drop_spent()

    def keep(is_reserved=None):
        if (reserved if is_reserved is None else is_reserved) and same:
            dec_r()

    def fail(is_reserved=None):
        if (reserved if is_reserved is None else is_reserved) and same:
            keep(True)
        else:
            n = count("n") + 1
            store._budget_set(budget_k, "n", n)
            if n >= budget and "spent_at" not in store._hash(budget_k):
                store._hset(budget_k, "spent_at", now_iso)

    def release():
        if holder != "" and store._raw_get(gate_k) == holder:
            store._delete(gate_k)

    def bump_caller():
        current = _num((store._hash(callers_k) or {}).get(f"{caller}|count"))
        if current < int(count_cap):
            store._hset(callers_k, f"{caller}|count", current + 1)
        store._hset(callers_k, f"{caller}|last_at", now_iso)

    if op == "passed":
        refund()
        if add_seen == "1":
            store._sadd(seen_k, route)
            store._expire(seen_k, seen_ttl)
        if new_proven != "":
            current = store._raw_get(proven_k)
            if current is not None:
                try:
                    doc = json.loads(current)
                except ValueError:
                    doc = None
                if isinstance(doc, dict) and doc.get("gen") == admitted_gen:
                    store._set(proven_k, new_proven, ex=proven_ttl)
    elif op == "refund":
        refund()
        release()
    elif op == "keep":
        keep()
        release()
    elif op == "banned":
        refund()
        release()
        store._hset(callers_k, f"{caller}|ban_at", now_iso)
    elif op == "rejected":
        fail()
        store._delete(proven_k)
        bump_caller()
        if latch_value != "":
            store._set(latch_k, latch_value, ex=latch_ttl)
            store._set(legacy_k, "1", ex=legacy_ttl)
        else:
            store._sadd(admin_k, req_key)
            store._expire(admin_k, admin_ttl)
        release()
    elif op == "proof_rest":
        refund()
        store._set(proven_k, new_proven, ex=proven_ttl)
        store._delete(latch_k, legacy_k)
        release()
    elif op == "adopt":
        if store._set(latch_k, latch_value, nx=True, ex=latch_ttl):
            fail(False)
            bump_caller()
    return 1


def _mirror_clear_on_auth_ok(store: MemoryRedis, keys, args):
    budget_k, callers_k, proven_k, latch_k, gate_k, legacy_k = keys
    new_proven, proven_ttl = args
    store._budget_set(budget_k, "n", 0)
    _mirror_clear_reservations(store, budget_k)
    store._hincrby(budget_k, "epoch", 1)
    store._hdel(budget_k, "spent_at")
    store._delete(callers_k, latch_k, gate_k, legacy_k)
    store._set(proven_k, new_proven, ex=proven_ttl)
    return store._count(budget_k, "epoch")


def _mirror_reopen(store: MemoryRedis, keys, args):
    (budget_k, gate_k), budget, now_ms = keys, int(args[0]), int(args[1])
    if not store._exists(gate_k):
        if _mirror_clear_reservations(store, budget_k) > 0:
            store._hincrby(budget_k, "epoch", 1)
    r = _mirror_sweep(store, budget_k, now_ms)
    n = store._count(budget_k, "n")
    lowered = max(r, min(n, budget - 1))
    store._budget_set(budget_k, "n", lowered)
    if lowered < budget:
        store._hdel(budget_k, "spent_at")
    return lowered


_MIRRORS = {
    RESERVE_PROOF: _mirror_reserve_proof,
    RESERVE_REQUEST: _mirror_reserve_request,
    SETTLE: _mirror_settle,
    CLEAR_ON_AUTH_OK: _mirror_clear_on_auth_ok,
    REOPEN: _mirror_reopen,
}


# ---------------------------------------------------------------------------
# Decoding and signing
# ---------------------------------------------------------------------------


def _decode(value: Any) -> Any:
    """str or bytes or int (or a list of them) as str/int: one reading for a
    client with ``decode_responses`` on and off."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        return {_decode(k): _decode(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return {_decode(v) for v in value}
    return value


def _count_value(value: Any) -> int:
    """A stored count as an int in ``[0, COUNT_CAP]``; anything else is 0."""
    value = _decode(value)
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value) if isinstance(value, int) else int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return min(max(parsed, 0), COUNT_CAP)


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _truthy(value: Any) -> bool:
    value = _decode(value)
    if isinstance(value, str):
        return value not in ("", "0")
    return bool(value)


@dataclass(frozen=True)
class _Latch:
    at: int
    step: int
    next: Optional[int]


@dataclass(frozen=True)
class _Proven:
    gen: str
    at: int


@dataclass
class _State:
    latch: Optional[_Latch]
    proven: Optional[_Proven]
    n: int
    r: int
    epoch: int
    spent_at: Optional[str]
    admin_only: bool
    seen: bool
    legacy: bool
    gate_held: bool
    latch_raw: Any = None
    proven_raw: Any = None

    @property
    def signature(self) -> Tuple[Any, ...]:
        return (self.latch_raw, self.proven_raw, self.gate_held, self.n, self.r, self.epoch)


@dataclass(frozen=True)
class Admission:
    """The decision for one request. ``kind`` is ``OPEN`` (send), ``CANARY``
    (send; it goes first), ``PROVE`` (run ``prove_token`` before anything),
    ``WITHHELD`` (don't send; ``reason`` says why) or ``GATED`` (couldn't decide
    in time; don't send). The rest is what ``observe`` needs to settle it."""

    kind: str
    reason: Optional[str] = None
    epoch: int = 0
    gen: Optional[str] = None
    reserved: bool = False
    proof_request: bool = False
    probe: bool = False
    latch_step: int = 0
    latch_at: int = 0
    holder: str = ""
    reservation: str = ""
    route: str = ""
    canary: Optional[Tuple[str, Any]] = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"Admission(kind={self.kind!r}, reason={self.reason!r}, epoch={self.epoch}, reserved={self.reserved})"


_WAIT = object()
_RESTART = object()
MAX_RESTARTS = 5


class _Waiter:
    def __init__(self):
        self.event = asyncio.Event()
        self.count = 0
        self.task: Optional["asyncio.Task[None]"] = None


class HAAuthLatch:
    """Admission, settlement and the cluster-wide failed sign-in budget for one
    process. See the module docstring for the rules; this class hides the key
    layout, signing, scripts, gate, canaries, waiters, the probe schedule and
    the in-flight cap.

    ``admit`` is called before a send and ``observe`` exactly once after it;
    neither raises (apart from cancellation). A held ``slot()`` spans the send.
    """

    def __init__(
        self,
        redis: Any,
        *,
        instance: str,
        service_api_key: Optional[str],
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        redis_timeout: float = 0.25,
        sleep: Callable[[float], Any] = asyncio.sleep,
        gate_lease: float = GATE_LEASE,
        gate_wait: float = GATE_WAIT,
        canary_after: float = CANARY_AFTER,
        proven_ttl: int = PROVEN_TTL,
        inflight_cap: int = INFLIGHT_CAP,
        schedule: Sequence[int] = PROBE_SCHEDULE_SECONDS,
        poll_interval: float = POLL_INTERVAL,
        refresh_gap: float = REFRESH_MIN_GAP,
    ):
        self._shared = bool(service_api_key) and redis is not None
        if not self._shared and os.environ.get("REDIS_URL"):
            # The failed sign-in budget is then per process, not per cluster.
            logger.warning(
                "ha_auth_not_shared", has_redis=redis is not None, has_service_key=bool(service_api_key),
            )
        self._redis = redis if self._shared else MemoryRedis()
        self._key = (service_api_key or uuid.uuid4().hex).encode()
        self._service_api_key = service_api_key or None
        self._instance = instance
        self._wall = wall
        self._mono = monotonic
        self._redis_timeout = redis_timeout
        self._sleep = sleep
        self._gate_lease = gate_lease
        self._gate_wait = gate_wait
        self._canary_after = canary_after
        self._proven_ttl = proven_ttl
        self._schedule = tuple(schedule)
        self._poll_interval = poll_interval
        self._refresh_gap = refresh_gap
        self._slots = asyncio.Semaphore(inflight_cap)
        self._scripts = {
            name: self._redis.register_script(source)
            for name, source in (
                ("reserve_proof", RESERVE_PROOF), ("reserve_request", RESERVE_REQUEST),
                ("settle", SETTLE), ("clear_on_auth_ok", CLEAR_ON_AUTH_OK), ("reopen", REOPEN),
            )
        }
        self._local_proof: Dict[str, Tuple[str, int]] = {}
        # A real rejection whose settlement didn't reach Redis: the proof it ended
        # may still be stored there, so nothing for this digest is admitted OPEN
        # until the settlement has been retried.
        self._unsettled: Dict[str, List[Tuple[Admission, str, str, str]]] = {}
        self._retrying: Set[str] = set()
        self._local_seen: Set[Tuple[str, str]] = set()
        self._route_canaries: Set[Tuple[str, str]] = set()
        self._stale_canaries: Set[str] = set()
        self._last_refresh: Dict[str, float] = {}
        self._waiters: Dict[str, _Waiter] = {}
        self._summaries: Dict[str, Dict[str, Any]] = {}
        self._warned_redis = False

    @property
    def shared(self) -> bool:
        """True when state is shared through Redis (a client and a service key)."""
        return self._shared

    # -- keys and values --------------------------------------------------

    def _k(self, name: str, digest: str = "") -> str:
        return f"{PREFIX}{name}:{digest}" if digest else f"{PREFIX}{name}"

    def _legacy_key(self, snapshot: Any) -> str:
        suffix = legacy_marker_digest(snapshot.token.reveal(), self._service_api_key) or "none"
        return LEGACY_MARKER_PREFIX + suffix

    def _sign(self, *parts: Any) -> str:
        return hmac.new(self._key, "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()[:32]

    def _latch_value(self, digest: str, at: int, step: int, next_at: Optional[int]) -> str:
        return json.dumps({"v": 1, "at": at, "step": step, "next": next_at,
                           "sig": self._sign("latch", digest, at, step, next_at)})

    def _proven_value(self, digest: str, gen: str, at: int) -> str:
        return json.dumps({"v": 1, "gen": gen, "at": at, "sig": self._sign("proven", digest, gen, at)})

    def _parse_latch(self, digest: str, raw: Any) -> Optional[_Latch]:
        doc = self._load(raw)
        try:
            at, step, next_at = doc["at"], doc["step"], doc["next"]
            if not (isinstance(at, int) and isinstance(step, int) and (next_at is None or isinstance(next_at, int))):
                return None
            if isinstance(at, bool) or isinstance(step, bool) or isinstance(next_at, bool):
                return None
            if not hmac.compare_digest(str(doc["sig"]), self._sign("latch", digest, at, step, next_at)):
                return None
        except (KeyError, TypeError):
            return None
        ceiling = int((self._wall() + MAX_NEXT_AHEAD_SECONDS) * 1000)
        if next_at is not None:
            next_at = min(next_at, ceiling)
        return _Latch(at, step, next_at)

    def _parse_proven(self, digest: str, raw: Any) -> Optional[_Proven]:
        doc = self._load(raw)
        try:
            gen, at = doc["gen"], doc["at"]
            if not (isinstance(gen, str) and isinstance(at, int)) or isinstance(at, bool):
                return None
            if not hmac.compare_digest(str(doc["sig"]), self._sign("proven", digest, gen, at)):
                return None
        except (KeyError, TypeError):
            return None
        return _Proven(gen, at)

    @staticmethod
    def _load(raw: Any) -> Any:
        raw = _decode(raw)
        if not isinstance(raw, str) or len(raw) > 4096:
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None

    def _now_ms(self) -> int:
        return int(self._wall() * 1000)

    # -- redis plumbing ---------------------------------------------------

    async def _bounded(self, awaitable):
        return await asyncio.wait_for(awaitable, self._redis_timeout)

    async def _call(self, name: str, keys: Sequence[str], args: Sequence[Any]) -> Any:
        return _decode(await self._bounded(self._scripts[name](keys=list(keys), args=[str(a) for a in args])))

    def _note_redis_failure(self, exc: BaseException) -> None:
        if not self._warned_redis:
            self._warned_redis = True
            logger.warning("ha_auth_redis_unavailable", error_class=type(exc).__name__)

    async def _read(self, snapshot: Any, route: str, key: str) -> _State:
        digest = snapshot.digest
        async with self._redis.pipeline(transaction=True) as pipe:  # MULTI/EXEC: one consistent view
            pipe.get(self._k("latch:v1", digest))
            pipe.get(self._k("proven:v2", digest))
            pipe.hmget(self._k("budget:v2"), "n", "r", "epoch", "spent_at")
            pipe.sismember(self._k("admin_only:v1", digest), key)
            pipe.sismember(self._k("seen:v1", digest), route)
            pipe.exists(self._legacy_key(snapshot))
            pipe.exists(self._k("gate:v1", digest))
            raw = await self._bounded(pipe.execute())
        latch_raw, proven_raw, budget, admin_only, seen, legacy, gate = raw
        n, r, epoch, spent_at = _decode(budget)
        return _State(
            latch=self._parse_latch(digest, latch_raw),
            proven=self._parse_proven(digest, proven_raw),
            n=_count_value(n), r=_count_value(r), epoch=_count_value(epoch), spent_at=spent_at,
            admin_only=_truthy(admin_only), seen=_truthy(seen), legacy=_truthy(legacy), gate_held=_truthy(gate),
            latch_raw=_decode(latch_raw), proven_raw=_decode(proven_raw),
        )

    async def _read_signature(self, snapshot: Any) -> Tuple[Any, ...]:
        state = await self._read(snapshot, "", "")
        return state.signature

    # -- admission --------------------------------------------------------

    async def admit(self, snapshot: Any, *, method: str, path: str, key: str) -> Admission:
        """Decide one request. Never raises (a failure is ``GATED``: nothing
        is sent that could fail authentication)."""
        try:
            return await self._admit(snapshot, method, path, key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ha_auth_admit_failed", error_class=type(exc).__name__)
            return Admission(GATED)

    async def _admit(self, snapshot: Any, method: str, path: str, key: str) -> Admission:
        digest = snapshot.digest
        route = route_key(method, path)
        proof_request = method.upper() == "WS" or is_token_proof(method, path)
        deadline = self._mono() + self._gate_wait
        restarts = 0
        while True:
            wake = self._event_for(digest)
            try:
                state = await self._read(snapshot, route, key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note_redis_failure(exc)
                return dataclasses.replace(self._offline(digest, route, proof_request), route=route)
            decision = await self._decide(snapshot, state, route, key, proof_request)
            if decision is _RESTART:  # the state moved between the read and the reservation
                restarts += 1
                if restarts > MAX_RESTARTS:
                    return Admission(GATED)
                continue
            if decision is not _WAIT:
                return dataclasses.replace(decision, route=route)
            remaining = deadline - self._mono()
            if remaining <= 0 or not await self._wait(snapshot, wake, remaining, state.signature):
                return Admission(GATED)

    async def _decide(self, snapshot: Any, state: _State, route: str, key: str, proof_request: bool):
        digest = snapshot.digest
        if digest in self._retrying or self._unsettled.get(digest):
            return await self._retry_unsettled(snapshot)
        now = self._now_ms()
        latch = state.latch
        if latch is None and state.legacy:
            latch = await self._adopt_legacy(snapshot, state, now)
        if latch is not None:
            return await self._decide_latched(snapshot, state, latch, now, proof_request)
        if state.admin_only:
            return Admission(WITHHELD, REASON_ADMIN_ONLY, proof_request=proof_request)
        if state.proven is None:
            return await self._gate(snapshot, state, proof_request)
        seen = proof_request or state.seen or (digest, route) in self._local_seen
        if not seen:
            return await self._route_canary(snapshot, state, route, proof_request)
        if now - state.proven.at > self._canary_after * 1000:
            return await self._stale_canary(snapshot, state, proof_request)
        return Admission(OPEN, epoch=state.epoch, gen=state.proven.gen, proof_request=proof_request)

    async def _retry_unsettled(self, snapshot: Any):
        """Redis is back (the read just worked): record every rejection whose settlement
        was lost before anything is admitted OPEN, then read again. Each entry is taken
        out before it is retried and put back only if the retry fails, and one coroutine
        at a time does this per digest, so none is settled twice by this process."""
        digest = snapshot.digest
        if digest in self._retrying:
            return _WAIT
        self._retrying.add(digest)
        try:
            pending = self._unsettled.get(digest, [])
            while pending:
                entry = pending.pop(0)
                admission, caller, request_key, latch_value = entry
                try:
                    await self._settle(snapshot, "rejected", admission, caller=caller, request_key=request_key, latch_value=latch_value)
                except BaseException:
                    pending.insert(0, entry)
                    raise
            self._unsettled.pop(digest, None)
        finally:
            self._retrying.discard(digest)
            self._wake(digest)
        return _RESTART

    async def _decide_latched(self, snapshot: Any, state: _State, latch: _Latch, now: int, proof_request: bool):
        if latch.next is None or now < latch.next:
            return Admission(WITHHELD, REASON_LATCHED, proof_request=proof_request)
        code, epoch, holder = await self._reserve_proof(snapshot, state)
        if code == 3:
            return _RESTART
        if code == 1:
            return Admission(PROVE, epoch=epoch, reserved=True, proof_request=True, probe=True,
                             latch_step=latch.step, latch_at=latch.at, holder=holder, reservation=holder)
        if code == 2:
            return Admission(WITHHELD, REASON_BUDGET, proof_request=proof_request)
        return Admission(WITHHELD, REASON_LATCHED, proof_request=proof_request)

    async def _gate(self, snapshot: Any, state: _State, proof_request: bool):
        code, epoch, holder = await self._reserve_proof(snapshot, state)
        if code == 3:
            return _RESTART
        if code == 1:
            return Admission(PROVE, epoch=epoch, reserved=True, proof_request=True, holder=holder, reservation=holder)
        if code == 2:
            return Admission(WITHHELD, REASON_BUDGET, proof_request=proof_request)
        return _WAIT

    async def _route_canary(self, snapshot: Any, state: _State, route: str, proof_request: bool):
        digest = snapshot.digest
        marker = (digest, route)
        if marker in self._route_canaries:
            return _WAIT
        self._route_canaries.add(marker)  # before the await: the next same-route request must see it
        try:
            reservation = f"{self._instance}:{uuid.uuid4().hex[:8]}"
            result = await self._call(
                "reserve_request", [self._k("budget:v2")],
                [REQUEST_ALLOWANCE, reservation, self._now_ms(), int(RESERVATION_TTL * 1000)],
            )
            code, epoch = int(result[0]), int(result[1])
        except BaseException:
            self._route_canaries.discard(marker)
            raise
        if code == 1:
            return Admission(CANARY, epoch=epoch, gen=state.proven.gen, reserved=True, reservation=reservation,
                             proof_request=proof_request, canary=("route", marker))
        self._route_canaries.discard(marker)
        if code == 2:
            return Admission(WITHHELD, REASON_BUDGET, proof_request=proof_request)
        return _WAIT

    async def _stale_canary(self, snapshot: Any, state: _State, proof_request: bool):
        """A proof older than ``CANARY_AFTER`` (an idle spell, or a Redis outage that kept it
        from being renewed) may no longer be true. One request, cluster-wide, finds out
        first: it takes a short lease, reserves against the budget like a route canary, and
        every other request waits. A pass renews the proof for everyone; a rejection ends it."""
        digest = snapshot.digest
        if digest in self._stale_canaries:
            return _WAIT
        self._stale_canaries.add(digest)  # before the awaits: the next request of this process must see it
        holder = f"{self._instance}:{uuid.uuid4().hex[:8]}"
        try:
            if not await self._bounded(self._redis.set(
                self._k("stale:v1", digest), holder, nx=True, px=int(self._gate_lease * 1000),
            )):
                self._stale_canaries.discard(digest)
                return _WAIT
            result = await self._call(
                "reserve_request", [self._k("budget:v2")],
                [REQUEST_ALLOWANCE, holder, self._now_ms(), int(RESERVATION_TTL * 1000)],
            )
            code, epoch = int(result[0]), int(result[1])
        except BaseException:
            self._stale_canaries.discard(digest)
            raise
        if code == 1:
            return Admission(CANARY, epoch=epoch, gen=state.proven.gen, reserved=True, reservation=holder,
                             proof_request=proof_request, canary=("stale", digest))
        self._stale_canaries.discard(digest)
        if code == 2:
            return Admission(WITHHELD, REASON_BUDGET, proof_request=proof_request)
        return _WAIT

    async def _reserve_proof(self, snapshot: Any, state: _State) -> Tuple[int, int, str]:
        digest = snapshot.digest
        holder = f"{self._instance}:{uuid.uuid4().hex[:8]}"
        result = await self._call(
            "reserve_proof",
            [self._k("budget:v2"), self._k("gate:v1", digest), self._k("latch:v1", digest), self._k("proven:v2", digest)],
            [holder, int(self._gate_lease * 1000), BUDGET, _iso(self._wall()), state.latch_raw or "", state.proven_raw or "",
             self._now_ms(), int(RESERVATION_TTL * 1000)],
        )
        return int(result[0]), int(result[1]), holder

    async def _adopt_legacy(self, snapshot: Any, state: _State, now: int) -> Optional[_Latch]:
        """An old registry's ``"1"`` marker counts as a latch at step 0, and as
        one failed sign-in, once."""
        digest = snapshot.digest
        next_at = now + self._schedule[0] * 1000 if self._schedule else None
        await self._settle(snapshot, "adopt", Admission(OPEN, epoch=state.epoch), caller="legacy",
                           latch_value=self._latch_value(digest, now, 0, next_at))
        return _Latch(now, 0, next_at)

    def _offline(self, digest: str, route: str, proof_request: bool) -> Admission:
        """Redis can't be read: only a process that already holds a proof for
        this digest, on a route it has seen, keeps sending."""
        local = self._local_proof.get(digest)
        seen = proof_request or (digest, route) in self._local_seen
        if local is None or not seen:
            return Admission(GATED)
        gen, at = local
        if self._now_ms() - at > self._canary_after * 1000:
            if digest in self._stale_canaries:
                return Admission(GATED)
            self._stale_canaries.add(digest)
            return Admission(CANARY, gen=gen, proof_request=proof_request, canary=("stale", digest))
        return Admission(OPEN, gen=gen, proof_request=proof_request)

    # -- waiting ----------------------------------------------------------

    def _event_for(self, digest: str) -> asyncio.Event:
        return self._waiters.setdefault(digest, _Waiter()).event

    def _wake(self, digest: str) -> None:
        waiter = self._waiters.get(digest)
        if waiter is None:
            return
        old, waiter.event = waiter.event, asyncio.Event()
        old.set()

    async def _wait(self, snapshot: Any, event: asyncio.Event, remaining: float, baseline: Tuple[Any, ...]) -> bool:
        """True when woken, False when ``remaining`` ran out first."""
        digest = snapshot.digest
        waiter = self._waiters.setdefault(digest, _Waiter())
        waiter.count += 1
        if waiter.task is None or waiter.task.done():
            waiter.task = asyncio.ensure_future(self._poll(snapshot, baseline))
        try:
            await asyncio.wait_for(event.wait(), remaining)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            waiter.count -= 1

    async def _poll(self, snapshot: Any, baseline: Tuple[Any, ...]) -> None:
        """One reader per digest while anyone waits: wake the waiters when the
        shared state changes, instead of every waiter reading Redis."""
        digest = snapshot.digest
        waiter = self._waiters[digest]
        last = baseline
        try:
            while waiter.count > 0:
                await self._sleep(self._poll_interval)
                try:
                    current = await self._read_signature(snapshot)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                if current != last:
                    last = current
                    self._wake(digest)
        finally:
            waiter.task = None

    # -- the in-flight cap --------------------------------------------------

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """One of this process's ``INFLIGHT_CAP`` places for a send. It is taken
        before ``admit``, so a request that queues here is admitted only after
        the earlier ones have settled."""
        async with self._slots:
            yield

    # -- settlement -------------------------------------------------------

    async def observe(self, snapshot: Any, admission: Admission, *, caller: str, key: str, result: str) -> None:
        """Settle one admitted send. Call exactly once per ``admit`` that
        returned ``OPEN``, ``CANARY`` or ``PROVE``, in a ``finally``. Never raises."""
        digest = snapshot.digest
        try:
            await self._observe(snapshot, admission, caller, key, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_redis_failure(exc)
        finally:
            self._release_canary(admission)
            self._wake(digest)

    def _release_canary(self, admission: Admission) -> None:
        if admission.canary is None:
            return
        kind, token = admission.canary
        (self._stale_canaries if kind == "stale" else self._route_canaries).discard(token)

    async def _observe(self, snapshot: Any, admission: Admission, caller: str, key: str, result: str) -> None:
        digest = snapshot.digest
        now = self._now_ms()
        route = admission.route
        if result == "proof_ok_ws":
            self._record_proof(digest, uuid.uuid4().hex[:16], now)
            await self._clear_on_auth_ok(snapshot)
        elif result == "proof_ok_rest":
            gen = uuid.uuid4().hex[:16]
            self._record_proof(digest, gen, now)
            await self._settle(snapshot, "proof_rest", admission, caller=caller, new_proven=self._proven_value(digest, gen, now))
        elif result == "passed":
            await self._settle_passed(snapshot, admission, caller, route, now)
        elif result == "auth_rejected":
            self._local_proof.pop(digest, None)
            latch_value = self._trip_value(digest, admission, now)
            try:
                await self._settle(snapshot, "rejected", admission, caller=caller, request_key=key, latch_value=latch_value)
            except BaseException:
                # Every rejection whose settlement may not have reached Redis is kept, one entry each.
                self._unsettled.setdefault(digest, []).append((admission, caller, key, latch_value))
                raise
        elif result == "banned":
            await self._settle(snapshot, "banned", admission, caller=caller)
        elif result == "unreachable_sent":
            await self._settle(snapshot, "keep", admission, caller=caller)
        elif admission.reserved or admission.holder:
            await self._settle(snapshot, "refund", admission, caller=caller)

    def _record_proof(self, digest: str, gen: str, now: int) -> None:
        self._local_proof[digest] = (gen, now)

    def _trip_value(self, digest: str, admission: Admission, now: int) -> str:
        """The latch a rejected proof writes; empty for a request that doesn't
        decide the token (its 401 is ``admin_only``)."""
        if not admission.proof_request:
            return ""
        step, at = (admission.latch_step + 1, admission.latch_at) if admission.probe else (0, now)
        next_at = at + self._schedule[step] * 1000 if step < len(self._schedule) else None
        return self._latch_value(digest, at, step, next_at)

    async def _settle_passed(self, snapshot: Any, admission: Admission, caller: str, route: str, now: int) -> None:
        digest = snapshot.digest
        add_seen = (digest, route) not in self._local_seen
        self._local_seen.add((digest, route))
        refresh = ""
        if admission.gen is not None:
            self._local_proof[digest] = (admission.gen, now)
            last = self._last_refresh.get(digest)
            if admission.canary is not None or last is None or self._mono() - last >= self._refresh_gap:
                self._last_refresh[digest] = self._mono()
                refresh = self._proven_value(digest, admission.gen, now)
        if not (admission.reserved or add_seen or refresh):
            return
        await self._settle(snapshot, "passed", admission, caller=caller, route=route,
                           add_seen=add_seen, new_proven=refresh)

    async def _settle(
        self, snapshot: Any, op: str, admission: Admission, *, caller: str,
        route: str = "", request_key: str = "", latch_value: str = "", new_proven: str = "", add_seen: bool = False,
    ) -> None:
        digest = snapshot.digest
        keys = [
            self._k("budget:v2"), self._k("proven:v2", digest), self._k("seen:v1", digest),
            self._k("admin_only:v1", digest), self._k("latch:v1", digest), self._k("gate:v1", digest),
            self._k("callers:v2"), self._legacy_key(snapshot),
        ]
        args = [
            op, admission.epoch, "1" if admission.reserved else "0", admission.holder, caller, _iso(self._wall()),
            route, request_key, admission.gen or "", new_proven, self._proven_ttl, SEEN_TTL, ADMIN_ONLY_TTL,
            latch_value, LATCH_TTL, LATCH_TTL, COUNT_CAP, BUDGET, "1" if add_seen else "0",
            admission.reservation, self._now_ms(),
        ]
        await self._call("settle", keys, args)

    async def _clear_on_auth_ok(self, snapshot: Any) -> None:
        digest = snapshot.digest
        gen, now = uuid.uuid4().hex[:16], self._now_ms()
        self._record_proof(digest, gen, now)
        await self._call(
            "clear_on_auth_ok",
            [self._k("budget:v2"), self._k("callers:v2"), self._k("proven:v2", digest), self._k("latch:v1", digest),
             self._k("gate:v1", digest), self._legacy_key(snapshot)],
            [self._proven_value(digest, gen, now), self._proven_ttl],
        )

    # -- operator save ----------------------------------------------------

    async def clear_and_reopen(self, snapshot: Any) -> None:
        """An operator saved the key: forget the stop for this digest and let
        exactly one gated attempt through. The counter only drops to
        ``BUDGET - 1`` (Home Assistant still holds those failures), and the
        per-caller display isn't touched. Never raises."""
        digest = snapshot.digest
        self._local_proof.pop(digest, None)
        self._local_seen = {item for item in self._local_seen if item[0] != digest}
        try:
            await self._bounded(self._redis.delete(
                self._k("latch:v1", digest), self._k("proven:v2", digest), self._k("gate:v1", digest),
                self._k("admin_only:v1", digest), self._k("seen:v1", digest), self._legacy_key(snapshot),
                self._k("stale:v1", digest),
            ))
            await self._call("reopen", [self._k("budget:v2"), self._k("gate:v1", digest)], [BUDGET, self._now_ms()])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_redis_failure(exc)
        self._wake(digest)

    # -- the display summary ----------------------------------------------

    async def sync(self, snapshot: Any) -> None:
        """Refresh what ``summary`` returns, for the publisher's cycle. Never raises."""
        digest = snapshot.digest
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.get(self._k("latch:v1", digest))
                pipe.hmget(self._k("budget:v2"), "n", "spent_at")
                pipe.hgetall(self._k("callers:v2"))
                latch_raw, budget, callers = await self._bounded(pipe.execute())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_redis_failure(exc)
            return
        self._summaries[digest] = self._build_summary(digest, latch_raw, _decode(budget), _decode(callers or {}))

    def _build_summary(self, digest: str, latch_raw: Any, budget: Sequence[Any], callers: Mapping[str, Any]) -> Dict[str, Any]:
        latch = self._parse_latch(digest, latch_raw)
        n, spent_at = _count_value(budget[0]), budget[1]
        failures: Dict[str, Dict[str, Any]] = {}
        banned_at: Optional[str] = None
        for name, value in callers.items():
            caller, _, field_name = str(name).partition("|")
            if field_name == "count" and _count_value(value) > 0:
                failures.setdefault(caller, {})["count"] = _count_value(value)
            elif field_name == "last_at":
                failures.setdefault(caller, {})["last_at"] = value if isinstance(value, str) else None
            elif field_name == "ban_at" and isinstance(value, str) and (banned_at is None or value > banned_at):
                banned_at = value
        failures = {c: {"count": f["count"], "last_at": f.get("last_at")} for c, f in failures.items() if "count" in f}
        spent = n >= BUDGET
        next_at = None
        if latch is not None and latch.next is not None and not spent:
            next_at = _iso(latch.next / 1000)
        return {
            "latched": latch is not None,
            "latched_at": _iso(latch.at / 1000) if latch is not None else None,
            "next_probe_at": next_at,
            "banned_at": banned_at,
            "failures": failures,
            "budget_spent": spent,
            "budget_spent_at": spent_at if spent and isinstance(spent_at, str) else None,
        }

    def summary(self, snapshot: Any) -> Dict[str, Any]:
        """The last synced auth facts for ``snapshot``'s digest: synchronous,
        no I/O."""
        known = self._summaries.get(snapshot.digest)
        if known is not None:
            return dict(known)
        return self._build_summary(snapshot.digest, None, (0, None), {})
