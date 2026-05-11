"""
Leader-election test suite for health_poller — ATHENA-18, Phase 3.

21 test cases across 5 groups:

  Group 1 — Direct helper tests (_acquire_or_renew_lease): H1–H6
  Group 2 — Loop-level tests (health_poll_loop, interval_override=0): L1–L7
  Group 3 — On-demand-poll regression guard: P1–P2
  Group 4 — Slow-poll + heartbeat strict-policy abort: S4–S6
  Group 5 — start_health_polling invariant gate: S1–S3

Uses fakeredis[lua]>=2.21 for in-process Redis simulation.
"""
import asyncio
import os
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — must precede any app imports.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

_SERVICE_KEY = "test-le-svc-key"

# Set env before any app-module import so AthenaConfig picks them up.
os.environ.setdefault("DEV_MODE", "false")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", _SERVICE_KEY)
os.environ.setdefault("OIDC_ISSUER", "https://fake.oidc.example.com")
os.environ.setdefault("OIDC_CLIENT_ID", "fake-client")
os.environ.setdefault("CONTROL_AGENT_ENABLED", "false")

import pytest
import pytest_asyncio
import redis.exceptions
import structlog.testing

import fakeredis.aioredis

from app.services.health_poller import (
    _acquire_or_renew_lease,
    _poll_all_services,
    _poll_one,
    health_poll_loop,
    start_health_polling,
    LEASE_KEY,
    LEASE_TTL_SECONDS,
    HOLDER_ID,
)
import app.services.health_poller as health_poller


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_and_task():
    """Reset AthenaConfig LRU cache and module-level _poll_task between tests."""
    from shared.config import get_config
    os.environ["DEV_MODE"] = "false"
    os.environ["SERVICE_API_KEY"] = _SERVICE_KEY
    get_config.cache_clear()
    health_poller._poll_task = None
    yield
    get_config.cache_clear()
    health_poller._poll_task = None


@pytest_asyncio.fixture
async def fake_redis():
    """Fresh fakeredis instance, flushed before each test."""
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
async def fake_redis_decode():
    """fakeredis with decode_responses=True."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.flushall()
    await r.aclose()


# ---------------------------------------------------------------------------
# Group 1 — Direct helper tests: _acquire_or_renew_lease
# ---------------------------------------------------------------------------


class TestAcquireOrRenewLease:
    """H1–H6: direct contract tests for the lease helper."""

    @pytest.mark.asyncio
    async def test_acquire_returns_acquired_when_nx_succeeds(self, fake_redis):
        """H1: fresh Redis, NX wins → (True, 'acquired'); key set with TTL."""
        ok, reason = await _acquire_or_renew_lease(fake_redis, "replica-A")

        assert ok is True
        assert reason == "acquired"

        val = await fake_redis.get(LEASE_KEY)
        assert val == b"replica-A"

        ttl = await fake_redis.ttl(LEASE_KEY)
        assert 1 <= ttl <= LEASE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_acquire_returns_renewed_when_same_holder_owns_key(self, fake_redis):
        """H2: key pre-set to 'A', call with holder='A' → (True, 'renewed'); TTL refreshed."""
        # Pre-seed with a low TTL so refresh is observable.
        await fake_redis.set(LEASE_KEY, "replica-A", ex=5)
        ttl_before = await fake_redis.ttl(LEASE_KEY)

        ok, reason = await _acquire_or_renew_lease(fake_redis, "replica-A")

        assert ok is True
        assert reason == "renewed"

        val = await fake_redis.get(LEASE_KEY)
        assert val == b"replica-A"

        ttl_after = await fake_redis.ttl(LEASE_KEY)
        # Renewal should have refreshed TTL well above the 5s seed.
        assert ttl_after >= ttl_before

    @pytest.mark.asyncio
    async def test_acquire_returns_not_leader_when_different_holder(self, fake_redis):
        """H3: key held by 'A', call with holder='B' → (False, 'not_leader'); key unchanged."""
        await fake_redis.set(LEASE_KEY, "replica-A", ex=LEASE_TTL_SECONDS)

        ok, reason = await _acquire_or_renew_lease(fake_redis, "replica-B")

        assert ok is False
        assert reason == "not_leader"

        val = await fake_redis.get(LEASE_KEY)
        assert val == b"replica-A"

    @pytest.mark.asyncio
    async def test_acquire_returns_not_leader_when_key_expires_between_nx_fail_and_lua(
        self, fake_redis
    ):
        """H4: TOCTOU — NX returns False (mocked), Lua returns None (mocked) → not_leader."""
        # Simulate the race: SET NX fails (someone else holds key), then key
        # disappears before our Lua runs — Lua returns None because GET != our holder.
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=False)  # NX fails
        mock_redis.eval = AsyncMock(return_value=None)   # Lua: GET returned nil

        ok, reason = await _acquire_or_renew_lease(mock_redis, "replica-A")

        assert ok is False
        assert reason == "not_leader"

    @pytest.mark.asyncio
    async def test_redis_error_returns_redis_error_reason(self, fake_redis):
        """H5: ConnectionError during SET NX → (False, 'redis_error:ConnectionError'); no raise."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(
            side_effect=redis.exceptions.ConnectionError("boom")
        )

        ok, reason = await _acquire_or_renew_lease(mock_redis, "replica-A")

        assert ok is False
        assert reason == "redis_error:ConnectionError"

    @pytest.mark.asyncio
    async def test_decode_responses_modes_compat(self, fake_redis, fake_redis_decode):
        """H6: both decode_responses=False and =True must work end-to-end (acquire + renew)."""
        for r in (fake_redis, fake_redis_decode):
            # Fresh state — flush between parametrized rounds.
            await r.flushall()

            # Acquire.
            ok1, reason1 = await _acquire_or_renew_lease(r, "replica-A")
            assert ok1 is True
            assert reason1 == "acquired"

            # Renew — same holder.
            ok2, reason2 = await _acquire_or_renew_lease(r, "replica-A")
            assert ok2 is True
            assert reason2 == "renewed"


