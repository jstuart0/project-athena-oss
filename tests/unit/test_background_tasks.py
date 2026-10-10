"""`_runtime.spawn_background`: retained until done, drained first at shutdown, bounded.

(The LLM router's metric-task drain belongs to the prompt-token phase, which
adds `LLMRouter._spawn`; it is tested there.)
"""
from __future__ import annotations

import asyncio
import gc
import logging
import sys
import time

import pytest

sys.path.insert(0, "src")

from orchestrator.nodes import _runtime  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


def test_spawn_background_keeps_a_strong_reference_until_done():
    async def scenario():
        release = asyncio.Event()
        ran = []

        async def work():
            await release.wait()
            ran.append(True)

        task = _runtime.spawn_background(work())
        gc.collect()
        assert task in _runtime._background_tasks
        await asyncio.sleep(0)
        gc.collect()
        assert task in _runtime._background_tasks and not task.done()
        release.set()
        await task
        await asyncio.sleep(0)
        assert ran == [True]
        assert task not in _runtime._background_tasks

    asyncio.run(scenario())


def test_close_all_awaits_pending_tasks_before_closing_the_session_manager():
    events = []

    class _SessionManager:
        async def close(self):
            events.append("session_manager_closed")

    async def scenario():
        _runtime.set_session_manager(_SessionManager())

        async def persist():
            await asyncio.sleep(0.05)
            events.append("persisted")

        _runtime.spawn_background(persist())
        await _runtime.close_all()

    asyncio.run(scenario())
    assert events == ["persisted", "session_manager_closed"]


def test_a_task_that_never_finishes_is_cancelled_after_the_bound(monkeypatch, caplog):
    monkeypatch.setattr(_runtime, "BACKGROUND_DRAIN_TIMEOUT_SECONDS", 0.05)
    caplog.set_level(logging.WARNING, logger=_runtime.logger.name)

    async def scenario():
        async def never():
            await asyncio.sleep(3600)

        task = _runtime.spawn_background(never())
        started = time.monotonic()
        await _runtime.drain_background()
        return task, time.monotonic() - started

    task, elapsed = asyncio.run(scenario())
    assert task.cancelled()
    assert elapsed < 1.0
    assert "background_tasks_abandoned count=1" in caplog.text


def test_a_clean_drain_logs_nothing_abandoned(caplog):
    caplog.set_level(logging.WARNING, logger=_runtime.logger.name)

    async def scenario():
        async def quick():
            await asyncio.sleep(0)

        _runtime.spawn_background(quick())
        await _runtime.drain_background()

    asyncio.run(scenario())
    assert "background_tasks_abandoned" not in caplog.text
    assert not _runtime._background_tasks


def test_a_failing_task_is_logged_by_class_only_and_released(caplog):
    caplog.set_level(logging.WARNING, logger=_runtime.logger.name)

    async def scenario():
        async def boom():
            raise ValueError("secret detail 10.0.0.5")

        task = _runtime.spawn_background(boom())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        return task

    task = asyncio.run(scenario())
    assert task not in _runtime._background_tasks
    assert "background_task_failed error_class=ValueError" in caplog.text
    assert "10.0.0.5" not in caplog.text


def test_drain_with_nothing_pending_returns_immediately():
    asyncio.run(_runtime.drain_background())
