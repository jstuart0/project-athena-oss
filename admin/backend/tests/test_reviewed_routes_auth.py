"""The 93 routes that used to answer anonymously: the full credential
matrix on every one of them, and what stays unwritten when a request is
refused.

Real app, real dependencies, real tokens: DEV_MODE's user bypass is off
(two cases turn it back on), users authenticate with Bearer JWTs, and
nothing overrides an auth dependency. Only the outbound calls the handlers
make are stubbed, and a socket guard fails any test that dials out anyway.

Ids are ``g<group>_<route file>_<n>-<case>``. The groups are the order the
routes are gated in (42, 23 and 28 routes), and the write tests carry their
route's group id too, so ``-k`` selects a group's whole gate:

- ``-k g1_``: 492 tests (462 matrix cases, 30 write and author tests);
- ``-k "g1_ or g2_"``: 748 (715 matrix cases, 33 others);
- the whole matrix: 1023 cases (93 routes x 11 credentials).
"""
from __future__ import annotations

import enum
import inspect
import os
import re
import socket
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.routing import compile_path

from app.auth.oidc import create_access_token
from app.database import get_db
from app.models import (
    Alert, CloudLLMProvider, CloudLLMUsage, EscalationEvent, EscalationState, ExternalAPIKey,
    GatewayConfig, LLMPerformanceMetric, ModelDownload, MusicConfig, RagService, SMSCostTracking,
    ToolProposal,
)
from app.utils.encryption import encrypt_value
from shared.config import _clear_cache_for_tests, get_config
from shared.route_walk import iter_api_routes

from tests.conftest import app, engine
from tests.test_route_auth_population import (
    PROGRESS_OP, REVIEWED, REVIEWED_BY_FILE, REVIEWED_GROUPS,
)

# The Control Agent's progress callback body. Read as a literal by
# tests/unit/test_reviewed_route_callers_wire.py, which pins the sender to it.
PROGRESS_BODY_KEYS = (
    "status", "progress_percent", "downloaded_bytes", "error_message", "download_path",
    "ollama_model_name", "ollama_imported",
)

CASES = (
    "none", "svc_correct", "svc_wrong", "svc_unset", "svc_non_ascii", "operator", "viewer",
    "owner_svc_wrong", "owner_svc_non_ascii", "dev_none", "dev_svc_wrong",
)

OPS = [
    (f"{group}_{name[:-3]}_{index}", op)
    for group in ("g1", "g2", "g3")
    for name in REVIEWED_GROUPS[group]
    for index, op in enumerate(REVIEWED_BY_FILE[name])
]
OP_ID = {op: op_id for op_id, op in OPS}
MATRIX = [(op_id, op, case) for op_id, op in OPS for case in CASES]
MATRIX_IDS = [f"{op_id}-{case}" for op_id, _op, case in MATRIX]

ALERTS_READ = ("GET", "/api/alerts/public/active-by-type")

# What each route answers to an accepted request built by `_request`: 200
# unless listed. Recorded at the base commit by calling every route with no
# credential, while they were anonymous, so each value is the handler's own
# answer to that request and never an auth result.
_PLACEHOLDER_404 = "no row for the placeholder path parameter"
_NOT_200 = {
    ("POST", "/api/cloud-llm-usage"): (201, "the route's declared status"),
    ("POST", "/api/llm-backends/metrics"): (201, "the route's declared status"),
    ("GET", "/api/cloud-providers/pricing/{provider}/{model_id}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/cloud-providers/{provider}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/cloud-providers/{provider}/health"): (404, _PLACEHOLDER_404),
    ("GET", "/api/component-models/component/{component_name}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/escalation/presets/active/public"): (404, "no active preset in an empty database"),
    ("GET", "/api/model-configs/public/{model_name:path}"): (404, "no row and no _default config"),
    PROGRESS_OP: (404, _PLACEHOLDER_404),
    ("GET", "/api/room-audio/internal/{room_name}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/room-tv/internal/{room_name}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/service-registry/services/{service_name}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/service-registry/services/{service_name}/url"): (404, _PLACEHOLDER_404),
    ("GET", "/api/tool-calling/tools/by-name/{tool_name}/api-keys/public"): (404, _PLACEHOLDER_404),
    ("GET", "/api/tool-calling/tools/{tool_id}/api-keys/public"): (404, _PLACEHOLDER_404),
    ("GET", "/api/tool-proposals/{proposal_id}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/voice-interfaces/public/{interface_name}"): (404, _PLACEHOLDER_404),
    ("GET", "/api/voice-config/services/{service_type}"): (404, "no STT service row in an empty database"),
}
EXPECTED_PASS = {op: _NOT_200.get(op, (200, ""))[0] for _op_id, op in OPS}