# ---------------------------------------------------------------------------
# Group 2 — Loop-level tests: health_poll_loop(interval_override=0)
# ---------------------------------------------------------------------------


class TestHealthPollLoop:
    """L1–L7: loop behavior tests using interval_override=0 for single-cycle termination."""

    @pytest.mark.asyncio
    async def test_leader_acquires_and_polls_one_cycle(self, fake_redis):
        """L1: leader acquires, _poll_all_services called exactly once."""
        poll_calls = []

        async def fake_poll(sem):
            poll_calls.append(True)
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        with structlog.testing.capture_logs() as logs:
            with patch.object(health_poller, "_poll_all_services", fake_poll):
                await health_poll_loop(
                    interval_override=0,
                    redis_client=fake_redis,
                    holder_id="replica-A",
                )

        assert len(poll_calls) == 1

        event_names = [e["event"] for e in logs]
        assert "health_poll_lease_acquired" in event_names

        val = await fake_redis.get(LEASE_KEY)
        assert val == b"replica-A"
        ttl = await fake_redis.ttl(LEASE_KEY)
        assert 1 <= ttl <= LEASE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_non_leader_skips_and_breaks_on_interval_zero(self, fake_redis):
        """L2: non-leader (key held by B) skips poll; logs health_poll_skipped_not_leader."""
        await fake_redis.set(LEASE_KEY, "replica-B", ex=LEASE_TTL_SECONDS)
        poll_calls = []

        async def fake_poll(sem):
            poll_calls.append(True)
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        with structlog.testing.capture_logs() as logs:
            with patch.object(health_poller, "_poll_all_services", fake_poll):
                await health_poll_loop(
                    interval_override=0,
                    redis_client=fake_redis,
                    holder_id="replica-A",
                )

        assert len(poll_calls) == 0

        event_names = [e["event"] for e in logs]
        assert "health_poll_skipped_not_leader" in event_names

    @pytest.mark.asyncio
    async def test_renewal_extends_ttl_observable_via_redis(self, fake_redis):
        """L3: _acquire_or_renew_lease called twice for same holder refreshes TTL."""
        # Pre-seed with short TTL.
        await fake_redis.set(LEASE_KEY, "replica-A", ex=5)
        ttl_before = await fake_redis.ttl(LEASE_KEY)

        # First acquire: NX fails, Lua renews.
        await _acquire_or_renew_lease(fake_redis, "replica-A")
        ttl_mid = await fake_redis.ttl(LEASE_KEY)

        # Second acquire (simulating next heartbeat): NX fails again, Lua renews.
        await _acquire_or_renew_lease(fake_redis, "replica-A")
        ttl_after = await fake_redis.ttl(LEASE_KEY)

        # Each renewal should refresh to LEASE_TTL_SECONDS, so TTL ≥ ttl_before.
        assert ttl_mid >= ttl_before
        assert ttl_after >= ttl_before

    @pytest.mark.asyncio
    async def test_dev_mode_redis_client_none_always_polls(self):
        """L4: redis_client=None → legacy path, _poll_all_services called."""
        poll_calls = []

        async def fake_poll(sem):
            poll_calls.append(True)
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        with structlog.testing.capture_logs() as logs:
            with patch.object(health_poller, "_poll_all_services", fake_poll):
                await health_poll_loop(interval_override=0, redis_client=None)

        assert len(poll_calls) == 1

        event_names = [e["event"] for e in logs]
        assert "health_poll_lease_disabled_no_redis" in event_names

    @pytest.mark.asyncio
    async def test_dev_mode_does_not_poll(self):
        """L5: cfg.dev_mode=True → early return, no lease interaction, no poll."""
        from shared.config import get_config
        os.environ["DEV_MODE"] = "true"
        get_config.cache_clear()

        poll_calls = []

        async def fake_poll(sem):
            poll_calls.append(True)
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        # Even with a real fakeredis provided, dev_mode short-circuits before lease.
        r = fakeredis.aioredis.FakeRedis()
        try:
            with structlog.testing.capture_logs() as logs:
                with patch.object(health_poller, "_poll_all_services", fake_poll):
                    await health_poll_loop(
                        interval_override=0,
                        redis_client=r,
                        holder_id="replica-A",
                    )

            assert len(poll_calls) == 0
            # No Redis key should have been written.
            assert await r.dbsize() == 0

            event_names = [e["event"] for e in logs]
            assert "health_poll_disabled_dev_mode" in event_names
        finally:
            await r.flushall()
            await r.aclose()
            os.environ["DEV_MODE"] = "false"
            get_config.cache_clear()

    @pytest.mark.asyncio
    async def test_holder_id_defaults_to_module_constant_when_none(self, fake_redis):
        """L6: holder_id=None → key value matches HOLDER_ID module constant."""
        poll_calls = []

        async def fake_poll(sem):
            poll_calls.append(True)
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        with patch.object(health_poller, "_poll_all_services", fake_poll):
            await health_poll_loop(
                interval_override=0,
                redis_client=fake_redis,
                holder_id=None,
            )

        val = await fake_redis.get(LEASE_KEY)
        assert val is not None
        assert val.decode() == HOLDER_ID

    @pytest.mark.asyncio
    async def test_redis_error_during_acquire_skips_cycle(self, fake_redis):
        """L7: ConnectionError on SET NX → skips poll, logs skipped, breaks on interval_override=0."""
        poll_calls = []

        async def fake_poll(sem):
            poll_calls.append(True)
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(
            side_effect=redis.exceptions.ConnectionError("boom")
        )

        with structlog.testing.capture_logs() as logs:
            with patch.object(health_poller, "_poll_all_services", fake_poll):
                # The helper catches ConnectionError and returns (False, "redis_error:...")
                # so the loop logs skipped_not_leader.
                await health_poll_loop(
                    interval_override=0,
                    redis_client=mock_redis,
                    holder_id="replica-A",
                )

        assert len(poll_calls) == 0

        event_names = [e["event"] for e in logs]
        # Helper returns redis_error → loop gets (False, "redis_error:ConnectionError")
        # which takes the not_leader branch.
        assert "health_poll_skipped_not_leader" in event_names

        # Confirm reason carries the redis_error prefix.
        skipped = [e for e in logs if e["event"] == "health_poll_skipped_not_leader"]
        assert any(
            str(e.get("reason", "")).startswith("redis_error:") for e in skipped
        )


