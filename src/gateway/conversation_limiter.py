"""Per-source new-conversation rate limiter (ATHENA-88 / F88 D4).

The gateway is the only hop that sees the real client address for HA
traffic (the orchestrator sees the gateway pod IP for everything). This
limiter bounds how many *new* conversations (first-turn requests with no
explicit session_id) a single source can start per minute, independent of
the existing global_rate_limiter (which is a single global bucket, not
per-source, and isn't consulted on /v1/responses).

Hides: the sliding window, the clock, and the LRU key bound. Callers only
see allow(key) -> bool and len(limiter).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Dict, List


class NewConversationLimiter:
    """Sliding 60s per-key window, LRU-bounded on the number of distinct keys."""

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

    def allow(self, key: str) -> bool:
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