PIPELINES = {"result": {"pipelines": [
    {"id": "p-simple", "name": "Ollama", "conversation_engine": "conversation.ollama_conversation"},
], "preferred_pipeline": "p-simple"}}


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

_PATH_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")
_PARAM_VALUES = {
    # Validated by the handler, not by a type: 'stt' or 'tts'.
    ("GET", "/api/voice-config/services/{service_type}"): {"service_type": "stt"},
}
_QUERY = {
    ALERTS_READ: {"alert_type": "stuck_sensor"},
    ("POST", "/api/alerts/public/resolve-by-entity"): {"entity_id": "binary_sensor.zz_motion"},
    ("GET", "/api/cloud-llm-usage/summary/range"): {"start_date": "2026-01-01", "end_date": "2026-01-31"},
}
_BODY = {
    ("POST", "/api/alerts/public/create"): {
        "alert_type": "stuck_sensor", "title": "Stuck sensor", "message": "No change for a day",
        "entity_id": "binary_sensor.zz_motion", "dedup_key": "stuck_sensor_zz",
    },
    ("POST", "/api/cloud-llm-usage"): {"provider": "openai", "model": "gpt-zz", "input_tokens": 1, "cost_usd": 0.01},
    ("POST", "/api/escalation/state/internal"): {
        "session_id": "zz-session", "escalated_to": "super_complex", "turns_remaining": 5},
    ("POST", "/api/escalation/events/internal"): {"session_id": "zz-session", "to_model": "complex"},
    ("POST", "/api/llm-backends/metrics"): {
        "timestamp": 1700000000.0, "model": "m1", "backend": "ollama", "latency_seconds": 0.5,
        "tokens": 10, "tokens_per_second": 20.0},
    ("POST", "/api/mcp-security/check-domain"): {"url": "https://mcp.example.org/sse"},
    PROGRESS_OP: {
        "status": "processing", "progress_percent": 50.0, "downloaded_bytes": 1024, "error_message": None,
        "download_path": None, "ollama_model_name": None, "ollama_imported": False},
    ("POST", "/api/tool-proposals"): {
        "name": "zz_tool", "description": "A tool", "trigger_phrases": ["do the thing"],
        "workflow_definition": {"nodes": []}},
}


def _walked(op):
    method, path = op
    for walked in iter_api_routes(app):
        if walked.path == path and method in walked.methods:
            return walked
    raise AssertionError(op)


def _url(op, overrides=None):
    signature = inspect.signature(_walked(op).route.endpoint)
    values = {**_PARAM_VALUES.get(op, {}), **(overrides or {})}

    def fill(match):
        name = match.group(1)
        if name in values:
            return str(values[name])
        annotation = signature.parameters[name].annotation if name in signature.parameters else str
        if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
            return str(next(iter(annotation)).value)
        return "999999" if annotation is int else "zz-placeholder"

    return _PATH_PARAM.sub(fill, op[1])


def _request(op, path_params=None):
    """(method, url, kwargs): placeholders for path parameters, and a
    schema-valid query and body, so an auth result is never a 422 in
    disguise."""
    kwargs = {}
    if op in _QUERY:
        kwargs["params"] = dict(_QUERY[op])
    if op in _BODY:
        kwargs["json"] = dict(_BODY[op])
    return op[0], _url(op, path_params), kwargs


# ---------------------------------------------------------------------------
# No outbound I/O
# ---------------------------------------------------------------------------