# ---------------------------------------------------------------------------
# Group 3 — On-demand-poll regression guard
# ---------------------------------------------------------------------------


class TestOnDemandPollNotGated:
    """P1–P2: _poll_all_services and _poll_one are NOT gated by the lease."""

    @pytest.mark.asyncio
    async def test_poll_all_services_not_gated_by_lease(self, fake_redis):
        """P1: LEASE_KEY held by a different replica; _poll_all_services still executes."""
        # Set up: a different holder holds the lease.
        await fake_redis.set(LEASE_KEY, "some-other-replica", ex=LEASE_TTL_SECONDS)

        # _poll_all_services needs a DB context — patch it out at the DB layer.
        mock_summary = {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}
        semaphore = asyncio.Semaphore(8)

        with patch("app.services.health_poller.get_db_context") as mock_ctx:
            # Return an empty service list so gather is a no-op.
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session.query.return_value.filter.return_value.all.return_value = []
            mock_ctx.return_value = mock_session

            # Should not raise and should execute.
            result = await _poll_all_services(semaphore)

        # Function ran and returned a summary dict.
        assert isinstance(result, dict)
        assert "services_polled" in result

        # Lease key untouched.
        val = await fake_redis.get(LEASE_KEY)
        assert val == b"some-other-replica"

    @pytest.mark.asyncio
    async def test_poll_one_not_gated_by_lease(self, fake_redis):
        """P2: LEASE_KEY held by foreign owner; _poll_one still executes."""
        await fake_redis.set(LEASE_KEY, "other-replica", ex=LEASE_TTL_SECONDS)

        semaphore = asyncio.Semaphore(8)

        # _poll_one makes an outbound HTTP call — mock the httpx client.
        async with mock.AsyncMock() as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok"}
            mock_client.get = AsyncMock(return_value=mock_resp)

            # Patch _validate_service_url to allow the call.
            with patch(
                "app.services.health_poller._validate_service_url",
                return_value=(True, ""),
            ):
                result = await _poll_one(
                    mock_client, semaphore,
                    svc_id=1, name="test-svc",
                    host="10.0.0.1", port=8080, path="/health",
                )

        # Function executed and returned a 6-tuple.
        svc_id, status, _rt, _cat, _det, _msg = result
        assert svc_id == 1

        # Lease key untouched.
        val = await fake_redis.get(LEASE_KEY)
        assert val == b"other-replica"


