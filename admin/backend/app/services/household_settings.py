"""admin-backend's own reads of household settings, straight from the database.

admin-backend is the writer, so it never goes through the HTTP reader in
``shared.household``. This module imports only ``shared.household_settings`` and
``shared.config``; the P6 drift test pins that.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

import structlog
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import SystemSetting
from shared.config import get_config
from shared.household_settings import (
    SECTIONS,
    SOURCE_ADMIN,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    Location,
    SourcedStr,
    parse_stored,
)

logger = structlog.get_logger()


def load(db: Session, name: str) -> Tuple[Optional[BaseModel], int]:
    """``(model, revision)`` of a stored section; ``(None, 0)`` when nothing is stored."""
    row = db.query(SystemSetting).filter(SystemSetting.key == SECTIONS[name].key).first()
    return parse_stored(name, row.value if row else None)


def resolve_sms_country_code(db: Session) -> SourcedStr:
    """The admin-saved country code, else ``SMS_DEFAULT_COUNTRY_CODE``, else ``1``."""
    section, _revision = load(db, "sms")
    if section is not None and section.default_country_code:
        return SourcedStr(section.default_country_code, SOURCE_ADMIN)
    config = get_config()
    from_env = "sms_default_country_code" in config.model_fields_set
    return SourcedStr(config.sms_default_country_code, SOURCE_ENV if from_env else SOURCE_DEFAULT)


def resolve_location(db: Session) -> Location:
    """The home location: the Base Knowledge Everyone row (D4), else the
    environment's ``DEFAULT_CITY``/``DEFAULT_STATE``."""
    # Imported here: the routes package imports this one's siblings at load.
    from app.routes.base_knowledge import location_for_services

    env_city = get_config().default_city
    env_state = os.getenv("DEFAULT_STATE", "")
    served = location_for_services(db)
    if served is not None:
        if served["city"]:
            city, state = served["city"], served["state"] or ""
        else:
            city, state = env_city, env_state
        return Location(city, state, served["text"] or "", SOURCE_ADMIN)
    text = ", ".join(p for p in (env_city, env_state) if p)
    return Location(env_city, env_state, text, SOURCE_ENV if text else SOURCE_DEFAULT)
