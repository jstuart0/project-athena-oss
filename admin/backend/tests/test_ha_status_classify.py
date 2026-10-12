"""ha_status.classify against the one truth table, plus the admin-only rules.

The fixture is the orchestrator's (tests/fixtures/ha_link/truth_table.json);
the same file drives `shared.ha_link.link_state` there, so the orchestrator's
/health bool and this card can't disagree. Time is injected.
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import ha_status

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ha_link" / "truth_table.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
NOW = datetime(2026, 10, 11, 12, 5, 0, tzinfo=timezone.utc)


def payload(case, **extra):
    return {**copy.deepcopy(case["view"]), "coverage": "cluster", "coverage_reason": None, "replicas_reporting": 2,
            "observed_at": "2026-10-11T12:05:00+00:00", **extra}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_truth_table_row(case):
    status = ha_status.classify(payload(case), None, "ok", NOW)
    assert (status.state, status.reason) == (case["state"], case["reason"])


@pytest.mark.parametrize("gateway", ["refused", "unavailable", "ok", "disabled", "zzz", None])
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_gateway_word_never_changes_state_or_reason(case, gateway):
    baseline = ha_status.classify(payload(case), None, "ok", NOW)
    status = ha_status.classify(payload(case), None, gateway, NOW)
    assert (status.state, status.reason) == (baseline.state, baseline.reason)


def _case(case_id):
    return next(c for c in CASES if c["id"] == case_id)


@pytest.mark.parametrize("case_id,state", [
    ("connected_rest_and_registry", "connected"),
    ("forbidden_once_after_success", "attention"),
    ("token_rejected_rest_only", "failing"),
])
def test_gateway_refused_is_a_muted_note_beside_each_of_connected_attention_and_failing(case_id, state):
    status = ha_status.classify(payload(_case(case_id)), None, "refused", NOW)
    assert status.state == state
    assert status.gateway == "refused" and status.gateway_sampled is True


def test_token_rejected_carries_both_timestamps():
    status = ha_status.classify(payload(_case("token_rejected_newer_than_success")), None, "ok", NOW)
    assert status.reason == "token_rejected"
    assert status.last_outcome_at == "2026-10-11T11:59:00+00:00"   # when it was rejected
    assert status.last_success_at == "2026-10-11T11:30:00+00:00"   # when it last worked


def test_a_connected_status_reports_the_success_as_the_last_outcome():
    status = ha_status.classify(payload(_case("connected_rest_and_registry")), None, "ok", NOW)
    assert status.last_outcome_at == status.last_success_at == "2026-10-11T11:59:00+00:00"
    assert status.areas == 7 and status.coverage == "cluster" and status.replicas_reporting == 2


def test_checked_at_is_now_and_a_now_earlier_than_the_timestamps_is_not_an_error():
    early = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)
    status = ha_status.classify(payload(_case("connected_rest_and_registry")), None, "ok", early)
    assert status.checked_at == "2026-10-11T09:00:00+00:00"
    assert status.state == "connected"


# -- admin-only rules ------------------------------------------------------------

@pytest.mark.parametrize("error", [
    "orchestrator_unreachable", "orchestrator_refused", "orchestrator_outdated", "orchestrator_blocked",
])
def test_a_failed_orchestrator_leg_is_unknown_and_never_a_home_assistant_failure(error):
    status = ha_status.classify(None, error, "refused", NOW)
    assert (status.state, status.reason) == ("unknown", error)
    assert status.areas is None and status.last_success_at is None
    assert status.gateway == "refused"  # still reported, muted


def test_an_error_code_that_is_not_an_orchestrator_one_reads_unreachable():
    assert ha_status.classify(None, "token_rejected", None, NOW).reason == "orchestrator_unreachable"
    assert ha_status.classify(None, "zzz", None, NOW).reason == "orchestrator_unreachable"


def test_not_configured_with_a_refused_gateway_is_still_not_configured():
    status = ha_status.classify(payload(_case("not_configured_with_failures_recorded")), None, "refused", NOW)
    assert (status.state, status.reason, status.gateway) == ("not_configured", "not_configured", "refused")


@pytest.mark.parametrize("word,expected", [
    ("ok", "ok"), ("refused", "refused"), ("unavailable", "unavailable"), ("disabled", "disabled"),
    ("zzz", "unknown"), ("<img src=x>", "unknown"), (7, "unknown"), (None, "not_checked"),
])
def test_the_gateway_word_is_a_closed_set(word, expected):
    assert ha_status.classify(payload(_case("connected_zero_areas")), None, word, NOW).gateway == expected


# -- the reason set ----------------------------------------------------------------

def test_the_reasons_produced_are_exactly_the_reason_set():
    produced = {c["reason"] for c in CASES}
    produced |= {ha_status.classify(None, e, None, NOW).reason for e in [
        "orchestrator_unreachable", "orchestrator_refused", "orchestrator_outdated", "orchestrator_blocked"]}
    assert produced == set(ha_status.REASONS)
    assert "gateway_token_rejected" not in ha_status.REASONS


# -- a payload that doesn't fit is outdated, not a 500 ---------------------------------

def good():
    return payload(_case("connected_rest_and_registry"))


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("rest"),
    lambda p: p.pop("registry"),
    lambda p: p.pop("configured"),
    lambda p: p.update(rest="nope"),
    lambda p: p.update(registry=None),
    lambda p: p.update(configured="yes"),
    lambda p: p["rest"].update(last_outcome="zzz"),
    lambda p: p["registry"].update(state="zzz"),
    lambda p: p["registry"].update(last_failure_class="zzz"),
    lambda p: p["rest"].pop("last_success_at"),
    lambda p: p["registry"].pop("areas"),
], ids=["no-rest", "no-registry", "no-configured", "rest-string", "registry-null", "configured-string",
        "unknown-outcome", "unknown-registry-state", "unknown-failure-class", "missing-rest-key", "missing-registry-key"])
def test_a_payload_that_does_not_fit_reads_orchestrator_outdated(mutate):
    p = good()
    mutate(p)
    status = ha_status.classify(p, None, "ok", NOW)
    assert (status.state, status.reason) == ("unknown", "orchestrator_outdated")


@pytest.mark.parametrize("value", [None, [], "text", 7, True])
def test_a_body_that_is_not_an_object_reads_orchestrator_outdated(value):
    assert ha_status.classify(value, None, "ok", NOW).reason == "orchestrator_outdated"


def test_integers_must_be_non_negative_and_timestamps_must_parse_else_none():
    p = good()
    p["registry"]["areas"] = "7"
    p["rest"]["consecutive_failures"] = -1
    p["rest"]["last_success_at"] = "yesterday"
    p["replicas_reporting"] = -3
    status = ha_status.classify(p, None, "ok", NOW)
    assert status.areas is None and status.replicas_reporting is None
    assert status.state in {"connected", "attention", "failing", "unknown"}  # no 500


def test_a_bool_is_not_a_count():
    p = good()
    p["registry"]["areas"] = True
    assert ha_status.classify(p, None, "ok", NOW).areas is None


def test_free_text_is_never_passed_through():
    p = good()
    p["coverage"] = "<img src=x>"
    p["coverage_reason"] = "<img src=x>"
    p["extra"] = "<img src=x>"
    status = ha_status.classify(p, None, "<img src=x>", NOW)
    blob = status.model_dump_json()
    assert "<img" not in blob and status.coverage is None


# -- the shared sign-in state (token_withheld, next_retry_at, auth_failures) ----------------------------


def _with_auth(case_id, **auth):
    base = _case(case_id)
    return payload({**base, "view": {**base["view"], "auth": {**base["view"]["auth"], **auth}}})


def test_a_latched_token_is_withheld_and_names_when_the_next_try_is():
    p = _with_auth("latched_newer_than_success", next_probe_at="2026-10-11T12:30:00+00:00")
    status = ha_status.classify(p, None, "ok", NOW)
    assert (status.reason, status.token_withheld) == ("token_rejected", True)
    assert status.next_retry_at == "2026-10-11T12:30:00+00:00"


def test_a_spent_budget_is_withheld_with_no_next_try():
    p = _with_auth("budget_spent_newer_than_success", next_probe_at="2026-10-11T12:30:00+00:00")
    status = ha_status.classify(p, None, "ok", NOW)
    assert (status.reason, status.token_withheld) == ("token_rejected", True)
    assert status.next_retry_at is None, "a spent budget is not retried until a new token is saved"


def test_a_latch_with_a_ban_newest_has_no_token_withheld():
    p = payload(_case("ban_beats_token_rejected_when_newest"))
    assert p["auth"]["latched"] is True
    status = ha_status.classify(p, None, "ok", NOW)
    assert (status.reason, status.token_withheld, status.next_retry_at) == ("ip_banned", False, None)


def test_a_connected_card_never_reports_a_withheld_token():
    status = ha_status.classify(payload(_case("connected_rest_and_registry")), None, "ok", NOW)
    assert (status.token_withheld, status.next_retry_at, status.auth_failures) == (False, None, [])


def test_auth_failures_follow_the_callers_order_and_drop_bad_entries():
    p = _with_auth("latched_newer_than_success", failures={
        "admin_ws": 1, "gateway_rest": 2, "orchestrator_rest": 3, "intruder": 5,
        "orchestrator_registry": -1, "gateway_registry": "7", "admin_rest": True,
    })
    status = ha_status.classify(p, None, "ok", NOW)
    assert status.auth_failures == [
        {"caller": "orchestrator_rest", "count": 3},
        {"caller": "gateway_rest", "count": 2},
        {"caller": "admin_ws", "count": 1},
    ]


def test_auth_failures_are_in_the_callers_order_not_sorted_either_way():
    p = _with_auth("latched_newer_than_success", failures={"admin_ws": 1, "orchestrator_registry": 4, "admin_rest": 2})
    status = ha_status.classify(p, None, "ok", NOW)
    assert [f["caller"] for f in status.auth_failures] == ["orchestrator_registry", "admin_rest", "admin_ws"]


@pytest.mark.parametrize("failures", [None, [], "x", 4])
def test_a_failures_value_that_is_not_an_object_reads_as_none(failures):
    status = ha_status.classify(_with_auth("latched_newer_than_success", failures=failures), None, "ok", NOW)
    assert status.auth_failures == [] and status.reason == "token_rejected"


@pytest.mark.parametrize("auth", ["x", 5, [], {"latched": "yes", "budget_spent": 1, "latched_at": 9}])
def test_a_malformed_auth_section_never_reads_orchestrator_outdated(auth):
    p = payload(_case("connected_rest_and_registry"), auth=auth)
    status = ha_status.classify(p, None, "ok", NOW)
    assert status.reason != "orchestrator_outdated" and status.token_withheld is False


def test_the_classifier_words_are_the_shared_vocabulary():
    from shared import ha_link

    for word in sorted(ha_link.OUTCOMES):
        p = good()
        p["rest"]["last_outcome"] = word
        assert ha_status.classify(p, None, "ok", NOW).reason != "orchestrator_outdated", word
    for word in ha_link.REGISTRY_STATES:
        p = good()
        p["registry"]["state"] = word
        assert ha_status.classify(p, None, "ok", NOW).reason != "orchestrator_outdated", word
    for bad in ("zzz", "AUTH_REJECTED"):
        p = good()
        p["rest"]["last_outcome"] = bad
        assert ha_status.classify(p, None, "ok", NOW).reason == "orchestrator_outdated"