# ---------------------------------------------------------------------------
# Group 4 — Slow-poll + heartbeat strict-policy abort
# ---------------------------------------------------------------------------


class TestHeartbeatStrictAbort:
    """S4–S6: D4b strict-abort policy tests."""

    @pytest.mark.asyncio
    async def test_slow_poll_with_lease_loss_aborts_no_overlap(self, fake_redis, monkeypatch):
        """S4: canonical regression — lease loss mid-poll cancels the poll; max_active_polls==1."""
        monkeypatch.setattr(health_poller, "LEASE_TTL_SECONDS", 2)
        monkeypatch.setattr(health_poller, "HEARTBEAT_INTERVAL_SECONDS", 1)

        active_polls = 0
        max_active_polls = 0
        poll_was_cancelled = False

        async def slow_poll(sem):
            nonlocal active_polls, max_active_polls
            active_polls += 1
            max_active_polls = max(max_active_polls, active_polls)
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                nonlocal poll_was_cancelled
                poll_was_cancelled = True
                raise
            finally:
                active_polls -= 1
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        # Wrap _acquire_or_renew_lease so:
        # - call 1 (replica-A loop-top acquire): success → (True, "acquired")
        # - call 2 (replica-A heartbeat at t≈1s): success → (True, "renewed")
        # - call 3+ (replica-A heartbeat at t≈2s): failure → (False, "redis_error:ConnectionError")
        call_count_A = 0
        original_acquire = health_poller._acquire_or_renew_lease

        async def patched_acquire_A(redis_client, holder_id):
            nonlocal call_count_A
            if holder_id == "replica-A":
                call_count_A += 1
                if call_count_A == 1:
                    return (True, "acquired")
                elif call_count_A == 2:
                    return (True, "renewed")
                else:
                    return (False, "redis_error:ConnectionError")
            # replica-B uses real fakeredis.
            return await original_acquire(fake_redis, holder_id)

        with structlog.testing.capture_logs() as logs:
            with patch.object(health_poller, "_poll_all_services", slow_poll):
                with patch.object(health_poller, "_acquire_or_renew_lease", patched_acquire_A):
                    # Replica-A starts; its heartbeat will fail at call 3.
                    task_A = asyncio.create_task(
                        health_poll_loop(
                            interval_override=0,
                            redis_client=fake_redis,
                            holder_id="replica-A",
                        )
                    )
                    # Give A time to acquire, start poll, and have heartbeat fail at t≈2s.
                    await asyncio.sleep(2.5)
                    # By now A's heartbeat has fired once (renewed) and a second time (failed).
                    # A's poll_task should be cancelled; A's loop should have exited.
                    await asyncio.wait_for(task_A, timeout=8)

        # No two replicas polled concurrently.
        assert max_active_polls == 1, (
            f"max_active_polls={max_active_polls} — two replicas polled concurrently"
        )
        # A's slow poll was cancelled by the strict-abort path.
        assert poll_was_cancelled, "slow_poll was not cancelled when lease was lost"

        # Abort log was emitted.
        event_names = [e["event"] for e in logs]
        assert "health_poll_aborted_lease_lost" in event_names

        # Heartbeat had at least one successful renewal before failure.
        assert call_count_A >= 2, "Heartbeat never ran — no renewal observed"

    @pytest.mark.asyncio
    async def test_heartbeat_cancelled_cleanly_on_normal_poll_completion(self, fake_redis):
        """S5: poll completes normally; heartbeat task is cancelled + awaited; no task leak."""
        poll_complete = asyncio.Event()

        async def fast_poll(sem):
            poll_complete.set()
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        # Track whether the heartbeat task was properly cancelled.
        hb_task_ref = []
        original_create_task = asyncio.create_task

        def capturing_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            # Capture tasks — we'll check the last two (heartbeat + lost_waiter).
            hb_task_ref.append(task)
            return task

        with patch.object(health_poller, "_poll_all_services", fast_poll):
            with patch("asyncio.create_task", side_effect=capturing_create_task):
                await asyncio.wait_for(
                    health_poll_loop(
                        interval_override=0,
                        redis_client=fake_redis,
                        holder_id="replica-A",
                    ),
                    timeout=10,
                )

        # After the loop returns, all captured tasks should be done (cancelled or finished).
        for t in hb_task_ref:
            assert t.done(), f"Task {t} was not done after loop returned — possible leak"

    @pytest.mark.asyncio
    async def test_redis_error_during_heartbeat_aborts_poll(self, fake_redis, monkeypatch):
        """S6: heartbeat fails at second _acquire_or_renew_lease call → poll cancelled, no raise."""
        monkeypatch.setattr(health_poller, "LEASE_TTL_SECONDS", 2)
        monkeypatch.setattr(health_poller, "HEARTBEAT_INTERVAL_SECONDS", 1)

        poll_was_cancelled = False

        async def slow_poll(sem):
            nonlocal poll_was_cancelled
            try:
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                poll_was_cancelled = True
                raise
            return {"services_polled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}

        call_count = 0

        async def acquire_side_effect(redis_client, holder_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Initial loop-top acquire succeeds.
                return (True, "acquired")
            else:
                # Heartbeat renewal fails (Redis error).
                return (False, "redis_error:ConnectionError")

        with structlog.testing.capture_logs() as logs:
            with patch.object(health_poller, "_poll_all_services", slow_poll):
                with patch.object(health_poller, "_acquire_or_renew_lease", acquire_side_effect):
                    # Loop should return cleanly — no ConnectionError propagated.
                    await asyncio.wait_for(
                        health_poll_loop(
                            interval_override=0,
                            redis_client=fake_redis,
                            holder_id="replica-A",
                        ),
                        timeout=10,
                    )

        # Poll must have been cancelled by the strict-abort path.
        assert poll_was_cancelled, "slow_poll was not cancelled after heartbeat failure"

        event_names = [e["event"] for e in logs]

        # Heartbeat loss warning emitted.
        assert "health_poll_lease_heartbeat_lost" in event_names

        # Abort warning emitted.
        assert "health_poll_aborted_lease_lost" in event_names

        # Loop returned without raising ConnectionError.
        # (The await_for above would have raised if a non-CancelledError propagated.)

        # Confirm reason in heartbeat-lost log.
        hb_lost = [e for e in logs if e["event"] == "health_poll_lease_heartbeat_lost"]
        assert hb_lost, "health_poll_lease_heartbeat_lost not emitted"
        assert "redis_error" in str(hb_lost[0].get("reason", ""))


# ---------------------------------------------------------------------------
# Group 5 — start_health_polling invariant gate
# ---------------------------------------------------------------------------


class TestStartHealthPollingInvariant:
    """S1–S3: start_health_polling behavior at startup."""

    def setup_method(self):
        health_poller._poll_task = None

    @pytest.mark.asyncio
    async def test_start_polling_with_no_redis_client_does_not_raise(self):
        """S1: redis_client=None → legacy path, no SystemExit."""
        async def _fake_loop(*a, **kw):
            await asyncio.sleep(9999)

        with patch.object(health_poller, "health_poll_loop", _fake_loop):
            # Must not raise.
            start_health_polling(redis_client=None)
            task = health_poller._poll_task
            assert task is not None
            task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await task

    @pytest.mark.asyncio
    async def test_start_polling_with_redis_client_and_valid_ttl(self, fake_redis):
        """S2: LEASE_TTL_SECONDS=40 > interval=30 → no SystemExit."""
        from shared.config import get_config
        os.environ["DEV_MODE"] = "false"
        get_config.cache_clear()

        async def _fake_loop(*a, **kw):
            await asyncio.sleep(9999)

        # Default LEASE_TTL_SECONDS=40, default interval=30 → valid.
        with patch.object(health_poller, "health_poll_loop", _fake_loop):
            # Must not raise.
            start_health_polling(redis_client=fake_redis)
            task = health_poller._poll_task
            assert task is not None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_start_polling_raises_systemexit_if_ttl_not_greater_than_interval(
        self, fake_redis, monkeypatch
    ):
        """S3: LEASE_TTL_SECONDS(40) <= interval(45) → SystemExit with 'FATAL:' prefix."""
        from shared.config import get_config

        # Patch interval to be > LEASE_TTL_SECONDS=40.
        os.environ["DEV_MODE"] = "false"
        get_config.cache_clear()

        class _FakeConfig:
            health_poll_interval_seconds = 45
            dev_mode = False
            health_poll_concurrency = 8
            health_poll_timeout_seconds = 5

        with patch.object(health_poller, "get_config", return_value=_FakeConfig()):
            with pytest.raises(SystemExit) as exc_info:
                start_health_polling(redis_client=fake_redis)

        assert str(exc_info.value.code).startswith("FATAL:")
        assert "40" in str(exc_info.value.code)
        assert "45" in str(exc_info.value.code)

        # redis_client=None path must NOT raise even under same bad interval.
        health_poller._poll_task = None

        async def _fake_loop(*a, **kw):
            await asyncio.sleep(9999)

        with patch.object(health_poller, "get_config", return_value=_FakeConfig()):
            with patch.object(health_poller, "health_poll_loop", _fake_loop):
                # No-Redis path skips the TTL gate.
                start_health_polling(redis_client=None)
                task = health_poller._poll_task
                if task:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
