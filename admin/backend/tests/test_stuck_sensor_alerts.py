"""Stuck-sensor alerts: reconcile, the reopen-safe create route, and the
"Check sensors now" relay to the orchestrator."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.models import Alert
from app.services import stuck_sensor_alerts, stuck_sensor_check
from shared.config import get_config

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
ORCH = "http://orch.test:8001"
RECONCILE = "/api/alerts/public/stuck-sensors/reconcile"


def _sensor(entity_id="binary_sensor.kitchen_motion", room="Kitchen", name="Kitchen Motion", hours=5.0):
    return stuck_sensor_alerts.StuckSensor(
        entity_id=entity_id, room=room, friendly_name=name, sensor_type="motion", hours_on=hours)


def _row(db, entity_id="binary_sensor.kitchen_motion", status="active", dedup=True, **extra):
    values = dict(
        alert_type="stuck_sensor", severity="warning", title="old", message="old", entity_id=entity_id,
        entity_type="motion_sensor", status=status, alert_data={},
        dedup_key=f"stuck_sensor_{entity_id}" if dedup else None)
    row = Alert(**{**values, **extra})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fresh(db, row):
    db.expire_all()
    return db.get(Alert, row.id)


OBS = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
KITCHEN = "binary_sensor.kitchen_motion"


def _req(stuck=(), healthy=(), observed_at=OBS):
    return stuck_sensor_alerts.StuckSensorReconcile(
        observed_at=observed_at, stuck=list(stuck), healthy=list(healthy))


class TestReconcile:
    def test_creates_an_active_alert(self, db):
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=5.26)]), NOW)
        assert (counts["created"], counts["updated"], counts["reopened"], counts["resolved"]) == (1, 0, 0, 0)
        row = db.query(Alert).one()
        assert (row.status, row.alert_type, row.dedup_key) == ("active", "stuck_sensor", "stuck_sensor_binary_sensor.kitchen_motion")
        assert row.title == "Stuck Motion Sensor: Kitchen"
        assert row.message == ("Kitchen Motion has reported 'on' for 5.3 hours. "
                               "It's left out of occupancy answers until it changes.")
        assert row.alert_data["room"] == "Kitchen"
        assert row.alert_data["observed_at"] == OBS.isoformat()

    def test_updates_an_active_alert_when_the_hours_grew(self, db):
        row = _row(db, alert_data={"hours_unchanged": 5.0})
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=9.0)]), NOW)
        assert counts["updated"] == 1
        row = _fresh(db, row)
        assert "9.0 hours" in row.message and row.status == "active"
        assert db.query(Alert).count() == 1

    def test_a_row_is_not_rewritten_for_a_small_change(self, db):
        row = _row(db, alert_data={"hours_unchanged": 5.0, "observed_at": OBS.isoformat()})
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=5.4)], observed_at=OBS + timedelta(hours=1)), NOW)
        assert counts["updated"] == 0
        row = _fresh(db, row)
        assert row.message == "old"

    def test_an_acknowledged_alert_stays_acknowledged(self, db):
        row = _row(db, status="acknowledged", acknowledged_at=NOW, alert_data={"hours_unchanged": 5.0})
        stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=9.0)]), NOW)
        row = _fresh(db, row)
        assert row.status == "acknowledged" and row.acknowledged_at is not None
        assert "9.0 hours" in row.message

    def test_reopens_a_resolved_alert_instead_of_inserting_a_duplicate(self, db):
        row = _row(db, status="resolved", resolved_at=NOW - timedelta(days=3),
                   resolution_notes="Auto-resolved: the sensor is no longer stuck",
                   acknowledged_at=NOW - timedelta(days=4))
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor()]), NOW)
        assert (counts["created"], counts["reopened"]) == (0, 1)
        assert db.query(Alert).count() == 1
        row = _fresh(db, row)
        assert row.status == "active"
        assert (row.resolved_at, row.resolved_by_id, row.resolution_notes) == (None, None, None)
        assert (row.acknowledged_at, row.acknowledged_by_id) == (None, None)

    def test_resolves_only_sensors_seen_healthy(self, db):
        seen = _row(db, entity_id="binary_sensor.old_motion", created_at=NOW - timedelta(days=214))
        ack = _row(db, entity_id="binary_sensor.ack_motion", status="acknowledged")
        unavailable = _row(db, entity_id="binary_sensor.flaky_motion")
        missing = _row(db, entity_id="binary_sensor.gone_motion")
        no_entity = _row(db, entity_id="binary_sensor.legacy_motion", dedup=False)
        no_entity.entity_id = None
        other_type = Alert(alert_type="service_down", title="t", message="m", status="active",
                           entity_id="binary_sensor.old_motion2")
        db.add(other_type)
        db.commit()
        counts = stuck_sensor_alerts.reconcile(
            db, _req([_sensor()], healthy=["binary_sensor.old_motion", "binary_sensor.ack_motion",
                                           "binary_sensor.old_motion2"]), NOW)
        assert counts["resolved"] == 2
        for row in (seen, ack):
            row = _fresh(db, row)
            assert row.status == "resolved" and row.resolved_at is not None
            assert row.resolution_notes == stuck_sensor_alerts.AUTO_RESOLVED_NOTE
        for row in (unavailable, missing, no_entity, other_type):
            assert _fresh(db, row).status == "active", "not listed as healthy, so untouched"

    def test_partial_list_with_an_unavailable_stuck_sensor_resolves_nothing(self, db):
        row = _row(db)  # the sensor flipped to unavailable: neither stuck nor healthy in the payload
        counts = stuck_sensor_alerts.reconcile(db, _req([], healthy=["binary_sensor.other_motion"]), NOW)
        assert counts["resolved"] == 0 and _fresh(db, row).status == "active"

    def test_an_empty_healthy_list_resolves_nothing(self, db):
        row = _row(db)
        assert stuck_sensor_alerts.reconcile(db, _req([]), NOW)["resolved"] == 0
        assert _fresh(db, row).status == "active"

    def test_stuck_wins_over_healthy_for_the_same_sensor(self, db):
        row = _row(db, alert_data={"hours_unchanged": 1.0})
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=9)], healthy=[KITCHEN]), NOW)
        assert counts["resolved"] == 0 and _fresh(db, row).status == "active"

    def test_a_stale_observation_changes_nothing(self, db):
        newer = OBS + timedelta(hours=2)
        stuck_row = _row(db, alert_data={"hours_unchanged": 5.0, "observed_at": newer.isoformat()})
        healthy_row = _row(db, entity_id="binary_sensor.office_motion",
                           alert_data={"observed_at": newer.isoformat()})
        resolved = _row(db, entity_id="binary_sensor.dining_motion", status="resolved",
                        alert_data={"observed_at": newer.isoformat()})
        counts = stuck_sensor_alerts.reconcile(
            db, _req([_sensor(hours=9), _sensor("binary_sensor.dining_motion", "Dining", "Dining Motion")],
                     healthy=["binary_sensor.office_motion"]), NOW)
        assert counts["skipped_stale"] == 3
        assert (counts["created"], counts["updated"], counts["reopened"], counts["resolved"]) == (0, 0, 0, 0)
        assert _fresh(db, stuck_row).message == "old"
        assert _fresh(db, healthy_row).status == "active"
        assert _fresh(db, resolved).status == "resolved"

    def test_a_resolve_records_its_observation_so_an_older_one_cannot_reopen_it(self, db):
        _row(db)
        stuck_sensor_alerts.reconcile(db, _req([], healthy=[KITCHEN], observed_at=OBS + timedelta(hours=1)), NOW)
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=9)]), NOW)
        assert counts["reopened"] == 0 and counts["skipped_stale"] == 1

    def test_a_row_of_another_type_with_the_dedup_key_is_skipped(self, db):
        other = Alert(alert_type="service_down", title="t", message="m", status="resolved", entity_id=KITCHEN,
                      dedup_key=f"stuck_sensor_{KITCHEN}")
        db.add(other)
        db.commit()
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor()]), NOW)
        assert counts["skipped_type"] == 1 and counts["created"] == 0 and counts["reopened"] == 0
        db.refresh(other)
        assert (other.status, other.alert_type) == ("resolved", "service_down")

    def test_repeat_call_is_stable(self, db):
        stuck_sensor_alerts.reconcile(db, _req([_sensor()]), NOW)
        counts = stuck_sensor_alerts.reconcile(db, _req([_sensor()]), NOW)
        assert (counts["created"], counts["updated"]) == (0, 0)

    def test_duplicate_entities_in_one_request_make_one_row(self, db):
        stuck_sensor_alerts.reconcile(db, _req([_sensor(hours=5), _sensor(hours=6)]), NOW)
        assert db.query(Alert).count() == 1

    def test_observed_at_in_the_future_is_refused(self):
        with pytest.raises(ValueError):
            _req(observed_at=datetime.now(timezone.utc) + timedelta(hours=1))


class TestReconcileRoute:
    def _post(self, client, stuck, healthy=(), headers=None, observed_at="2026-10-12T12:00:00+00:00"):
        return client.post(RECONCILE, json={"observed_at": observed_at, "stuck": stuck, "healthy": list(healthy)},
                           headers=headers if headers is not None else {"X-Service-Key": get_config().service_api_key})

    def _body(self, **kw):
        return {"entity_id": KITCHEN, "room": "Kitchen", "friendly_name": "Kitchen Motion",
                "sensor_type": "motion", "hours_on": 5.0, **kw}

    def test_service_call_reconciles_and_returns_counts(self, client, db):
        _row(db, entity_id="binary_sensor.old_motion")
        resp = self._post(client, [self._body()], healthy=["binary_sensor.old_motion"])
        assert resp.status_code == 200, resp.text
        assert (resp.json()["created"], resp.json()["resolved"]) == (1, 1)

    def test_resolved_row_holding_the_dedup_key_does_not_500(self, client, db):
        _row(db, status="resolved")
        resp = self._post(client, [self._body()])
        assert resp.status_code == 200, resp.text
        assert resp.json()["reopened"] == 1

    def test_a_signed_in_user_is_refused(self, owner_client):
        resp = self._post(owner_client, [], headers={})
        assert resp.status_code in (401, 422)

    @pytest.mark.parametrize("bad", [
        {"room": "x" * 101}, {"friendly_name": ""}, {"hours_on": -1}, {"entity_id": "e" * 201},
    ])
    def test_bounded_fields_are_422(self, client, bad):
        assert self._post(client, [self._body(**bad)]).status_code == 422

    def test_lists_are_bounded(self, client):
        many = [self._body(entity_id=f"binary_sensor.s{i}_motion") for i in range(501)]
        assert self._post(client, many).status_code == 422
        assert self._post(client, [], healthy=[f"binary_sensor.h{i}" for i in range(5001)]).status_code == 422
        assert self._post(client, [], healthy=["e" * 201]).status_code == 422

    def test_observed_at_is_required(self, client):
        resp = client.post(RECONCILE, json={"stuck": [], "healthy": []},
                           headers={"X-Service-Key": get_config().service_api_key})
        assert resp.status_code == 422


class TestCreateReusesAClosedRow:
    def _payload(self):
        return {"alert_type": "stuck_sensor", "title": "New title", "message": "new",
                "entity_id": "binary_sensor.kitchen_motion", "dedup_key": "stuck_sensor_binary_sensor.kitchen_motion"}

    def _post(self, client):
        return client.post("/api/alerts/public/create", json=self._payload(),
                           headers={"X-Service-Key": get_config().service_api_key})

    def test_resolved_row_is_reopened_not_duplicated(self, client, db):
        row = _row(db, status="resolved", resolved_at=NOW, resolution_notes="done")
        resp = self._post(client)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == row.id and resp.json()["status"] == "active"
        assert resp.json()["title"] == "New title" and resp.json()["resolution_notes"] is None
        assert db.query(Alert).count() == 1

    def test_a_closed_row_of_another_type_is_409_and_never_retyped(self, client, db):
        row = _row(db, status="resolved")
        row.alert_type = "service_down"
        db.commit()
        resp = self._post(client)
        assert resp.status_code == 409
        assert resp.json() == {"detail": "dedup_key belongs to a different alert type"}
        db.expire_all()
        assert db.get(Alert, row.id).alert_type == "service_down" and db.get(Alert, row.id).status == "resolved"

    def test_open_row_is_returned_unchanged(self, client, db):
        row = _row(db, status="acknowledged")
        resp = self._post(client)
        assert resp.json()["id"] == row.id and resp.json()["status"] == "acknowledged"
        assert resp.json()["title"] == "old"


@pytest.fixture
def orchestrator(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", ORCH)
    monkeypatch.setattr(stuck_sensor_check, "check_ssrf_safe", AsyncMock(return_value=(True, "")))
    real = httpx.AsyncClient
    state = {"requests": [], "answer": lambda request: httpx.Response(200, json=GOOD)}

    def handler(request):
        state["requests"].append(request)
        result = state["answer"](request)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        stuck_sensor_check.httpx, "AsyncClient",
        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))
    return state


GOOD = {
    "ok": True, "cached": False, "checked_at": "2026-10-13T10:00:00+00:00", "sensors_checked": 23, "stuck_count": 1,
    "stuck": [{"entity_id": "binary_sensor.alpha_motion", "room": "Alpha", "friendly_name": "Alpha Motion",
               "sensor_type": "motion", "hours_on": 6.2}],
    "reconciled": True, "reconcile": {"created": 0, "updated": 1, "reopened": 0, "resolved": 2},
}
CHECK = "/api/alerts/stuck-sensors/check"
SECRET = "http://secret.internal:9/leak"


class TestCheckButtonRoute:
    def test_success_returns_the_summary_and_sends_the_key(self, owner_client, orchestrator):
        resp = owner_client.post(CHECK)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {k: v for k, v in GOOD.items() if k != "ok"}
        sent = orchestrator["requests"][0]
        assert str(sent.url) == f"{ORCH}/admin/sensors/stuck-check"
        assert sent.headers["x-service-key"] == get_config().service_api_key

    @pytest.mark.parametrize("answer,status,code", [
        (lambda r: httpx.ConnectError(f"boom {SECRET}"), 502, "orchestrator_unreachable"),
        (lambda r: httpx.Response(404), 502, "orchestrator_outdated"),
        (lambda r: httpx.Response(401), 502, "orchestrator_refused"),
        (lambda r: httpx.Response(302, headers={"location": SECRET}), 502, "orchestrator_refused"),
        (lambda r: httpx.Response(500, text=f"Traceback {SECRET}"), 502, "orchestrator_unreachable"),
        (lambda r: httpx.Response(200, json={"surprise": SECRET}), 502, "unexpected_response"),
        (lambda r: httpx.Response(200, text="not json " + SECRET), 502, "unexpected_response"),
        (lambda r: httpx.Response(503, json={"ok": False, "error": "home_assistant_unavailable"}), 503, "home_assistant_unavailable"),
        (lambda r: httpx.Response(503, json={"ok": False, "error": "home_assistant_not_configured"}), 503, "home_assistant_not_configured"),
        (lambda r: httpx.Response(503, json={"ok": False, "error": "no_entities_reported"}), 503, "no_entities_reported"),
        (lambda r: httpx.Response(503, json={"ok": False, "error": SECRET}), 502, "orchestrator_unreachable"),
    ])
    def test_failures_are_closed_phrases(self, owner_client, orchestrator, answer, status, code):
        orchestrator["answer"] = answer
        resp = owner_client.post(CHECK)
        assert resp.status_code == status
        assert resp.json() == {"detail": stuck_sensor_check.FAILURES[code][1]}
        assert "secret" not in resp.text and "boom" not in resp.text

    def test_blocked_address_is_a_closed_phrase_and_sends_nothing(self, owner_client, orchestrator, monkeypatch):
        monkeypatch.setattr(stuck_sensor_check, "check_ssrf_safe", AsyncMock(return_value=(False, "private")))
        resp = owner_client.post(CHECK)
        assert resp.status_code == 502
        assert resp.json() == {"detail": stuck_sensor_check.FAILURES["orchestrator_blocked"][1]}
        assert orchestrator["requests"] == []

    def test_viewer_is_403_and_nothing_is_sent(self, viewer_client, orchestrator):
        assert viewer_client.post(CHECK).status_code == 403
        assert orchestrator["requests"] == []

    def test_a_service_key_is_not_accepted(self, client, orchestrator):
        resp = client.post(CHECK, headers={"X-Service-Key": get_config().service_api_key})
        assert resp.status_code == 401 and orchestrator["requests"] == []

    def test_every_failure_code_has_a_status_and_a_phrase(self):
        for code, (status, phrase) in stuck_sensor_check.FAILURES.items():
            assert status in (502, 503) and phrase.endswith(".")
        assert set(stuck_sensor_check.ORCHESTRATOR_CODES) <= set(stuck_sensor_check.FAILURES)
