"""model_safe_errors: the allowlist policy for error text, its categories, and a seeded fuzz of the invariant.

What may reach a model, a speaker or a client is a fixed phrase chosen by category, or a `UserSafeText`
that `make_user_safe` vetted. Nothing an operator would recognise (env-var names, URLs, addresses, paths,
tracebacks, class names) survives inside an error object.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")
sys.modules.setdefault("prometheus_client", mock.MagicMock())

from orchestrator.model_safe_errors import (  # noqa: E402
    PHRASES,
    SCRUBBED_KEYS,
    TOOL_ERROR_FALLBACK,
    RAGToolError,
    UserSafeText,
    is_error_object,
    log_safe,
    make_user_safe,
    model_safe_error,
    scrub_tool_result,
    tool_error_result,
)

TESLA = "Service returned status 503: TESLAMATE_DB_HOST is not configured"
AMTRAK = "Service returned status 400: No origin specified and DEFAULT_AMTRAK_STATION is not configured"
AMTRAK_DETAIL = "No origin specified and DEFAULT_AMTRAK_STATION is not configured"


# --- categories --------------------------------------------------------------------------------------


def test_the_tesla_string_is_the_not_configured_phrase():
    assert model_safe_error(TESLA) == PHRASES["not_configured"]
    assert "TESLAMATE" not in model_safe_error(TESLA)


def test_a_connect_error_is_unavailable_and_carries_no_url_or_address():
    text = model_safe_error("Connection failed: All connection attempts failed http://10.0.0.5:8030")
    assert text == PHRASES["unavailable"]
    assert "10.0.0.5" not in text and "http" not in text
    assert model_safe_error(httpx.ConnectError("http://10.0.0.5:8030/x")) == PHRASES["unavailable"]


def test_the_amtrak_detail_fails_the_charset_and_the_model_gets_the_bad_request_phrase():
    assert make_user_safe(AMTRAK_DETAIL, 400) is None
    response = SimpleNamespace(status_code=400, user_detail=None, error=AMTRAK)
    error = RAGToolError(response)
    assert model_safe_error(error) == PHRASES["bad_request"]
    assert "AMTRAK" not in model_safe_error(error)
    assert str(error) == AMTRAK, "the raw text stays on the exception for logs and tool_usage_metrics"


@pytest.mark.parametrize("value,status,category", [
    ("Request timed out", None, "timeout"),
    (asyncio.TimeoutError(), None, "timeout"),
    (httpx.ReadTimeout("slow"), None, "timeout"),
    ("API key missing", None, "not_configured"),
    ("anything", 401, "not_configured"),
    ("anything", 403, "not_configured"),
    ("anything", 400, "bad_request"),
    ("anything", 404, "bad_request"),
    ("anything", 422, "bad_request"),
    ("anything", 418, "failed"),
    ("anything", 429, "unavailable"),
    ("anything", 500, "unavailable"),
    ("anything", 503, "unavailable"),
    ("boom", None, "failed"),
    (RuntimeError("boom"), None, "failed"),
    (None, None, "failed"),
    (404, None, "failed"),
])
def test_categories(value, status, category):
    assert model_safe_error(value, status_code=status) == PHRASES[category]


def test_an_http_status_error_uses_its_responses_status():
    request = httpx.Request("GET", "http://10.0.0.5/x")
    error = httpx.HTTPStatusError("bad", request=request, response=httpx.Response(503, request=request))
    assert model_safe_error(error) == PHRASES["unavailable"]


# --- make_user_safe ------------------------------------------------------------------------------------------


def test_a_compliant_4xx_detail_passes_verbatim_only_as_user_safe_text():
    detail = "No trains found for that date"
    safe = make_user_safe(detail, 404)
    assert isinstance(safe, UserSafeText) and safe == detail
    assert model_safe_error(safe) == detail
    assert model_safe_error(detail) != detail, "the same text as a plain string is replaced"


@pytest.mark.parametrize("detail,status,passes", [
    ("a" * 200, 400, True),
    ("a" * 201, 400, False),
    ("two\nlines", 400, False),
    ("carriage\rreturn", 400, False),
    ("has_underscore", 400, False),
    ("slash/path", 400, False),
    ("colon: here", 400, False),
    ("at 10.0.0.5 now", 400, False),
    ("version 1.2.3 ok", 400, False),
    ("call api.internal.corp now", 400, False),
    ("see host.example.com", 400, False),
    ("costs 3.5 dollars", 400, True),
    ("an ordinary sentence. Another one.", 400, True),
    (" ".join(["word"] * 30), 400, True),
    (" ".join(["word"] * 31), 400, False),
    ("ConnectError happened", 400, False),
    ("SomeException thrown", 400, False),
    ("Error", 400, False),
    ("Terror alert", 400, True),
    ("It's fine, really - ok.", 400, True),
    ("fine", 399, False),
    ("fine", 500, False),
    ("fine", None, False),
    ("", 400, False),
    (None, 400, False),
    (404, 400, False),
    (["No trains"], 400, False),
    ("unicode é", 400, False),
])
def test_make_user_safe_boundaries(detail, status, passes):
    assert (make_user_safe(detail, status) is not None) is passes


def test_user_safe_text_cannot_be_constructed_directly():
    with pytest.raises(TypeError):
        UserSafeText("No trains found for that date")
    with pytest.raises(TypeError):
        UserSafeText("x", object())
    assert isinstance(make_user_safe("fine", 400), UserSafeText)


def test_user_safe_text_is_constructed_only_by_make_user_safe():
    import ast
    from pathlib import Path

    source_dir = Path(__file__).resolve().parents[2] / "src"
    callers = []
    for path in [p for d in ("orchestrator", "shared", "gateway") for p in (source_dir / d).rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "UserSafeText":
                callers.append(path.name)
    assert callers == ["model_safe_errors.py"], callers


def test_a_user_safe_exception_carries_its_text_through_ragtoolerror():
    safe = make_user_safe("No trains found for that date", 404)
    error = RAGToolError(SimpleNamespace(status_code=404, user_detail=safe, error="Service returned status 404: No trains found for that date"))
    assert model_safe_error(error) == "No trains found for that date"
    assert tool_error_result(error) == {"error": "No trains found for that date"}


# --- scrub_tool_result -----------------------------------------------------------------------------------------


def test_a_nested_errors_list_is_scrubbed():
    result = {"events": [], "errors": [{"msg": "failed at /var/lib/app/db.py: TESLAMATE_KEY"}], "error": "x"}
    scrubbed = scrub_tool_result(result)
    assert "TESLAMATE" not in json.dumps(scrubbed) and "/var/lib" not in json.dumps(scrubbed)
    assert scrubbed["events"] == [] and scrubbed["errors"][0]["msg"] in PHRASES.values()


@pytest.mark.parametrize("value", [404, None, {"code": 5}, RuntimeError("secret http://10.0.0.5"), 3.5, True, ["a", 1]])
def test_non_string_error_values_become_a_fixed_phrase(value):
    scrubbed = scrub_tool_result({"error": value, "keep": 1})
    assert scrubbed["keep"] == 1
    flat = json.dumps(scrubbed) if scrubbed != TOOL_ERROR_FALLBACK else scrubbed
    assert "secret" not in flat and "10.0.0.5" not in flat


def test_an_error_with_a_falsy_value_is_not_an_error_object():
    payload = {"error": None, "message": "x"}
    assert scrub_tool_result(payload) == payload


def test_a_user_safe_error_value_survives_scrubbing():
    safe = make_user_safe("No trains found for that date", 404)
    assert scrub_tool_result({"error": safe})["error"] == "No trains found for that date"


def test_unprocessable_input_returns_the_fixed_string():
    cyclic = {"error": "x"}
    cyclic["self"] = cyclic
    assert scrub_tool_result(cyclic) == TOOL_ERROR_FALLBACK
    assert scrub_tool_result({"error": "x", "when": object()}) == TOOL_ERROR_FALLBACK
    deep = current = {"error": "x"}
    for _ in range(200):
        current["next"] = {}
        current = current["next"]
    assert scrub_tool_result(deep) == TOOL_ERROR_FALLBACK


def test_scrubbing_does_not_mutate_its_input():
    original = {"error": "bad http://10.0.0.5", "data": [1, 2]}
    snapshot = json.loads(json.dumps(original))
    scrub_tool_result(original)
    assert original == snapshot


def test_log_safe_is_class_and_status_only():
    assert log_safe(RuntimeError("secret")) == {"error_class": "RuntimeError", "http_status": None}
    assert log_safe(RAGToolError(SimpleNamespace(status_code=503, user_detail=None, error="secret"))) == {
        "error_class": "RAGToolError", "http_status": 503}


@pytest.mark.parametrize("phrase", list(PHRASES.values()))
def test_scrubbing_is_idempotent(phrase):
    assert model_safe_error(phrase) == phrase
    once = scrub_tool_result({"error": phrase, "events": []})
    assert scrub_tool_result(once) == once == {"error": phrase, "events": []}


def test_the_scrubbed_key_set_is_the_documented_one():
    assert SCRUBBED_KEYS == {"error", "detail", "message", "msg", "reason", "exception", "errors"}


# --- seeded fuzz --------------------------------------------------------------------------------------------------

ENV_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
PATH_SEGMENT = re.compile(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CLASS_NAME = re.compile(r"\b[A-Za-z0-9]*(?:Error|Exception)\b")

LEAKS = [
    "TESLAMATE_DB_HOST", "DEFAULT_AMTRAK_STATION", "http://10.0.0.5:8030/x", "postgres://u:p@db/x", "203.0.113.20",
    "/var/lib/athena/app.py", "Traceback (most recent call last):", "ConnectError", "KeyError('k')", "ValueError: nope",
]
SAFE_BITS = ["Sorry", "plain words", "42", "", " "]
KEYS = sorted(SCRUBBED_KEYS)


def _text(rng):
    return " ".join(rng.choice(LEAKS + SAFE_BITS) for _ in range(rng.randint(1, 4)))


def _value(rng, depth=0):
    kind = rng.random()
    if kind < 0.45 or depth > 3:
        return _text(rng)
    if kind < 0.55:
        return rng.choice([404, 0, 3.14, None, True, False])
    if kind < 0.65:
        return RuntimeError(_text(rng))
    if kind < 0.8:
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(KEYS + ["other", "x"]): _value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def _error_object(rng):
    body = {rng.choice(KEYS): _value(rng) for _ in range(rng.randint(1, 4))}
    body["error"] = body.get("error") or _text(rng) or "x"       # a truthy error makes it an error object
    return body


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def _assert_clean(text):
    assert not ENV_TOKEN.search(text), text
    assert "://" not in text and "Traceback" not in text, text
    assert not IPV4.search(text) and not PATH_SEGMENT.search(text) and not CLASS_NAME.search(text), text


def _holds_an_error_object(value):
    if isinstance(value, dict):
        return is_error_object(value) or any(_holds_an_error_object(v) for v in value.values())
    if isinstance(value, list):
        return any(_holds_an_error_object(v) for v in value)
    return False


def test_fuzz_error_objects_never_leak_and_never_raise():
    rng = random.Random(20261010)
    for _ in range(3000):
        scrubbed = scrub_tool_result(_error_object(rng))
        if scrubbed == TOOL_ERROR_FALLBACK:
            continue
        for key in SCRUBBED_KEYS & set(scrubbed):
            for text in _strings(scrubbed[key]):
                _assert_clean(text)


def test_fuzz_successful_payloads_come_back_equal():
    rng = random.Random(20261010)
    checked = 0
    for _ in range(3000):
        payload = {rng.choice(["message", "reason", "detail", "data", "events"]): _value(rng) for _ in range(rng.randint(1, 3))}
        payload = json.loads(json.dumps(payload, default=str))             # JSON-shaped, as a tool result is
        payload.pop("error", None)
        payload.pop("success", None)
        if _holds_an_error_object(payload):
            continue            # a nested failure is scrubbed by design; this arm is about successful payloads
        assert scrub_tool_result(payload) == payload
        checked += 1
    assert checked > 1000
