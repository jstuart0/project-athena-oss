"""The sender: first boot and heartbeat, cadence and backoff, the lease and
its re-checks, identity and the endpoint-bound wire key, opt-outs read from
the process env and `.env`, error redaction, reset, and the startup log."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
from datetime import datetime, timedelta

import httpx
import pytest
from structlog.testing import capture_logs

from app.models import SystemSetting
from app.services import settings_lease
from app.services.telemetry import classify, sender
from shared.config import TELEMETRY_DEFAULT_ENDPOINT
from tests._telemetry_support import FakeClock, Recorder, settings
from tests.conftest import TestingSessionLocal

RESET_KEYS = (
    "telemetry.installation_id", "telemetry.install_key", "telemetry.installation_created_at",
    "telemetry.provenance", "telemetry.first_boot_sent_at", "telemetry.last_attempt_at",
    "telemetry.last_success_at", "telemetry.next_due_at", "telemetry.consecutive_failures",
    "telemetry.last_error", "telemetry.last_payload",
)


@pytest.fixture
def harness(telemetry_env, db, monkeypatch):
    clock = FakeClock()
    recorder = Recorder()
    monkeypatch.setattr(sender, "CLOCK", clock)
    monkeypatch.setattr(sender, "TRANSPORT", recorder.transport)
    return clock, recorder


def cycle(force=False):
    return asyncio.run(sender.run_cycle(force=force))


def _state():
    return settings(TestingSessionLocal)


def _when(key):
    value = _state().get(key)
    return datetime.fromisoformat(value) if value else None


def _put(key, value):
    session = TestingSessionLocal()
    try:
        row = session.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is None:
            session.add(SystemSetting(key=key, value=value, category="telemetry"))
        else:
            row.value = value
        session.commit()
    finally:
        session.close()


def _hold_foreign_lease(expires_at):
    _put(sender.LEASE_KEY, json.dumps({"holder": "other-replica/1", "expires_at": expires_at.isoformat()}))


def _wire_key(install_key, endpoint):
    raw = base64.urlsafe_b64decode(install_key + "=" * (-len(install_key) % 4))
    digest = hmac.new(raw, classify.endpoint_origin(endpoint).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# First boot, heartbeat, cadence
# ---------------------------------------------------------------------------

def test_first_cycle_sends_first_boot(harness):
    clock, rec = harness
    assert cycle() == "sent"
    assert len(rec.requests) == 1
    request = rec.requests[0]
    body = json.loads(request.content)
    assert body["event"] == "first_boot"
    assert body["installation_id"] == _state()["telemetry.installation_id"]
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("athena-telemetry/")
    assert len(request.headers["x-athena-install-key"]) == 43
    assert _state()["telemetry.last_payload"].encode() == request.content
    assert str(request.url) == TELEMETRY_DEFAULT_ENDPOINT


def test_forced_second_cycle_is_heartbeat(harness):
    clock, rec = harness
    cycle()
    assert cycle(force=True) == "sent"
    assert [p["event"] for p in rec.payloads()] == ["first_boot", "heartbeat"]
    assert rec.payloads()[0]["installation_id"] == rec.payloads()[1]["installation_id"]


def test_success_waits_23_hours(harness):
    clock, rec = harness
    cycle()
    clock.advance(hours=22, minutes=59)
    assert cycle() == "not_due"
    assert len(rec.requests) == 1
    clock.advance(minutes=1)
    assert cycle() == "sent"
    assert len(rec.requests) == 2


def test_5xx_backoff_ladder_and_success_resets(harness):
    clock, rec = harness
    rec.status = 503
    ladder = []
    for _ in range(7):
        start = clock.now
        assert cycle() == "failed"
        due = _when("telemetry.next_due_at")
        ladder.append((due - start) / timedelta(hours=1))
        clock.now = due
    assert ladder == [1, 2, 4, 8, 16, 24, 24]
    assert _state()["telemetry.consecutive_failures"] == "7"
    rec.status = 200
    assert cycle() == "sent"
    assert _state()["telemetry.consecutive_failures"] == "0"
    assert _when("telemetry.next_due_at") - clock.now == timedelta(hours=23)


def test_4xx_waits_a_day(harness):
    clock, rec = harness
    rec.status = 400
    cycle()
    for _ in range(3):
        clock.advance(hours=1)
        assert cycle() == "not_due"
    assert len(rec.requests) == 1
    clock.advance(hours=21)
    rec.status = 200
    assert cycle() == "sent"
    assert len(rec.requests) == 2


def test_429_waits_23_hours(harness):
    clock, rec = harness
    rec.status = 429
    cycle()
    clock.advance(hours=22)
    assert cycle() == "not_due"
    clock.advance(hours=1)
    rec.status = 200
    assert cycle() == "sent"
    assert len(rec.requests) == 2


def test_failed_first_boot_is_retried_as_first_boot(harness):
    clock, rec = harness
    rec.status = 503
    cycle()
    clock.advance(hours=1)
    rec.status = 200
    cycle()
    cycle(force=True)
    assert [p["event"] for p in rec.payloads()] == ["first_boot", "first_boot", "heartbeat"]


def test_restart_within_23_hours_sends_nothing(harness):
    clock, rec = harness
    cycle()
    sender._reset_for_tests()
    clock.advance(hours=3)
    assert cycle() == "not_due"
    assert len(rec.requests) == 1


def test_loop_timing_and_clean_stop(telemetry_env, db, monkeypatch):
    sleeps = []
    cycles = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    async def fake_cycle(force=False):
        cycles.append(force)
        return "not_due"

    monkeypatch.setattr(sender, "SLEEP", fake_sleep)
    monkeypatch.setattr(sender, "run_cycle", fake_cycle)
    monkeypatch.setattr(sender, "RNG", random.Random(7))

    async def main():
        sender.start_telemetry()
        for _ in range(100_000):
            if len(sleeps) >= 40:
                break
            await asyncio.sleep(0)
        task = sender._loop_task_for_tests()
        await sender.stop_telemetry()
        assert sender._loop_task_for_tests() is None
        return task

    task = asyncio.run(main())
    assert 300 <= sleeps[0] <= 360
    assert all(3000 <= s <= 4200 for s in sleeps[1:])
    assert len(set(sleeps[1:])) > 1
    assert len(cycles) >= 38
    assert task is not None and task.done()


# ---------------------------------------------------------------------------
# Lease and re-checks
# ---------------------------------------------------------------------------

def test_foreign_unexpired_lease_blocks(harness):
    clock, rec = harness
    _hold_foreign_lease(clock.now + timedelta(seconds=100))
    assert cycle() == "busy"
    assert cycle(force=True) == "busy"
    assert rec.requests == []
    assert "telemetry.installation_id" not in _state()


def test_expired_foreign_lease_is_taken_over(harness):
    clock, rec = harness
    _hold_foreign_lease(clock.now - timedelta(seconds=1))
    assert cycle() == "sent"
    assert len(rec.requests) == 1
    assert sender.LEASE_KEY not in _state()


def test_due_is_rechecked_under_the_lease(harness, monkeypatch):
    clock, rec = harness

    def other_replica_sent():
        _put("telemetry.last_success_at", clock.now.isoformat())
        _put("telemetry.next_due_at", (clock.now + timedelta(hours=23)).isoformat())

    monkeypatch.setattr(sender, "_AFTER_DUE_CHECK", other_replica_sent)
    assert cycle() == "not_due"
    assert rec.requests == []


def test_lost_lease_at_the_fence_sends_nothing(harness, monkeypatch):
    clock, rec = harness
    monkeypatch.setattr(sender.settings_lease, "renew", lambda *a, **k: False)
    assert cycle(force=True) == "lease_lost"
    assert rec.requests == []


def test_admin_disable_just_before_the_post_sends_nothing(harness, monkeypatch):
    clock, rec = harness
    monkeypatch.setattr(sender, "_BEFORE_FENCE", lambda: _put("telemetry.admin_disabled", "true"))
    assert cycle(force=True) == "disabled"
    assert rec.requests == []


def test_id_race_returns_the_existing_id(harness, monkeypatch):
    clock, rec = harness
    foreign = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(sender, "_BEFORE_ID_INSERT", lambda: _put("telemetry.installation_id", foreign))
    assert cycle() == "sent"
    assert rec.payloads()[0]["installation_id"] == foreign
    session = TestingSessionLocal()
    try:
        assert session.query(SystemSetting).filter(SystemSetting.key == "telemetry.installation_id").count() == 1
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Disabled: nothing sent and no identity minted
# ---------------------------------------------------------------------------

def _unreadable(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("X=1\n")
    monkeypatch.setattr(sender, "DOTENV_PATH", str(path))

    def boom(_path):
        raise OSError("read failed")

    monkeypatch.setattr(sender, "DOTENV_READER", boom)


DISABLED = {
    "env_unreadable": _unreadable,
    "env_athena_telemetry": lambda mp, tp: mp.setenv("ATHENA_TELEMETRY", "off"),
    "env_athena_telemetry_unrecognized": lambda mp, tp: mp.setenv("ATHENA_TELEMETRY", "disabled"),
    "env_do_not_track": lambda mp, tp: mp.setenv("DO_NOT_TRACK", "1"),
    "endpoint_unset": lambda mp, tp: mp.setenv("ATHENA_TELEMETRY_ENDPOINT", ""),
    "endpoint_invalid": lambda mp, tp: mp.setenv("ATHENA_TELEMETRY_ENDPOINT", "http://evil.example/v1/ping"),
    "install_class_ci": lambda mp, tp: (mp.delenv("ATHENA_TELEMETRY_MODE"), mp.setenv("CI", "true")),
    "install_class_test": lambda mp, tp: (mp.delenv("ATHENA_TELEMETRY_MODE"), mp.delenv("CI", raising=False)),
    "ephemeral_database": lambda mp, tp: mp.setattr(sender, "EPHEMERAL_DB_CHECK", lambda: True),
    "admin_setting": lambda mp, tp: _put("telemetry.admin_disabled", "true"),
}


@pytest.mark.parametrize("reason", sorted(DISABLED))
def test_disabled_sends_nothing_and_mints_no_identity(harness, monkeypatch, tmp_path, db, reason):
    clock, rec = harness
    DISABLED[reason](monkeypatch, tmp_path)
    assert cycle(force=True) == "disabled"
    status = sender.get_status(db, None)
    assert (status["enabled"], status["reason"]) == (False, reason)
    assert rec.requests == []
    assert "telemetry.installation_id" not in _state()
    assert "telemetry.install_key" not in _state()


def test_status_on_a_fresh_db_mints_nothing(harness, db):
    status = sender.get_status(db, None)
    assert status["enabled"] is True and status["installation_id"] is None
    assert _state() == {}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_connect_error_is_recorded_without_the_host(harness):
    clock, rec = harness

    def refuse(request):
        raise httpx.ConnectError("connection refused by http://collector.example:1234/v1/ping", request=request)

    rec.handler = refuse
    assert cycle() == "failed"
    stored = _state()["telemetry.last_error"]
    assert json.loads(stored) == {"type": "ConnectError", "status": None}
    assert "collector" not in stored


def test_redirect_is_a_failure_and_not_followed(harness):
    clock, rec = harness
    rec.handler = lambda request: httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
    assert cycle() == "failed"
    assert len(rec.requests) == 1
    assert json.loads(_state()["telemetry.last_error"]) == {"type": "http", "status": 302}
    assert "telemetry.last_success_at" not in _state()


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, total, chunk=1024):
        self.total, self.chunk, self.sent = total, chunk, 0

    async def __aiter__(self):
        while self.sent < self.total:
            self.sent += self.chunk
            yield b"x" * self.chunk

    async def aclose(self):
        pass


def test_response_body_read_is_capped(harness):
    clock, rec = harness
    stream = _CountingStream(1024 * 1024)
    rec.handler = lambda request: httpx.Response(200, stream=stream)
    assert cycle() == "sent"
    assert stream.sent <= sender.RESPONSE_CAP_BYTES + stream.chunk


def test_install_key_mismatch_does_not_rotate(harness):
    clock, rec = harness
    rec.status, rec.body = 403, b'{"error":"install_key_mismatch"}'
    with capture_logs() as logs:
        assert cycle() == "failed"
    before = _state()
    assert _when("telemetry.next_due_at") - clock.now == timedelta(hours=24)
    assert [e for e in logs if e["log_level"] == "warning" and e["event"] == "telemetry_install_key_rejected"]
    clock.advance(hours=24)
    rec.status, rec.body = 200, b"{}"
    cycle()
    after = _state()
    assert after["telemetry.installation_id"] == before["telemetry.installation_id"]
    assert after["telemetry.install_key"] == before["telemetry.install_key"]


def test_cycle_is_bounded_and_releases_the_lease(harness, monkeypatch):
    clock, rec = harness

    async def stall(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    rec.handler = stall
    monkeypatch.setattr(sender, "CYCLE_TIMEOUT_SECONDS", 0.3)
    assert cycle() == "timeout"
    assert sender.LEASE_KEY not in _state()


# ---------------------------------------------------------------------------
# Opt-outs from the process env and `.env`
# ---------------------------------------------------------------------------

def test_dotenv_only_opt_out(harness, monkeypatch, tmp_path):
    clock, rec = harness
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sender, "DOTENV_PATH", ".env")
    monkeypatch.delenv("ATHENA_TELEMETRY", raising=False)
    (tmp_path / ".env").write_text("ATHENA_TELEMETRY=off\n")
    assert cycle(force=True) == "disabled"
    assert rec.requests == []


@pytest.mark.parametrize("dotenv,reason", [
    ("ATHENA_TELEMETRY=off\n", "env_athena_telemetry"),
    ("DO_NOT_TRACK=1\n", "env_do_not_track"),
])
def test_empty_process_value_never_masks_dotenv_opt_out(harness, monkeypatch, tmp_path, db, dotenv, reason):
    clock, rec = harness
    (tmp_path / ".env").write_text(dotenv)
    monkeypatch.setattr(sender, "DOTENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("ATHENA_TELEMETRY", "")
    monkeypatch.setenv("DO_NOT_TRACK", "")
    assert cycle(force=True) == "disabled"
    assert sender.get_status(db, None)["reason"] == reason
    assert rec.requests == []


def test_process_on_does_not_override_dotenv_off(harness, monkeypatch, tmp_path):
    clock, rec = harness
    (tmp_path / ".env").write_text("ATHENA_TELEMETRY=off\n")
    monkeypatch.setattr(sender, "DOTENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("ATHENA_TELEMETRY", "on")
    assert cycle(force=True) == "disabled"
    assert rec.requests == []


def _assert_unreadable(harness, db):
    clock, rec = harness
    with capture_logs() as logs:
        assert cycle(force=True) == "disabled"
    assert sender.get_status(db, None)["reason"] == "env_unreadable"
    assert len([e for e in logs if e["log_level"] == "warning"]) == 1
    assert rec.requests == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read a mode-000 file")
def test_unreadable_dotenv_is_off(harness, monkeypatch, tmp_path, db):
    path = tmp_path / ".env"
    path.write_text("ATHENA_TELEMETRY=on\n")
    path.chmod(0)
    monkeypatch.setattr(sender, "DOTENV_PATH", str(path))
    try:
        _assert_unreadable(harness, db)
    finally:
        path.chmod(0o600)


def test_unreadable_dotenv_is_off_via_reader_hook(harness, monkeypatch, tmp_path, db):
    _unreadable(monkeypatch, tmp_path)
    _assert_unreadable(harness, db)


def test_undecodable_dotenv_is_off(harness, monkeypatch, tmp_path, db):
    path = tmp_path / ".env"
    path.write_bytes(b"ATHENA_TELEMETRY=\xff\xfe\n")
    monkeypatch.setattr(sender, "DOTENV_PATH", str(path))
    _assert_unreadable(harness, db)


def test_absent_dotenv_has_no_effect(harness, monkeypatch, tmp_path):
    clock, rec = harness
    monkeypatch.setattr(sender, "DOTENV_PATH", str(tmp_path / "missing.env"))
    assert cycle() == "sent"


# ---------------------------------------------------------------------------
# Identity and the endpoint-bound wire key
# ---------------------------------------------------------------------------

def test_wire_key_is_endpoint_bound_hmac(harness, monkeypatch):
    clock, rec = harness
    cycle()
    install_key = _state()["telemetry.install_key"]
    header = rec.requests[0].headers["x-athena-install-key"]
    assert header == _wire_key(install_key, TELEMETRY_DEFAULT_ENDPOINT)
    monkeypatch.setenv("ATHENA_TELEMETRY_ENDPOINT", "http://127.0.0.1:8787/v1/ping")
    cycle(force=True)
    local_header = rec.requests[1].headers["x-athena-install-key"]
    assert local_header == _wire_key(install_key, "http://127.0.0.1:8787/v1/ping")
    assert local_header != header
    raw = base64.urlsafe_b64decode(install_key + "=" * (-len(install_key) % 4))
    for request in rec.requests:
        wire = b"".join(k + b":" + v for k, v in request.headers.raw) + request.content
        assert install_key.encode() not in wire
        assert raw not in wire
        assert request.headers["x-athena-install-key"].encode() not in request.content
    assert header not in _state()["telemetry.last_payload"]


def test_id_without_key_regenerates_both(harness):
    clock, rec = harness
    old = "11111111-2222-4333-8444-555555555555"
    _put("telemetry.installation_id", old)
    with capture_logs() as logs:
        assert cycle() == "sent"
    state = _state()
    assert state["telemetry.installation_id"] != old
    assert len(state["telemetry.install_key"]) == 43
    assert rec.payloads()[0]["installation_id"] == state["telemetry.installation_id"]
    assert rec.payloads()[0]["event"] == "first_boot"
    assert len([e for e in logs if e["log_level"] == "warning" and e["event"] == "telemetry_identity_regenerated"]) == 1


def test_provenance_new_vs_upgraded(harness, db):
    from app.models import User

    clock, rec = harness
    cycle()
    assert rec.payloads()[0]["install"]["provenance"] == "new"
    sender.reset_identity()
    db.add(User(username="old", email="old@example.com", role="owner", created_at=clock.now - timedelta(days=2)))
    db.commit()
    cycle()
    assert rec.payloads()[1]["install"]["provenance"] == "upgraded"


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def test_reset_clears_identity_and_backoff(harness):
    clock, rec = harness
    assert cycle() == "sent"
    rec.status = 403
    cycle(force=True)
    rec.status = 503
    cycle(force=True)
    assert _when("telemetry.next_due_at") > clock.now
    before = _state()
    assert all(k in before for k in RESET_KEYS)
    old_id = sender.reset_identity()
    assert old_id == before["telemetry.installation_id"]
    after = _state()
    assert not [k for k in RESET_KEYS if k in after]
    rec.status = 200
    assert cycle() == "sent"
    payload = rec.payloads()[-1]
    assert payload["installation_id"] != old_id and payload["event"] == "first_boot"


def test_reset_under_a_held_lease_deletes_nothing(harness):
    clock, rec = harness
    cycle()
    before = _state()
    _hold_foreign_lease(clock.now + timedelta(seconds=100))
    with pytest.raises(settings_lease.LeaseBusy):
        sender.reset_identity()
    after = _state()
    after.pop(sender.LEASE_KEY)
    assert after == before


# ---------------------------------------------------------------------------
# Startup disclosure
# ---------------------------------------------------------------------------

def _startup_events():
    async def main():
        sender.start_telemetry()
        await sender.stop_telemetry()

    with capture_logs() as logs:
        asyncio.run(main())
    return [e for e in logs if str(e.get("event", "")).startswith("telemetry_")]


def test_startup_log_discloses_endpoint_and_opt_out(harness):
    events = _startup_events()
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "telemetry_enabled"
    message = event["message"]
    for needle in (TELEMETRY_DEFAULT_ENDPOINT, "ATHENA_TELEMETRY=off", "DO_NOT_TRACK=1", "pseudonymous",
                   "docs/CONFIGURATION.md#telemetry"):
        assert needle in message, needle


def test_startup_log_when_disabled_states_reason(harness, monkeypatch):
    monkeypatch.setenv("ATHENA_TELEMETRY", "off")
    events = _startup_events()
    assert len(events) == 1
    assert events[0]["event"] == "telemetry_disabled"
    assert events[0]["reason"] == "env_athena_telemetry"
    assert "ATHENA_TELEMETRY=off" in events[0]["message"]


def test_startup_never_raises(harness, monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(sender, "LEASE_SESSION_FACTORY", broken)
    with capture_logs() as logs:
        sender.start_telemetry()
    assert [e for e in logs if e["event"] == "telemetry_start_failed"]


# ---------------------------------------------------------------------------
# Manual sends (D46 / codex M): spacing re-checked inside the lease
# ---------------------------------------------------------------------------

def test_manual_sends_back_to_back_post_once(harness):
    clock, rec = harness
    assert asyncio.run(sender.run_cycle(force=True, manual=True)) == "sent"
    clock.advance(minutes=9, seconds=59)
    assert asyncio.run(sender.run_cycle(force=True, manual=True)) == "too_soon"
    assert len(rec.requests) == 1
    clock.advance(seconds=1)
    assert asyncio.run(sender.run_cycle(force=True, manual=True)) == "sent"
    assert len(rec.requests) == 2


def test_manual_spacing_does_not_block_scheduled_or_plain_forced_cycles(harness):
    clock, rec = harness
    assert asyncio.run(sender.run_cycle(force=True, manual=True)) == "sent"
    assert asyncio.run(sender.run_cycle(force=True)) == "sent"
    assert len(rec.requests) == 2


def test_manual_reservation_is_atomic(harness, db, monkeypatch):
    clock, rec = harness
    assert sender.reserve_manual_send(db, clock.now) is True
    assert sender.reserve_manual_send(db, clock.now) is False
    clock.advance(minutes=10)

    def competing():
        other = TestingSessionLocal()
        try:
            monkeypatch.setattr(sender, "_BEFORE_SEND_RESERVE", None)
            assert sender.reserve_manual_send(other, clock.now) is True
        finally:
            other.close()

    monkeypatch.setattr(sender, "_BEFORE_SEND_RESERVE", competing)
    assert sender.reserve_manual_send(db, clock.now) is False
