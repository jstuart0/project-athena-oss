"""The admin metrics route stores and returns `prompt_tokens`, from the same JSON file
the router's test asserts its posted payload against."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.auth.oidc import get_current_user
from app.models import LLMPerformanceMetric
from main import app

URL = "/api/llm-backends/metrics"
FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm_metric_payload_v1.json"


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user

    app.dependency_overrides[get_current_user] = _get_user
    yield client


def _payload(**overrides):
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body.update(overrides)
    return body


def _stored(db, request_id):
    return db.query(LLMPerformanceMetric).filter(LLMPerformanceMetric.request_id == request_id).one()


def test_the_shared_fixture_posts_and_reads_back(owner_client, db):
    resp = owner_client.post(URL, json=_payload(request_id="fx-1"))
    assert resp.status_code == 201, resp.text
    assert _stored(db, "fx-1").prompt_tokens == 321
    listed = [m for m in owner_client.get(URL).json() if m["request_id"] == "fx-1"]
    assert listed and listed[0]["prompt_tokens"] == 321 and listed[0]["tokens_generated"] == 12


def test_a_reported_zero_is_stored_as_zero_not_null(owner_client, db):
    assert owner_client.post(URL, json=_payload(request_id="fx-zero", prompt_tokens=0)).status_code == 201
    assert _stored(db, "fx-zero").prompt_tokens == 0
    listed = [m for m in owner_client.get(URL).json() if m["request_id"] == "fx-zero"]
    assert listed[0]["prompt_tokens"] == 0


@pytest.mark.parametrize("how", ["removed", "null"])
def test_a_missing_count_is_stored_as_null(owner_client, db, how):
    body = _payload(request_id=f"fx-{how}")
    if how == "removed":
        del body["prompt_tokens"]               # an older writer
    else:
        body["prompt_tokens"] = None
    assert owner_client.post(URL, json=body).status_code == 201
    assert _stored(db, f"fx-{how}").prompt_tokens is None
    listed = [m for m in owner_client.get(URL).json() if m["request_id"] == f"fx-{how}"]
    assert listed[0]["prompt_tokens"] is None


@pytest.mark.parametrize("bad", [-1, -100, "many", 1.5, [1]])
def test_a_bad_count_is_a_422_and_nothing_is_stored(owner_client, db, bad):
    resp = owner_client.post(URL, json=_payload(request_id="fx-bad", prompt_tokens=bad))
    assert resp.status_code == 422, resp.text
    assert db.query(LLMPerformanceMetric).filter(LLMPerformanceMetric.request_id == "fx-bad").count() == 0


def test_a_newer_writer_with_an_unknown_field_is_still_accepted(owner_client, db):
    assert owner_client.post(URL, json=_payload(request_id="fx-extra", something_new=1)).status_code == 201


FIXTURE_DIR = FIXTURE.parent
ALL_FIXTURES = sorted(FIXTURE_DIR.glob("llm_metric_payload_v1*.json"))


def test_the_router_fixtures_are_all_present():
    assert len(ALL_FIXTURES) >= 3
    assert {"llm_metric_payload_v1.json", "llm_metric_payload_v1_full.json",
            "llm_metric_payload_v1_stream_early_stop.json"} <= {p.name for p in ALL_FIXTURES}


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=[p.name for p in ALL_FIXTURES])
def test_every_router_fixture_is_accepted_by_the_real_route_and_stored(owner_client, db, path):
    """What the router posts (including an early-stopped stream's row and a row with every label) is a valid
    LLMMetricCreate: a 422 here would mean the row silently never lands."""
    body = json.loads(path.read_text(encoding="utf-8"))
    body["request_id"] = f"fx-{path.stem}"
    resp = owner_client.post(URL, json=body)
    assert resp.status_code == 201, (path.name, resp.text)
    stored = _stored(db, body["request_id"])
    assert stored.prompt_tokens == body["prompt_tokens"]
    assert stored.backend == body["backend"] and stored.stage == body["stage"]
    assert (stored.user_id, stored.zone, stored.intent) == (body["user_id"], body["zone"], body["intent"])


def test_a_row_without_a_backend_is_the_422_the_router_must_never_cause(owner_client):
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body["backend"] = None
    assert owner_client.post(URL, json=body).status_code == 422
