"""Stuck-sensor detection: per-type thresholds, and one reconcile per good fetch.

`classify_motion_sensors` is pure. `_scan_motion_sensors` and
`check_stuck_sensors_now` are driven with a fake entity manager and a recorded
admin call, so nothing here touches a network.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestrator.nodes import _runtime  # noqa: E402,F401  (first orchestrator import)
from orchestrator import smart_home_controller as shc  # noqa: E402
from orchestrator.smart_home_controller import SmartHomeController  # noqa: E402

NOW = datetime.now(timezone.utc)  # the scan reads the real clock
FETCHED_AT = NOW - timedelta(seconds=90)
RECONCILE_PATH = "/api/alerts/public/stuck-sensors/reconcile"


def _entity(entity_id, state="on", hours=5.0, name=None):
    changed = (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    return entity_id, {
        "entity_id": entity_id, "state": state, "last_changed": changed,
        "attributes": {"friendly_name": name or entity_id.split(".")[1].replace("_", " ").title()},
    }


def _entities(*pairs):
    return dict(pairs)


def _controller(entities=None, error=None):
    controller = SmartHomeController.__new__(SmartHomeController)

    class _Manager:
        calls = []

        async def get_entities(self, force_refresh=False):
            self.calls.append(force_refresh)
            if error:
                raise error
            return entities

        def entities_fetched_at(self):
            return FETCHED_AT

    controller.entity_manager = _Manager()
    return controller


def _stuck_ids(entities):
    return [s["entity_id"] for s in _controller().classify_motion_sensors(entities, NOW).stuck]


class TestThresholds:
    def test_motion_on_five_hours_is_stuck(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.kitchen_motion", hours=5))) == [
            "binary_sensor.kitchen_motion"]

    def test_motion_on_three_hours_is_not_stuck(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.kitchen_motion", hours=3))) == []

    def test_pir_uses_the_motion_threshold(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.office_pir_detection", hours=5))) == [
            "binary_sensor.office_pir_detection"]

    def test_presence_on_five_hours_is_not_stuck(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.living_room_presence", hours=5))) == []

    def test_presence_on_twenty_five_hours_is_stuck(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.living_room_presence", hours=25))) == [
            "binary_sensor.living_room_presence"]

    def test_occupancy_on_five_hours_is_not_stuck_and_on_twenty_five_is(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.dining_occupancy", hours=5))) == []
        assert _stuck_ids(_entities(_entity("binary_sensor.dining_occupancy", hours=25))) == [
            "binary_sensor.dining_occupancy"]

    @pytest.mark.parametrize("entity_id", [
        "binary_sensor.kitchen_motion", "binary_sensor.living_room_presence", "binary_sensor.dining_occupancy",
    ])
    def test_off_for_any_length_is_not_stuck(self, entity_id):
        assert _stuck_ids(_entities(_entity(entity_id, state="off", hours=500))) == []

    def test_every_sensor_type_has_a_threshold(self):
        assert set(shc.STUCK_ON_HOURS_BY_TYPE) == {"motion", "pir", "occupancy", "presence"}

    def test_outdoor_sensor_is_excluded(self):
        assert _stuck_ids(_entities(_entity("binary_sensor.driveway_motion", hours=50))) == []

    def test_outdoor_patterns_are_generic_words_only(self):
        # Deployment-specific names (a street, a property) must never live in this list.
        assert set(shc.OUTDOOR_SENSOR_PATTERNS) == {
            "front_door", "back_door", "entrance_door", "entrance", "outdoor", "driveway",
            "porch", "deck", "patio", "exterior", "outside", "garage_door", "doorbell",
        }


class TestSeenHealthy:
    def _classify(self, *pairs):
        return _controller().classify_motion_sensors(_entities(*pairs), NOW)

    @pytest.mark.parametrize("state", ["unavailable", "unknown", "", None])
    def test_a_sensor_that_is_not_on_or_off_is_neither_stuck_nor_seen_healthy(self, state):
        result = self._classify(_entity("binary_sensor.kitchen_motion", state=state, hours=50))
        assert result.stuck == [] and result.seen_healthy == [] and result.classified == 1

    def test_an_unparseable_or_missing_last_changed_is_not_seen_healthy(self):
        bad = _entity("binary_sensor.kitchen_motion", state="off")
        bad[1]["last_changed"] = "not a time"
        missing = _entity("binary_sensor.office_motion", state="off")
        missing[1]["last_changed"] = ""
        result = self._classify(bad, missing)
        assert result.seen_healthy == [] and result.stuck == []

    def test_a_sensor_without_a_room_is_not_classified(self):
        result = self._classify(_entity("binary_sensor.zz_unmapped_motion", hours=1))
        assert result.classified == 0 and result.seen_healthy == []

    def test_on_and_off_sensors_within_their_limit_are_seen_healthy(self):
        result = self._classify(
            _entity("binary_sensor.kitchen_motion", state="off", hours=100),
            _entity("binary_sensor.living_room_presence", state="on", hours=5),
            _entity("binary_sensor.office_motion", state="on", hours=9),
        )
        assert sorted(result.seen_healthy) == ["binary_sensor.kitchen_motion", "binary_sensor.living_room_presence"]
        assert [s["entity_id"] for s in result.stuck] == ["binary_sensor.office_motion"]


class TestHealthyPrioritisation:
    def test_presence_in_a_room_is_the_authority_and_pir_is_not_listed(self):
        entities = _entities(
            _entity("binary_sensor.office_presence", hours=1),
            _entity("binary_sensor.office_pir_detection", hours=1),
        )
        healthy, stuck, seen, _ = _controller().classify_motion_sensors(entities, NOW)
        assert [s["entity_id"] for s in healthy] == ["binary_sensor.office_presence"]
        assert sorted(seen) == ["binary_sensor.office_pir_detection", "binary_sensor.office_presence"], \
            "the deprioritized PIR was still seen healthy"
        assert healthy[0]["is_authoritative"] is True
        assert stuck == []

    def test_stuck_motion_sensor_leaves_the_healthy_list(self):
        entities = _entities(
            _entity("binary_sensor.kitchen_motion", hours=9),
            _entity("binary_sensor.office_motion", hours=1),
        )
        healthy, stuck, seen, _ = _controller().classify_motion_sensors(entities, NOW)
        assert [s["entity_id"] for s in healthy] == ["binary_sensor.office_motion"]
        assert seen == ["binary_sensor.office_motion"]
        assert [s["entity_id"] for s in stuck] == ["binary_sensor.kitchen_motion"]
        assert stuck[0]["sensor_type"] == "motion"
        assert stuck[0]["hours_unchanged"] == pytest.approx(9, abs=0.01)


class _Admin:
    """Records the calls the controller makes to admin-backend."""

    def __init__(self, monkeypatch, status=200, body=None, error=None):
        self.requests = []
        self.status, self.body, self.error = status, body, error
        real = httpx.AsyncClient
        outer = self

        def handler(request):
            outer.requests.append(request)
            if outer.error:
                raise outer.error
            return httpx.Response(outer.status, json=outer.body if outer.body is not None else {
                "created": 1, "updated": 0, "reopened": 0, "resolved": 2})

        monkeypatch.setattr(shc, "get_admin_url", lambda: "http://admin.test")
        monkeypatch.setattr(shc, "service_key_headers", lambda: {"X-Service-Key": "k"})
        import httpx as _httpx
        monkeypatch.setattr(
            _httpx, "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}),
        )

    @property
    def reconciles(self):
        return [r for r in self.requests if r.url.path == RECONCILE_PATH]


def _run(coro):
    return asyncio.run(coro)


class TestReconcile:
    def test_one_reconcile_call_after_a_successful_fetch(self, monkeypatch):
        admin = _Admin(monkeypatch)
        entities = _entities(
            _entity("binary_sensor.kitchen_motion", hours=9),
            _entity("binary_sensor.office_motion", hours=9),
            _entity("binary_sensor.dining_motion", hours=1),
        )
        scan = _run(_controller(entities)._scan_motion_sensors())
        assert len(admin.reconciles) == 1
        import json
        sent = json.loads(admin.reconciles[0].content)["stuck"]
        body = json.loads(admin.reconciles[0].content)
        assert set(body) == {"observed_at", "stuck", "healthy"}
        assert body["observed_at"] == FETCHED_AT.isoformat(), "the query path sends the fetch time, not now"
        assert body["healthy"] == ["binary_sensor.dining_motion"]
        assert sorted(s["entity_id"] for s in sent) == ["binary_sensor.kitchen_motion", "binary_sensor.office_motion"]
        assert set(sent[0]) == {"entity_id", "room", "friendly_name", "sensor_type", "hours_on", "last_changed"}
        assert admin.reconciles[0].headers["x-service-key"] == "k"
        assert scan.reconcile == {"created": 1, "updated": 0, "reopened": 0, "resolved": 2}

    def test_nothing_stuck_still_reconciles_with_the_healthy_list_so_old_alerts_clear(self, monkeypatch):
        admin = _Admin(monkeypatch)
        _run(_controller(_entities(_entity("binary_sensor.kitchen_motion", hours=1)))._scan_motion_sensors())
        import json
        assert len(admin.reconciles) == 1
        body = json.loads(admin.reconciles[0].content)
        assert body["stuck"] == [] and body["healthy"] == ["binary_sensor.kitchen_motion"]

    def test_a_partial_list_sends_only_what_was_seen(self, monkeypatch):
        """HA is starting up: the stuck sensor is unavailable, one other is on."""
        admin = _Admin(monkeypatch)
        entities = _entities(
            _entity("binary_sensor.kitchen_motion", state="unavailable", hours=50),
            _entity("binary_sensor.office_motion", state="off", hours=1),
        )
        _run(_controller(entities)._scan_motion_sensors())
        import json
        body = json.loads(admin.reconciles[0].content)
        assert body["stuck"] == [] and body["healthy"] == ["binary_sensor.office_motion"]

    def test_not_called_when_no_motion_sensor_was_classified(self, monkeypatch):
        admin = _Admin(monkeypatch)
        entities = _entities(("light.kitchen", {"state": "on", "attributes": {}, "last_changed": ""}))
        scan = _run(_controller(entities)._scan_motion_sensors())
        assert admin.reconciles == [] and scan.reconcile is None

    def test_odd_sensors_are_clamped_so_they_cannot_422_the_request(self, monkeypatch):
        admin = _Admin(monkeypatch)
        controller = _controller()
        long_id = "binary_sensor.kitchen_motion_" + "x" * 300
        stuck = [{"entity_id": long_id, "room": "R" * 300, "friendly_name": "", "sensor_type": "motion",
                  "hours_unchanged": 9.04, "last_changed": "t" * 100}]
        _run(controller._reconcile_stuck_alerts(stuck, ["h" * 300], NOW))
        import json
        body = json.loads(admin.reconciles[0].content)
        sent = body["stuck"][0]
        assert len(sent["entity_id"]) == 200 and len(sent["room"]) == 100 and len(sent["last_changed"]) == 64
        assert sent["friendly_name"] == long_id[:255], "falls back to the entity id"
        assert len(body["healthy"][0]) == 200

    def test_a_missing_room_falls_back(self, monkeypatch):
        admin = _Admin(monkeypatch)
        stuck = [{"entity_id": "binary_sensor.a_motion", "room": None, "friendly_name": "A", "sensor_type": "motion",
                  "hours_unchanged": 5, "last_changed": None}]
        _run(_controller()._reconcile_stuck_alerts(stuck, [], NOW))
        import json
        assert json.loads(admin.reconciles[0].content)["stuck"][0]["room"] == "Unknown room"
        assert json.loads(admin.reconciles[0].content)["stuck"][0]["last_changed"] is None

    def test_logs_carry_counts_and_no_entity_ids(self, monkeypatch, caplog):
        import logging
        caplog.set_level(logging.DEBUG)
        _Admin(monkeypatch)
        entities = _entities(
            _entity("binary_sensor.kitchen_motion", hours=9),
            _entity("binary_sensor.office_pir_detection", hours=1),
            _entity("binary_sensor.office_presence", state="off", hours=1),
        )
        _run(_controller(entities)._scan_motion_sensors())
        assert "kitchen" not in caplog.text.lower() and "office" not in caplog.text.lower()
        assert all(not hasattr(r, "stuck_sensors") for r in caplog.records)

    def test_not_called_when_the_fetch_raises(self, monkeypatch):
        admin = _Admin(monkeypatch)
        controller = _controller(error=ConnectionError("ha down"))
        with pytest.raises(ConnectionError):
            _run(controller._scan_motion_sensors())
        assert admin.reconciles == []
        assert _run(controller._get_all_motion_sensors_with_stuck_detection()) == ([], [])
        assert admin.reconciles == []

    def test_not_called_when_the_entity_dict_is_empty(self, monkeypatch):
        admin = _Admin(monkeypatch)
        scan = _run(_controller({})._scan_motion_sensors())
        assert admin.reconciles == [] and scan.reconcile is None and scan.entities_seen == 0

    def test_the_query_path_keeps_the_cache_and_the_live_check_forces_a_refresh(self, monkeypatch):
        _Admin(monkeypatch)
        entities = _entities(_entity("binary_sensor.kitchen_motion", hours=1))
        query_controller = _controller(entities)
        _run(query_controller._get_all_motion_sensors_with_stuck_detection())
        assert query_controller.entity_manager.calls == [False]
        live_controller = _controller(entities)
        _run(live_controller.check_stuck_sensors_now())
        assert live_controller.entity_manager.calls == [True]

    @pytest.mark.parametrize("failure", [
        {"status": 401}, {"status": 500}, {"error": httpx.ConnectError("boom http://secret.example")},
    ])
    def test_a_failed_reconcile_is_not_an_exception_and_logs_the_class_only(self, monkeypatch, caplog, failure):
        _Admin(monkeypatch, **failure)
        entities = _entities(_entity("binary_sensor.kitchen_motion", hours=9))
        scan = _run(_controller(entities)._scan_motion_sensors())
        assert scan.reconcile is None and len(scan.stuck) == 1
        assert "secret.example" not in caplog.text


class TestLiveCheck:
    def test_summary_shape(self, monkeypatch):
        _Admin(monkeypatch)
        entities = _entities(
            _entity("binary_sensor.kitchen_motion", hours=9, name="Kitchen Motion"),
            _entity("binary_sensor.office_motion", hours=1),
        )
        result = _run(_controller(entities).check_stuck_sensors_now())
        hours_on = result["stuck"][0].pop("hours_on")  # the scan reads the real clock
        assert hours_on == pytest.approx(9.0, abs=0.2)
        assert datetime.fromisoformat(result.pop("checked_at")).tzinfo is not None
        assert result == {
            "ok": True, "cached": False, "sensors_checked": 2, "stuck_count": 1,
            "stuck": [{"entity_id": "binary_sensor.kitchen_motion", "room": "Kitchen",
                       "friendly_name": "Kitchen Motion", "sensor_type": "motion"}],
            "reconciled": True, "reconcile": {"created": 1, "updated": 0, "reopened": 0, "resolved": 2},
        }

    def test_the_forced_refresh_sends_now_as_observed_at(self, monkeypatch):
        import json
        admin = _Admin(monkeypatch)
        before = datetime.now(timezone.utc)
        _run(_controller(_entities(_entity("binary_sensor.kitchen_motion", hours=1))).check_stuck_sensors_now())
        observed = datetime.fromisoformat(json.loads(admin.reconciles[0].content)["observed_at"])
        assert before <= observed <= datetime.now(timezone.utc) and observed != FETCHED_AT

    def test_a_second_check_inside_the_cooldown_returns_the_first_answer(self, monkeypatch):
        admin = _Admin(monkeypatch)
        controller = _controller(_entities(_entity("binary_sensor.kitchen_motion", hours=9)))

        async def twice():
            return await controller.check_stuck_sensors_now(), await controller.check_stuck_sensors_now()

        first, second = _run(twice())
        assert first["cached"] is False and second["cached"] is True
        assert second["checked_at"] == first["checked_at"] and second["stuck"] == first["stuck"]
        assert controller.entity_manager.calls == [True], "Home Assistant was read once"
        assert len(admin.reconciles) == 1

    def test_concurrent_checks_run_once(self, monkeypatch):
        admin = _Admin(monkeypatch)
        controller = _controller(_entities(_entity("binary_sensor.kitchen_motion", hours=9)))

        async def together():
            return await asyncio.gather(*(controller.check_stuck_sensors_now() for _ in range(5)))

        results = _run(together())
        assert sorted(r["cached"] for r in results) == [False, True, True, True, True]
        assert len(admin.reconciles) == 1 and controller.entity_manager.calls == [True]

    def test_after_the_cooldown_it_reads_home_assistant_again(self, monkeypatch):
        admin = _Admin(monkeypatch)
        controller = _controller(_entities(_entity("binary_sensor.kitchen_motion", hours=9)))
        clock = {"now": 1000.0}
        monkeypatch.setattr(shc.time, "monotonic", lambda: clock["now"])

        async def run():
            await controller.check_stuck_sensors_now()
            clock["now"] += shc.STUCK_CHECK_COOLDOWN_SECONDS + 1
            return await controller.check_stuck_sensors_now()

        again = _run(run())
        assert again["cached"] is False and controller.entity_manager.calls == [True, True]
        assert len(admin.reconciles) == 2

    def test_a_failed_check_is_not_cached(self, monkeypatch):
        _Admin(monkeypatch, status=503)
        controller = _controller(_entities(_entity("binary_sensor.kitchen_motion", hours=9)))

        async def twice():
            await controller.check_stuck_sensors_now()
            return await controller.check_stuck_sensors_now()

        _run(twice())
        assert controller.entity_manager.calls == [True, True]

    def test_reconcile_failure_is_reported_not_hidden(self, monkeypatch):
        _Admin(monkeypatch, status=503)
        result = _run(_controller(_entities(_entity("binary_sensor.kitchen_motion", hours=9))).check_stuck_sensors_now())
        assert result["ok"] is True and result["reconciled"] is False and result["reconcile"] is None

    def test_home_assistant_failure_is_a_closed_code_with_no_exception_text(self, monkeypatch):
        admin = _Admin(monkeypatch)
        result = _run(_controller(error=RuntimeError("token abc123 rejected")).check_stuck_sensors_now())
        assert result == {"ok": False, "error": "home_assistant_unavailable"}
        assert admin.reconciles == []

    def test_empty_entity_dict_is_not_a_check(self, monkeypatch):
        admin = _Admin(monkeypatch)
        assert _run(_controller({}).check_stuck_sensors_now()) == {"ok": False, "error": "no_entities_reported"}
        assert admin.reconciles == []