_LOOPBACK = ("127.", "::1", "localhost")
_ALL_ATTEMPTS: list = []


def _is_loopback(host) -> bool:
    text = host.decode() if isinstance(host, bytes) else str(host)
    return text.startswith(_LOOPBACK[0]) or text in _LOOPBACK[1:]


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch):
    """Records and refuses every connect to a non-loopback address, and
    fails the test at teardown if there was one."""
    attempts: list = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            attempts.append(address[:2])
            _ALL_ATTEMPTS.append(address[:2])
            raise ConnectionRefusedError("a test tried to open a real connection")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield attempts
    assert attempts == [], f"real connection(s) attempted: {attempts}"


class Probe:
    """Stands in for every outbound HTTP call a handler makes: an empty 200,
    counted."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={})

    @property
    def calls(self):
        return len(self.requests)


def _served_globals(path):
    """Globals of the module the served route was defined in (another test
    file evicts and re-imports app.* mid-suite, so a fresh import can be a
    different module object from the one the app's routes use)."""
    (endpoint,) = {w.route.endpoint for w in iter_api_routes(app) if w.path == path}
    return endpoint.__globals__


def _date_trunc(unit, value):
    """SQLite has no date_trunc; GET /api/cloud-llm-usage/analytics/hourly
    uses it. Hour precision is all that route asks for."""
    return None if value is None else str(value)[:13] + ":00:00"


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", False)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    monkeypatch.setitem(
        _served_globals("/api/ha-pipelines/pipelines"), "ha_websocket_command", AsyncMock(return_value=PIPELINES))
    connection = engine.raw_connection()
    connection.driver_connection.create_function("date_trunc", 2, _date_trunc)
    _clear_cache_for_tests()
    yield
    connection.driver_connection.create_function("date_trunc", 2, None)
    _clear_cache_for_tests()


@pytest.fixture(scope="module", autouse=True)
def _leaves_the_config_as_the_environment_says():
    """After the last test here, whatever config object is cached (or would
    be built) matches the environment: a later file can't inherit an empty
    service key from the `svc_unset` cases."""
    yield
    assert get_config().service_api_key == os.environ.get("SERVICE_API_KEY", "") != ""


@pytest.fixture
def config_env():
    """Set a config environment variable for one test. Undone in this order:
    the variable first, then the config cache, so the next test (in this or
    a later file) can't inherit a config object built from the patched
    value."""
    patch = pytest.MonkeyPatch()

    def set_variable(name, value):
        patch.setenv(name, value)
        _clear_cache_for_tests()

    yield set_variable
    patch.undo()
    _clear_cache_for_tests()


def _dev_bypass_on(monkeypatch):
    from tests.conftest import get_current_user as served_get_current_user

    monkeypatch.setitem(served_get_current_user.__globals__, "DEV_MODE", True)
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", True)


@pytest.fixture
def api(db, monkeypatch):
    """A client that reports a crash as status 500 instead of re-raising,
    with every outbound httpx call answered by `api.probe`. The stub goes in
    after the app has started and comes out before it stops."""
    registry = _served_globals("/api/modules/refresh-all")["module_registry"]
    probe = Probe()
    real_async_client = httpx.AsyncClient

    def stub(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=httpx.MockTransport(probe.handler), **kwargs)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            registry._http_client = None
            registry.invalidate_cache()
            with monkeypatch.context() as patch:
                patch.setattr(httpx, "AsyncClient", stub)
                client.probe = probe
                yield client
            registry._http_client = None
    finally:
        app.dependency_overrides.clear()


def _bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def _service_key():
    return {"X-Service-Key": get_config().service_api_key}


@pytest.fixture
def owner(test_user):
    return test_user


