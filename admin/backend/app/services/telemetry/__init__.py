"""Pseudonymous install telemetry (admin-backend is the only sender)."""
from app.services.telemetry.schema import SCHEMA_VERSION
from app.services.telemetry.sender import (
    get_status,
    request_send,
    reset_identity,
    run_cycle,
    set_admin_disabled,
    start_telemetry,
    stop_telemetry,
)

__all__ = [
    "SCHEMA_VERSION",
    "get_status",
    "request_send",
    "reset_identity",
    "run_cycle",
    "set_admin_disabled",
    "start_telemetry",
    "stop_telemetry",
]
