"""jarvis-web's per-session counters are bounded (xander L6).

Every chat turn records its session id; without bounds a stream of fresh
ids grows the process forever. Idle entries expire, and past the cap the
least recently used entry goes.
"""
from __future__ import annotations

from . import _jarvis_web_harness as h


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_lru_cap_evicts_least_recently_used():
    counters = h.main.SessionCounters(max_entries=3, ttl_seconds=3600, clock=_Clock())
    for sid in ("a", "b", "c"):
        counters.record_message(sid)
    counters.record_message("a")
    counters.record_message("d")
    assert len(counters) == 3
    assert counters.get("b") is None
    assert counters.get("a")["message_count"] == 2


def test_idle_entries_expire():
    clock = _Clock()
    counters = h.main.SessionCounters(max_entries=100, ttl_seconds=60, clock=clock)
    counters.record_message("old")
    clock.now += 30
    counters.record_message("fresh")
    clock.now += 31
    assert counters.get("old") is None
    assert counters.get("fresh")["message_count"] == 1
    assert "_seen" not in counters.get("fresh")


def test_many_chats_stay_bounded(monkeypatch):
    """Named: the app's own store holds at most its cap across chat turns."""
    h.configure()
    h.install_outbound(monkeypatch)
    monkeypatch.setattr(h.main, "sessions", h.main.SessionCounters(max_entries=5))
    c = h.client()
    for _ in range(8):
        c.cookies.clear()
        assert c.post("/api/chat", json={"message": "hi"}, headers={**h.via_proxy(h.LAN), **h.CSRF}).status_code == 200
    assert len(h.main.sessions) == 5
    h.configure()