def _creds(case, *, owner, viewer, operator, monkeypatch, config_env):
    if case in ("none", "dev_none"):
        headers = {}
    elif case == "svc_correct":
        headers = _service_key()
    elif case in ("svc_wrong", "dev_svc_wrong"):
        headers = {"X-Service-Key": "wrong-key"}
    elif case == "svc_unset":
        config_env("SERVICE_API_KEY", "")
        headers = {"X-Service-Key": "anything"}
    elif case == "svc_non_ascii":
        headers = {"X-Service-Key": b"k\xff"}
    elif case == "operator":
        headers = _bearer(operator)
    elif case == "viewer":
        headers = _bearer(viewer)
    elif case == "owner_svc_wrong":
        headers = {**_bearer(owner), "X-Service-Key": "wrong-key"}
    elif case == "owner_svc_non_ascii":
        headers = {**_bearer(owner), "X-Service-Key": b"k\xff"}
    else:
        raise AssertionError(case)
    if case.startswith("dev_"):
        _dev_bypass_on(monkeypatch)
    return headers


P = "pass"
#                      service_or_user  user  service
EXPECTED = {
    "none":            (401, 401, 422),
    "svc_correct":     (P, 401, P),
    "svc_wrong":       (401, 401, 401),
    "svc_unset":       (503, 401, 503),
    "svc_non_ascii":   (401, 401, 401),
    "operator":        (P, P, 422),
    "viewer":          (403, 403, 422),
    "owner_svc_wrong": (401, 401, 401),
    # A key that can't even be compared is still a key: no fall-through to
    # the valid user sent with it.
    "owner_svc_non_ascii": (401, 401, 401),
    "dev_none":        (P, P, 422),
    "dev_svc_wrong":   (401, 401, 401),
}
_KIND_COLUMN = {"service_or_user": 0, "user": 1, "service": 2}


def _expected(op, case):
    kind, _permission = REVIEWED[op]
    if case == "viewer" and op == ALERTS_READ:
        return EXPECTED_PASS[op]  # the one route a scoped role may read
    value = EXPECTED[case][_KIND_COLUMN[kind]]
    return EXPECTED_PASS[op] if value == P else value


# B1 -----------------------------------------------------------------------

def test_matrix_population():
    assert len(OPS) == 93
    assert len(MATRIX) == 1023
    assert len(set(MATRIX_IDS)) == 1023
    pattern = re.compile(r"^g[123]_[a-z_]+_\d+-[a-z_]+$")
    assert all(pattern.match(case_id) for case_id in MATRIX_IDS)
    by_group = {group: sum(1 for op_id, _ in OPS if op_id.startswith(group + "_")) for group in ("g1", "g2", "g3")}
    assert by_group == {"g1": 42, "g2": 23, "g3": 28}
    assert OP_ID[("GET", "/api/features/public")].startswith("g2_")
    assert OP_ID[PROGRESS_OP].startswith("g1_")
    assert OP_ID[("GET", "/api/voice-config/running-config")].startswith("g3_")
    assert {case for _i, _o, case in MATRIX} == set(EXPECTED) == set(CASES)
    assert len(CASES) == 11


# B2 -----------------------------------------------------------------------

def _first_match(method, url_path):
    """The route the app serves this request with: the first, in
    registration order, whose path and method both match."""
    for walked in iter_api_routes(app):
        if method in walked.methods and compile_path(walked.path)[0].match(url_path):
            return walked.path
    return None


def _misrouted():
    found = {}
    for _op_id, op in OPS:
        method, url, _kwargs = _request(op)
        served_by = _first_match(method, url)
        if served_by != op[1]:
            found[op] = served_by
    return found


def test_matrix_urls_resolve_to_their_own_route():
    misrouted = _misrouted()
    assert not misrouted, (
        f"{len(misrouted)} request(s) answered by a different route than the one under test: {misrouted}"
    )


def test_first_match_self_test():
    assert _first_match("GET", "/api/modules/enabled") == "/api/modules/enabled"
    assert _first_match("GET", "/api/modules/zz-placeholder") == "/api/modules/{module_id}"
    assert _first_match("GET", "/api/tool-proposals/stats/summary") == "/api/tool-proposals/stats/summary"
    assert _first_match("GET", "/api/cloud-providers/health/all") == "/api/cloud-providers/health/all"
    assert _first_match("GET", "/api/zz-no-such-route") is None


# B3 -----------------------------------------------------------------------

