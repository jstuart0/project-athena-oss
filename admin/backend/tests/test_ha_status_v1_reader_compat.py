"""An admin build from before the shared sign-in state still reads today's orchestrator.

``Frozen*`` below is a verbatim copy of the models in ``ha_status.py`` as of the
commit before the sign-in work (strict Literals, ``extra="ignore"``). Today's
golden body must parse through it to a normal view, so an admin-backend that is
rolled out after the orchestrator (or the other way round) never shows
``orchestrator_outdated`` over a body that only gained keys. The current
classifier is run on the same body and must agree on state and reason.
"""
import copy
import json
import pathlib
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.services import ha_status

FIXTURES = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ha_link"
GOLDEN = json.loads((FIXTURES / "admin_ha_status_v2.json").read_text())
NOW = datetime(2026, 10, 11, 12, 5, tzinfo=timezone.utc)

Outcome = Literal["ok", "auth_rejected", "forbidden", "ha_error", "timeout", "unreachable", "tls_error", "error"]
RegistryState = Literal["refused", "unavailable", "ok", "disabled"]
FailureClass = Literal["auth", "timeout", "unreachable", "other"]


def _nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _iso_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class _FrozenSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, value: Any, info) -> Any:
        name = info.field_name
        if name.endswith("_at"):
            return _iso_or_none(value)
        if name in ("consecutive_failures", "areas"):
            return _nonneg_int(value)
        return value


class FrozenRest(_FrozenSection):
    last_outcome: Optional[Outcome]
    last_outcome_at: Optional[str]
    last_success_at: Optional[str]
    consecutive_failures: Optional[int]


class FrozenRegistry(_FrozenSection):
    state: RegistryState
    areas: Optional[int]
    last_success_at: Optional[str]
    last_failure_at: Optional[str]
    last_failure_class: Optional[FailureClass]


class FrozenPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=False)

    configured: bool
    rest: FrozenRest
    registry: FrozenRegistry
    coverage: Optional[str] = None
    replicas_reporting: Optional[int] = None

    @field_validator("configured", mode="before")
    @classmethod
    def _configured_is_a_real_bool(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("configured must be a boolean")
        return value

    @field_validator("coverage", mode="before")
    @classmethod
    def _coverage(cls, value: Any) -> Optional[str]:
        return value if value in ("cluster", "replica") else None

    @field_validator("replicas_reporting", mode="before")
    @classmethod
    def _replicas(cls, value: Any) -> Optional[int]:
        return _nonneg_int(value)


def test_the_golden_body_parses_through_the_frozen_v1_models():
    parsed = FrozenPayload.model_validate(GOLDEN)
    assert parsed.configured is True
    assert parsed.rest.last_outcome == "ok" and parsed.registry.state == "ok" and parsed.registry.areas == 9


def test_the_golden_body_carries_the_keys_the_old_reader_ignores():
    assert {"auth"} <= set(GOLDEN)
    assert {"ban_suspected"} <= set(GOLDEN["rest"]) and {"ban_suspected"} <= set(GOLDEN["registry"])


@pytest.mark.parametrize("mutate", [
    lambda body: body.update(auth={"latched": True, "latched_at": "2026-10-11T11:59:00+00:00", "budget_spent": True, "failures": {"x": 1}}),
    lambda body: body["rest"].update(ban_suspected=True, last_outcome="forbidden"),
    lambda body: body["registry"].update(ban_suspected=True, state="refused", last_failure_class="other"),
    lambda body: body.update(coverage_reason="redis_error", new_top_level_key={"nested": [1, 2]}),
], ids=["auth-set", "rest-ban", "registry-ban", "unknown-keys"])
def test_added_keys_never_break_the_frozen_reader(mutate):
    body = copy.deepcopy(GOLDEN)
    mutate(body)
    FrozenPayload.model_validate(body)


def test_the_frozen_reader_does_reject_a_new_outcome_word_which_is_why_words_are_never_added():
    body = copy.deepcopy(GOLDEN)
    body["rest"]["last_outcome"] = "ip_banned"
    with pytest.raises(ValidationError):
        FrozenPayload.model_validate(body)


def test_the_current_classifier_reads_the_same_golden_body_without_complaint():
    status = ha_status.classify(copy.deepcopy(GOLDEN), None, "ok", NOW)
    assert (status.state, status.reason) == ("connected", "ok")
    assert status.token_withheld is False and status.auth_failures == []


def test_the_current_classifier_reads_a_v1_body_with_none_of_the_new_keys():
    body = {key: value for key, value in copy.deepcopy(GOLDEN).items() if key != "auth"}
    for section in ("rest", "registry"):
        body[section].pop("ban_suspected", None)
    status = ha_status.classify(body, None, "ok", NOW)
    assert (status.state, status.reason) == ("connected", "ok")
