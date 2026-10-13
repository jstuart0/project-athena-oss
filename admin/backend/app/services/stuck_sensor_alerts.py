"""Stuck-sensor alerts: one place that opens, refreshes, reopens and resolves them.

The orchestrator decides which motion/presence sensors are stuck and sends
them, with the sensors it positively saw healthy, as of ``observed_at``.
``reconcile`` changes the ``stuck_sensor`` alerts for those sensors only:

- a stuck sensor gets one alert, keyed ``stuck_sensor_<entity_id>``; a row that
  was resolved earlier is reopened, never duplicated (``dedup_key`` is unique);
- an active or acknowledged alert is resolved only for a sensor listed as
  healthy. A sensor that is unavailable, missing or unreadable is left alone,
  so a partial Home Assistant answer never clears an alert;
- a row last written from a newer observation is not touched, and an open row
  is rewritten only when its hours grew noticeably.

``reopen_alert`` is shared with the generic ``/public/create`` route so both
paths treat a closed row the same way.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict

import structlog
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Alert

logger = structlog.get_logger()

ALERT_TYPE = "stuck_sensor"
ENTITY_TYPE = "motion_sensor"
OPEN_STATUSES = ("active", "acknowledged")
MAX_STUCK_SENSORS = 500
MAX_HEALTHY_SENSORS = 5000
# An open alert is rewritten only after its hours grew by this much.
MIN_HOURS_GROWTH = 0.5
# An observation this far ahead of the server clock is a bad clock, not news.
MAX_CLOCK_AHEAD = timedelta(minutes=5)
AUTO_RESOLVED_NOTE = "Auto-resolved: the sensor is no longer stuck"


class StuckSensor(BaseModel):
    """One sensor the orchestrator found stuck. Bounded: it arrives over the wire."""
    entity_id: str = Field(min_length=1, max_length=200)
    room: str = Field(min_length=1, max_length=100)
    friendly_name: str = Field(min_length=1, max_length=255)
    sensor_type: str = Field(max_length=20)
    hours_on: float = Field(ge=0, le=1_000_000)
    last_changed: str | None = Field(default=None, max_length=64)


class StuckSensorReconcile(BaseModel):
    observed_at: datetime
    stuck: list[StuckSensor] = Field(max_length=MAX_STUCK_SENSORS)
    healthy: list[str] = Field(max_length=MAX_HEALTHY_SENSORS)

    @field_validator("observed_at")
    @classmethod
    def _aware_and_not_in_the_future(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value > datetime.now(timezone.utc) + MAX_CLOCK_AHEAD:
            raise ValueError("observed_at is in the future")
        return value

    @field_validator("healthy")
    @classmethod
    def _bounded_ids(cls, ids: list[str]) -> list[str]:
        if any(not 0 < len(i) <= 200 for i in ids):
            raise ValueError("entity ids must be 1 to 200 characters")
        return ids


def dedup_key_for(entity_id: str) -> str:
    return f"{ALERT_TYPE}_{entity_id}"


def reopen_alert(alert: Alert, now: datetime) -> None:
    """Make a closed alert a fresh active one: its earlier resolution or
    acknowledgement no longer describes it."""
    alert.status = "active"
    alert.created_at = now
    alert.resolved_at = None
    alert.resolved_by_id = None
    alert.resolution_notes = None
    alert.acknowledged_at = None
    alert.acknowledged_by_id = None


def _describe(sensor: StuckSensor, observed_at: datetime) -> Dict:
    return {
        "title": f"Stuck Motion Sensor: {sensor.room}",
        "message": (
            f"{sensor.friendly_name} has reported 'on' for {sensor.hours_on:.1f} hours. "
            "It's left out of occupancy answers until it changes."
        ),
        "alert_data": {
            "room": sensor.room,
            "state": "on",
            "sensor_type": sensor.sensor_type,
            "hours_unchanged": sensor.hours_on,
            "last_changed": sensor.last_changed,
            "observed_at": observed_at.isoformat(),
        },
    }


def _stored(row: Alert, key: str):
    data = row.alert_data if isinstance(row.alert_data, dict) else {}
    return data.get(key)


def _is_newer_observation(row: Alert, observed_at: datetime) -> bool:
    """True when the row was last written from a later observation than this one."""
    raw = _stored(row, "observed_at")
    if not isinstance(raw, str):
        return False
    try:
        stored = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored > observed_at


def _hours_grew(row: Alert, hours_on: float) -> bool:
    previous = _stored(row, "hours_unchanged")
    if not isinstance(previous, (int, float)):
        return True
    return hours_on - previous >= MIN_HOURS_GROWTH


def _apply(db: Session, request: StuckSensorReconcile, now: datetime) -> Dict[str, int]:
    observed_at = request.observed_at
    by_entity = {s.entity_id: s for s in request.stuck}
    healthy = set(request.healthy) - set(by_entity)
    counts = {"created": 0, "updated": 0, "reopened": 0, "resolved": 0, "skipped_stale": 0, "skipped_type": 0}

    for entity_id, sensor in by_entity.items():
        key = dedup_key_for(entity_id)
        fields = _describe(sensor, observed_at)
        row = db.query(Alert).filter(Alert.dedup_key == key).first()
        if row is None:
            db.add(Alert(
                alert_type=ALERT_TYPE, severity="warning", entity_id=entity_id,
                entity_type=ENTITY_TYPE, dedup_key=key, status="active", **fields,
            ))
            counts["created"] += 1
            continue
        if row.alert_type != ALERT_TYPE:
            counts["skipped_type"] += 1
            continue
        if _is_newer_observation(row, observed_at):
            counts["skipped_stale"] += 1
            continue
        if row.status in OPEN_STATUSES:
            if not _hours_grew(row, sensor.hours_on):
                continue
            counts["updated"] += 1
        else:
            reopen_alert(row, now)
            counts["reopened"] += 1
        row.severity = "warning"
        row.entity_type = ENTITY_TYPE
        row.entity_id = entity_id
        row.updated_at = now
        for name, value in fields.items():
            setattr(row, name, value)

    open_rows = db.query(Alert).filter(
        Alert.alert_type == ALERT_TYPE, Alert.status.in_(OPEN_STATUSES)
    ).all()
    for row in open_rows:
        if row.entity_id not in healthy:
            continue
        if _is_newer_observation(row, observed_at):
            counts["skipped_stale"] += 1
            continue
        row.status = "resolved"
        row.resolved_at = now
        row.updated_at = now
        row.resolution_notes = AUTO_RESOLVED_NOTE
        row.alert_data = {**(row.alert_data if isinstance(row.alert_data, dict) else {}),
                          "observed_at": observed_at.isoformat()}
        counts["resolved"] += 1
    return counts


def reconcile(db: Session, request: StuckSensorReconcile, now: datetime) -> Dict[str, int]:
    """Apply `request` in one transaction.

    A concurrent reconcile on another replica can insert the same dedup_key
    first; the loser retries once and then finds the row.
    """
    for attempt in (1, 2):
        try:
            counts = _apply(db, request, now)
            db.commit()
            logger.info("stuck_sensor_alerts_reconciled", stuck=len(request.stuck),
                        healthy=len(request.healthy), **counts)
            return counts
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
    raise AssertionError("unreachable")