def test_expected_pass_codes_are_pinned():
    assert set(EXPECTED_PASS) == set(REVIEWED)
    assert len(EXPECTED_PASS) == 93
    assert set(_NOT_200) <= set(REVIEWED)
    assert all(reason.strip() for _status, reason in _NOT_200.values())
    assert not [op for op, status in EXPECTED_PASS.items() if status in {401, 403, 422, 503}]
    assert all(status < 500 for status in EXPECTED_PASS.values())
    assert sum(1 for status in EXPECTED_PASS.values() if status == 200) >= 60


# B4 -----------------------------------------------------------------------

@pytest.mark.parametrize("op,case", [(op, case) for _i, op, case in MATRIX], ids=MATRIX_IDS)
def test_credential_matrix(op, case, api, owner, viewer_user, operator_user, monkeypatch, config_env):
    headers = _creds(case, owner=owner, viewer=viewer_user, operator=operator_user, monkeypatch=monkeypatch,
                     config_env=config_env)
    method, url, kwargs = _request(op)
    response = api.request(method, url, headers=headers, **kwargs)
    assert response.status_code == _expected(op, case), (
        f"{method} {op[1]} [{REVIEWED[op][0]}] with {case}: {response.status_code} {response.text[:200]}"
    )


# B6 -----------------------------------------------------------------------

