"""Entity-state freshness (ATHENA-269): a short max_age for locks and covers,
a single-flight bulk refresh, single-entity reads, and per-id stale marks."""
from __future__ import annotations

import asyncio
import unittest.mock as mock
from datetime import datetime, timedelta

import pytest

import orchestrator.smart_home_controller as shc
from orchestrator.ha_entity_manager import HAEntityManager

pytestmark = pytest.mark.filterwarnings("ignore")


def state(entity_id, value, name=None):
    return {"entity_id": entity_id, "state": value, "attributes": {"friendly_name": name or entity_id}}


class FakeHA:
    """A client whose states a test can flip; bulk reads can be held open."""

    def __init__(self, states):
        self.states = {s["entity_id"]: s for s in states}
        self.bulk_calls = 0
        self.single_calls = []
        self.hold = None
        self.entered = asyncio.Event()

    async def get_states(self):
        self.bulk_calls += 1
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        return [dict(s) for s in self.states.values()]

    async def get_state(self, entity_id, **_):
        self.single_calls.append(entity_id)
        return dict(self.states[entity_id])

    def flip(self, entity_id, value):
        self.states[entity_id] = state(entity_id, value, self.states[entity_id]["attributes"]["friendly_name"])


def make(states=None):
    ha = FakeHA(states or [state("lock.front", "locked", "Front Door"), state("light.den", "off", "Den")])
    return ha, HAEntityManager(ha)


def run(coro):
    return asyncio.run(coro)


def age(manager, seconds):
    manager._cache_time = datetime.now() - timedelta(seconds=seconds)


def test_a_read_older_than_max_age_refetches_and_a_younger_one_does_not():
    async def go():
        ha, m = make()
        await m.get_entities()
        ha.flip("lock.front", "unlocked")
        assert (await m.get_entities(max_age=5))["lock.front"]["state"] == "locked"
        age(m, 6)
        assert (await m.get_entities(max_age=5))["lock.front"]["state"] == "unlocked"
        assert ha.bulk_calls == 2
    run(go())


def test_other_reads_keep_the_five_minute_cache():
    async def go():
        ha, m = make()
        await m.get_entities()
        age(m, 120)
        await m.get_entities()
        assert ha.bulk_calls == 1
        age(m, 301)
        await m.get_entities()
        assert ha.bulk_calls == 2
    run(go())


def test_concurrent_refreshes_make_one_home_assistant_call():
    async def go():
        ha, m = make()
        ha.hold = asyncio.Event()
        tasks = [asyncio.create_task(m.get_entities(force_refresh=True)) for _ in range(5)]
        await ha.entered.wait()  # the first call is inside get_states, holding the lock
        for _ in range(10):      # every other task has now reached the lock
            await asyncio.sleep(0)
        ha.hold.set()
        results = await asyncio.gather(*tasks)
        assert ha.bulk_calls == 1
        assert all(r["lock.front"]["state"] == "locked" for r in results)
    run(go())


def test_a_named_read_fetches_one_entity_not_everything():
    async def go():
        ha, m = make()
        await m.get_entities()
        ha.flip("lock.front", "unlocked")
        fresh = await m.get_entity("lock.front", max_age=0)
        assert fresh["state"] == "unlocked"
        assert ha.single_calls == ["lock.front"] and ha.bulk_calls == 1
        assert (await m.get_entities())["lock.front"]["state"] == "unlocked"
    run(go())


def test_a_named_read_within_max_age_makes_no_call():
    async def go():
        ha, m = make()
        await m.get_entities()
        await m.get_entity("lock.front", max_age=5)
        assert ha.single_calls == []
        await m.get_entity("lock.front", max_age=0)
        await m.get_entity("lock.front", max_age=5)
        assert ha.single_calls == ["lock.front"]
    run(go())


def test_invalidate_marks_only_the_written_ids_stale():
    async def go():
        ha, m = make()
        await m.get_entities()
        ha.flip("lock.front", "unlocked")
        ha.flip("light.den", "on")
        m.invalidate(["lock.front"])
        cache = await m.get_entities()
        assert cache["lock.front"]["state"] == "unlocked"
        assert cache["light.den"]["state"] == "off"
        assert ha.single_calls == ["lock.front"] and ha.bulk_calls == 1
        await m.get_entities()
        assert ha.single_calls == ["lock.front"]  # marked once, read once
    run(go())


def test_is_the_door_locked_right_after_locking_reflects_the_change():
    async def go():
        ha, m = make([state("lock.front", "unlocked", "Front Door"), state("lock.back", "locked", "Back Door")])
        controller = shc.SmartHomeController(entity_manager=m, llm_router=mock.MagicMock())
        first = await controller._handle_lock_intent("get_status", None, mock.MagicMock(), "is the front door locked")
        assert first == "Front Door is unlocked."
        ha.flip("lock.front", "locked")
        m.invalidate(["lock.front"])
        age(m, 1)
        again = await controller._handle_lock_intent("get_status", None, mock.MagicMock(), "is the front door locked")
        assert again == "Front Door is locked."
        assert ha.bulk_calls == 1  # the named read used the one entity
    run(go())


def test_an_id_marked_while_a_bulk_fetch_runs_stays_marked():
    async def go():
        ha, m = make()
        ha.hold = asyncio.Event()
        task = asyncio.create_task(m.get_entities(force_refresh=True))
        await ha.entered.wait()
        ha.flip("lock.front", "unlocked")  # the write lands after the bulk read began
        m.invalidate(["lock.front"])
        ha.hold.set()
        cache = await task
        assert cache["lock.front"]["state"] == "unlocked"
        assert ha.single_calls == ["lock.front"]
    run(go())


@pytest.mark.parametrize("failure", [RuntimeError("404"), asyncio.TimeoutError(), PermissionError("withheld 401")])
def test_a_failing_single_reread_keeps_the_cache_and_never_fails_other_reads(failure):
    async def go():
        ha, m = make()
        await m.get_entities()

        async def broken(entity_id, **_):
            raise failure

        ha.get_state = broken
        m.invalidate(["lock.front"])
        cache = await m.get_entities()
        assert cache["lock.front"]["state"] == "locked" and cache["light.den"]["state"] == "off"
        assert "lock.front" not in m._stale_ids  # dropped; the next write marks it again
        m.invalidate(["lock.front"])
        ha.get_state = FakeHA.get_state.__get__(ha)
        ha.flip("lock.front", "unlocked")
        assert (await m.get_entities())["lock.front"]["state"] == "unlocked"
    run(go())