def _alert(db, **overrides):
    values = dict(alert_type="stuck_sensor", severity="warning", title="Stuck", message="m",
                  entity_id="binary_sensor.zz_motion", status="active", alert_data={})
    values.update(overrides)
    row = Alert(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_support_role_keeps_the_alerts_read(api, db, support_user):
    _alert(db, entity_id="binary_sensor.zz_seeded")
    alerts = api.get("/api/alerts/public/active-by-type", params={"alert_type": "stuck_sensor"},
                     headers=_bearer(support_user))
    assert alerts.status_code == 200, alerts.text
    assert "binary_sensor.zz_seeded" in alerts.text
    features = api.get("/api/features/public", headers=_bearer(support_user))
    assert features.status_code == 403, features.text


# B7 / B8: the 11 writes ----------------------------------------------------

def _state(db, **overrides):
    values = dict(session_id="zz-session", escalated_to="complex", turns_remaining=3)
    values.update(overrides)
    row = EscalationState(**values)
    db.add(row)
    db.commit()
    return row


def _download(db):
    row = ModelDownload(repo_id="zz/model", filename="model.gguf", model_format="gguf", status="pending")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fresh(db, model, **filters):
    db.expire_all()
    return db.query(model).filter_by(**filters).first()


def _count(model):
    return lambda db, api, seeded: db.query(model).count()


class Write:
    """One side-effecting operation: how to seed it, what to send, and the
    observable that tells whether it happened."""

    def __init__(self, op, observe, *, seed=None, path_params=None, credential="service",
                 accepted=None, note=""):
        self.op, self.observe, self.seed = op, observe, seed or (lambda db: None)
        self.path_params = path_params or (lambda seeded: None)
        self.credential = credential
        # The status of an accepted request, when this test's seeded request
        # isn't the matrix's placeholder request.
        self.accepted = accepted if accepted is not None else EXPECTED_PASS[op]
        self.note = note

    @property
    def id(self):
        return OP_ID[self.op]

    def send(self, api, seeded, headers):
        method, url, kwargs = _request(self.op, self.path_params(seeded))
        return api.request(method, url, headers=headers, **kwargs)


def _real_module_id():
    """A module with a component that has a health endpoint, so a refresh
    really probes."""
    modules = _served_globals("/api/modules/refresh-all")["MODULES"]
    return next(mid for mid, module in modules.items()
                if any(component.health_endpoint for component in module.components))


WRITES = [
    Write(("POST", "/api/alerts/public/create"), _count(Alert)),
    Write(("POST", "/api/alerts/public/resolve-by-entity"),
          lambda db, api, seeded: _fresh(db, Alert, id=seeded).status,
          seed=lambda db: _alert(db).id),
    Write(("POST", "/api/cloud-llm-usage"), _count(CloudLLMUsage)),
    Write(("POST", "/api/escalation/state/internal"),
          lambda db, api, seeded: (_fresh(db, EscalationState, session_id="zz-session").escalated_to,
                                   _fresh(db, EscalationState, session_id="zz-session").turns_remaining),
          seed=lambda db: _state(db).id),
    Write(("POST", "/api/escalation/events/internal"), _count(EscalationEvent)),
    Write(("PUT", "/api/escalation/state/{session_id}/decrement"),
          lambda db, api, seeded: _fresh(db, EscalationState, session_id="zz-session").turns_remaining,
          seed=lambda db: _state(db).id, path_params=lambda seeded: {"session_id": "zz-session"}),
    Write(("POST", "/api/llm-backends/metrics"), _count(LLMPerformanceMetric)),
    Write(("POST", "/api/tool-proposals"), _count(ToolProposal)),
    Write(PROGRESS_OP, lambda db, api, seeded: _fresh(db, ModelDownload, id=seeded).status,
          seed=lambda db: _download(db).id, path_params=lambda seeded: {"download_id": seeded},
          accepted=200, note="a seeded download row; the matrix's placeholder id is a 404"),
    Write(("POST", "/api/modules/refresh-all"), lambda db, api, seeded: api.probe.calls, credential="operator"),
    Write(("POST", "/api/modules/{module_id}/refresh"), lambda db, api, seeded: api.probe.calls,
          path_params=lambda seeded: {"module_id": _real_module_id()}, credential="operator"),
]


def test_write_population():
    assert len(WRITES) == 11
    assert ("POST", "/api/tool-proposals") in {w.op for w in WRITES}
    non_get = {op for op in REVIEWED if op[0] != "GET"}
    assert {w.op for w in WRITES} == non_get - {("POST", "/api/mcp-security/check-domain")}
    assert set(_BODY[PROGRESS_OP]) == set(PROGRESS_BODY_KEYS)


def _write_headers(write, operator):
    return _bearer(operator) if write.credential == "operator" else _service_key()


@pytest.mark.parametrize("write", WRITES, ids=[w.id for w in WRITES])
def test_anonymous_write_is_refused_and_writes_nothing(write, api, db):
    seeded = write.seed(db)
    before = write.observe(db, api, seeded)
    response = write.send(api, seeded, {})
    refused = 422 if REVIEWED[write.op][0] == "service" else 401
    assert response.status_code == refused, f"{write.op}: {response.status_code} {response.text[:200]}"
    assert write.observe(db, api, seeded) == before, f"{write.op}: the refused write landed"


@pytest.mark.parametrize("write", WRITES, ids=[w.id for w in WRITES])
def test_credentialed_write_lands(write, api, db, operator_user):
    seeded = write.seed(db)
    before = write.observe(db, api, seeded)
    response = write.send(api, seeded, _write_headers(write, operator_user))
    assert response.status_code == write.accepted, f"{write.op}: {response.status_code} {response.text[:200]}"
    assert write.observe(db, api, seeded) != before, f"{write.op}: an accepted write changed nothing"


# B9 / B10: the 5 GETs that write --------------------------------------------

def _provider(db, owner):
    db.add(CloudLLMProvider(provider="openai", display_name="OpenAI", enabled=True))
    db.add(ExternalAPIKey(service_name="openai", api_name="OpenAI", api_key_encrypted=encrypt_value("zz-provider-key"),
                          endpoint_url="https://api.openai.example", enabled=True, created_by_id=owner.id))
    db.commit()


def _provider_health(db, api, seeded):
    return (_fresh(db, CloudLLMProvider, provider="openai").last_health_check, api.probe.calls)


WRITING_GETS = [
    Write(("GET", "/api/escalation/state/{session_id}/public"),
          lambda db, api, seeded: _fresh(db, EscalationState, session_id="zz-session") is not None,
          seed=lambda db: _state(db, turns_remaining=0).id,
          path_params=lambda seeded: {"session_id": "zz-session"}),
    Write(("GET", "/api/cloud-providers/{provider}/health"), _provider_health,
          path_params=lambda seeded: {"provider": "openai"}, credential="operator", accepted=200,
          note="a seeded provider; the matrix's placeholder provider is a 404"),
    Write(("GET", "/api/gateway-config/public"), _count(GatewayConfig)),
    Write(("GET", "/api/music-config/internal"), _count(MusicConfig)),
    Write(("GET", "/api/music-config/browser-playback"), _count(MusicConfig), credential="operator"),
]
# What each observable is before the GET runs: the row is there, or the
# table is empty. Asserted, so a fixture that pre-seeds can't make the case
# vacuous.
_PRECONDITION = {
    ("GET", "/api/escalation/state/{session_id}/public"): True,
    ("GET", "/api/cloud-providers/{provider}/health"): (None, 0),
    ("GET", "/api/gateway-config/public"): 0,
    ("GET", "/api/music-config/internal"): 0,
    ("GET", "/api/music-config/browser-playback"): 0,
}
REJECTED_GETS = [(get, "none") for get in WRITING_GETS] + [
    (get, "svc_correct") for get in WRITING_GETS if REVIEWED[get.op][0] == "user"
]


def test_writing_get_population():
    assert len(WRITING_GETS) == 5
    assert ("GET", "/api/escalation/state/{session_id}/public") in {g.op for g in WRITING_GETS}
    assert len(REJECTED_GETS) == 7
    assert {g.op for g in WRITING_GETS} == set(_PRECONDITION)
    assert len(WRITES) + len(WRITING_GETS) == 16


def _seed_get(get, db, owner):
    if get.op == ("GET", "/api/cloud-providers/{provider}/health"):
        _provider(db, owner)
        return None
    return get.seed(db)


@pytest.mark.parametrize("get,credential", REJECTED_GETS, ids=[f"{g.id}-{c}" for g, c in REJECTED_GETS])
def test_rejected_get_changes_nothing(get, credential, api, db, owner):
    seeded = _seed_get(get, db, owner)
    before = get.observe(db, api, seeded)
    assert before == _PRECONDITION[get.op]
    response = get.send(api, seeded, _service_key() if credential == "svc_correct" else {})
    assert response.status_code == 401, f"{get.op}: {response.status_code} {response.text[:200]}"
    assert get.observe(db, api, seeded) == before, f"{get.op}: the refused GET wrote"
    assert api.probe.calls == 0


@pytest.mark.parametrize("get", WRITING_GETS, ids=[g.id for g in WRITING_GETS])
def test_accepted_get_still_does_its_work(get, api, db, owner, operator_user):
    seeded = _seed_get(get, db, owner)
    before = get.observe(db, api, seeded)
    assert before == _PRECONDITION[get.op]
    response = get.send(api, seeded, _write_headers(get, operator_user))
    assert response.status_code == get.accepted, f"{get.op}: {response.status_code} {response.text[:200]}"
    assert get.observe(db, api, seeded) != before, f"{get.op}: the accepted GET did none of its work"


# B11 ----------------------------------------------------------------------

ALREADY_GATED = {
    "service_or_user": ("GET", "/api/room-groups", {}),
    "service": ("POST", "/api/sms/internal/log-send",
                {"params": {"phone_number": "+15550100000", "content": "hi", "status": "sent"}}),
    "service_401": ("POST", "/api/internal/guest-mode/verify-pin", {"json": {"pin": "0000", "tier": "household"}}),
    "user": ("GET", "/api/guests", {}),
}


# The same key sent with a valid owner Bearer, on the route kind that accepts
# either credential: a comparison that gives up on the key must not fall
# through to the user.
ALREADY_GATED_KINDS = sorted(ALREADY_GATED) + ["service_or_user_with_owner_bearer"]


@pytest.mark.parametrize("kind", ALREADY_GATED_KINDS)
def test_non_ascii_key_is_refused_on_already_gated_routes(kind, api, db, owner):
    db.add(SMSCostTracking(month=datetime.now(timezone.utc).date().replace(day=1), message_count=0, segment_count=0,
                           incoming_count=0, outgoing_count=0, estimated_cost_cents=0,
                           outgoing_sms_cents=0, incoming_sms_cents=0))
    db.commit()
    headers = {"X-Service-Key": b"k\xff"}
    if kind.endswith("_with_owner_bearer"):
        headers.update(_bearer(owner))
        kind = kind[: -len("_with_owner_bearer")]
    method, url, kwargs = ALREADY_GATED[kind]
    response = api.request(method, url, headers=headers, **kwargs)
    assert response.status_code == 401, f"{method} {url}: {response.status_code} {response.text[:200]}"


# B12 ----------------------------------------------------------------------

PROPOSAL_AUTHORS = {
    "g1_key_with_body_owner": ("service", {"created_by": "owner"}, "llm"),
    "g1_operator_with_body_llm": ("operator", {"created_by": "llm"}, "user:{operator_id}"),
    "g1_key_without_the_field": ("service", {}, "llm"),
}


@pytest.mark.parametrize("caller", sorted(PROPOSAL_AUTHORS))
def test_tool_proposal_author_is_stamped_by_the_server(caller, api, db, operator_user):
    credential, extra, stored = PROPOSAL_AUTHORS[caller]
    op = ("POST", "/api/tool-proposals")
    headers = _bearer(operator_user) if credential == "operator" else _service_key()
    response = api.post("/api/tool-proposals", json={**_BODY[op], **extra}, headers=headers)
    assert response.status_code == EXPECTED_PASS[op], response.text
    (row,) = db.query(ToolProposal).all()
    assert row.created_by == stored.format(operator_id=operator_user.id)


# L14 (lives here: its route is gated with the second group) -----------------

DISABLED_SERVICE_URL = ("GET", "/api/service-registry/services/{service_name}/url")


def _fresh_rejection_log():
    """Empty the served rejection middleware's rate-limit table and rebind
    its logger: the matrix cases before this one were refused on the same
    route, and a line inside their window would be suppressed either way."""
    (served,) = [m.cls.__call__.__globals__ for m in app.user_middleware
                 if getattr(m.cls, "__name__", "") == "AuthRejectionMiddleware"]
    served["_reset_for_tests"]()
    vars(served["logger"]).pop("bind", None)


@pytest.mark.parametrize("op", [DISABLED_SERVICE_URL], ids=[OP_ID[DISABLED_SERVICE_URL]])
def test_a_route_s_own_503_to_a_valid_key_is_not_an_auth_rejection(op, api, db, monkeypatch):
    db.add(RagService(name="zz-disabled", display_name="Disabled", enabled=False, host="zz.example", port=8010))
    db.commit()
    _fresh_rejection_log()
    with structlog.testing.capture_logs() as logs:
        response = api.get("/api/service-registry/services/zz-disabled/url", headers=_service_key())
    assert response.status_code == 503, response.text
    assert "disabled" in response.text
    assert [r for r in logs if r.get("event") == "admin_auth_rejected"] == []

    # Positive control: the same request while no key is configured is a
    # refusal with the same status, and this capture sees its line.
    monkeypatch.setenv("SERVICE_API_KEY", "")
    _clear_cache_for_tests()
    try:
        with structlog.testing.capture_logs() as logs:
            refused = api.get("/api/service-registry/services/zz-disabled/url", headers={"X-Service-Key": "zz-any-key"})
    finally:
        monkeypatch.undo()
        _clear_cache_for_tests()
    assert refused.status_code == 503
    assert [(r["route"], r["reason"]) for r in logs if r.get("event") == "admin_auth_rejected"] == [
        (op[1], "service_key_unconfigured")]


# B5 (last, so the session-wide record covers every test above) --------------

def test_no_test_opened_a_real_connection(socket_guard):
    planted = ("192.0.2.1", 9)
    earlier = list(_ALL_ATTEMPTS)
    with pytest.raises(OSError):
        socket.create_connection(planted, timeout=1)
    assert socket_guard == [planted], "the guard records a real attempt"
    socket_guard.clear()
    assert earlier == [], f"earlier test(s) in this file dialed out: {earlier}"
    _ALL_ATTEMPTS.clear()

