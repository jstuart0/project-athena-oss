"""A value interpolated into the path of a call to admin-backend can't leave
its place in the route.

httpx resolves dot segments before it sends (``/by-name/../../x`` goes out as
another route's path), and an unencoded ``?``, ``#`` or ``/`` moves the rest
of the template into the query, drops it, or adds a segment. These calls
carry the service key, and one of the values (a tool-call name) is chosen by
the model.

First half: each site is called with hostile values over a recording
transport, and the path that would be sent is checked against the route's
template. Second half: a source rule, so a new interpolated segment that
isn't passed through ``path_segment`` / ``path_segments`` fails here.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shared import admin_config, admin_url, bypass_cache, llm_router, service_key, service_registry  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402

ADMIN = "http://admin.test"
_REAL_ASYNC_CLIENT = httpx.AsyncClient

# Each is one path segment's worth of trouble.
HOSTILE = {
    "parent": "../secrets",
    "climb": "a/../../b",
    "query": "a?x=1",
    "fragment": "a#frag",
    "encoded_slash": "a%2Fb",
    "space": "a b",
    "backslash": "a\\..\\b",
}
DOT_SEGMENTS = {"dot": ".", "dot_dot": ".."}


@pytest.fixture
def sent(monkeypatch):
    """Every request any ``httpx.AsyncClient`` built during the test would
    send; each is answered 404."""
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(404, json={})

    def client(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("SERVICE_API_KEY", "segment-test-key")
    _clear_cache_for_tests()
    service_key._reset_for_tests()
    yield requests
    _clear_cache_for_tests()
    service_key._reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


def _admin_method(name, *extra):
    async def call(value):
        client = admin_config.AdminConfigClient(admin_url=ADMIN, api_key="segment-test-key")
        try:
            return await getattr(client, name)(value, *extra)
        finally:
            await client.close()
    return call


def _router_method(name, *leading):
    async def call(value):
        router = llm_router.LLMRouter(admin_url=ADMIN)
        return await getattr(router, name)(*leading, value)
    return call


async def _service_url(value):
    service_registry._url_cache.clear()
    return await service_registry.get_service_url(value)


async def _bypass(value):
    bypass_cache._bypass_cache.clear()
    return await bypass_cache.get_bypass_config(value, ADMIN, "segment-test-key")


async def _routing_strategy(value):
    main = importlib.import_module("orchestrator.main")
    main._intent_routing_cache.clear()
    return await main.get_intent_routing_strategy(value)


async def _service_bypass(value):
    main = importlib.import_module("orchestrator.main")
    return await main.check_service_bypass(value)


async def _pricing_provider(value):
    return await llm_router.LLMRouter(admin_url=ADMIN)._get_model_pricing(value, "m1")


# id -> (path before the value, path after it, the call)
SINGLE_SEGMENT_SITES = {
    "admin_config.get_tool_api_key_requirements": (
        "/api/tool-calling/tools/by-name/", "/api-keys/public", _admin_method("get_tool_api_key_requirements")),
    "admin_config.get_escalation_state": (
        "/api/escalation/state/", "/public", _admin_method("get_escalation_state")),
    "admin_config.get_component_model": (
        "/api/component-models/component/", "", _admin_method("get_component_model")),
    "admin_config.get_voice_interface_config": (
        "/api/voice-interfaces/internal/config/", "", _admin_method("get_voice_interface_config")),
    "admin_config.resolve_room_group": (
        "/api/room-groups/resolve/", "", _admin_method("resolve_room_group")),
    "admin_config.get_user_session_by_device": (
        "/api/user-sessions/device/", "", _admin_method("get_user_session_by_device")),
    "admin_config.get_external_api_key": (
        "/api/external-api-keys/public/", "/key", _admin_method("get_external_api_key")),
    "admin_config.get_service_usage": (
        "/api/internal/service-usage/", "", _admin_method("get_service_usage")),
    "llm_router._get_model_pricing.model": (
        "/api/cloud-providers/pricing/openai/", "", _router_method("_get_model_pricing", "openai")),
    "llm_router._get_model_pricing.provider": (
        "/api/cloud-providers/pricing/", "/m1", _pricing_provider),
    "service_registry.get_service_url": (
        "/api/service-registry/services/", "/url", _service_url),
    "bypass_cache.get_bypass_config": (
        "/api/rag-service-bypass/public/", "/config", _bypass),
    "orchestrator.main.get_intent_routing_strategy": (
        "/api/intent-routing/strategy/configs/", "", _routing_strategy),
    "orchestrator.main.check_service_bypass": (
        "/api/rag-service-bypass/public/", "/config", _service_bypass),
}


@pytest.fixture(autouse=True)
def _admin_base(monkeypatch):
    monkeypatch.setattr(service_registry, "ADMIN_API_URL", ADMIN)
    monkeypatch.setattr(importlib.import_module("orchestrator.main"), "ADMIN_API_URL", ADMIN)


def _segment_of(request, before, after):
    """The one segment the value became, or a failure naming what was sent."""
    target = request.url.raw_path.decode()
    path, _, query = target.partition("?")
    assert path.startswith(before) and path.endswith(after), f"left the template: {target}"
    segment = path[len(before):len(path) - len(after)] if after else path[len(before):]
    assert "/" not in segment and segment not in ("", ".", ".."), f"not one segment: {target}"
    return segment, query


@pytest.mark.parametrize("site", sorted(SINGLE_SEGMENT_SITES))
def test_a_plain_value_is_requested_as_written(site, sent):
    """Positive control: these sites do send, and a plain value is unchanged."""
    before, after, call = SINGLE_SEGMENT_SITES[site]
    _run(call("plain_value-1"))

    assert len(sent) == 1
    assert _segment_of(sent[0], before, after) == ("plain_value-1", "")


@pytest.mark.parametrize("kind", sorted(HOSTILE))
@pytest.mark.parametrize("site", sorted(SINGLE_SEGMENT_SITES))
def test_a_hostile_value_stays_one_segment_of_its_template(site, kind, sent):
    before, after, call = SINGLE_SEGMENT_SITES[site]
    _run(call(HOSTILE[kind]))

    assert len(sent) <= 1
    for request in sent:
        segment, query = _segment_of(request, before, after)
        assert query == "", f"the value reached the query: {request.url}"
        assert request.url.fragment == ""


@pytest.mark.parametrize("kind", sorted(DOT_SEGMENTS))
@pytest.mark.parametrize("site", sorted(SINGLE_SEGMENT_SITES))
def test_a_dot_segment_is_never_sent(site, kind, sent):
    """Encoding can't save "." or "..": the client or a proxy resolves them."""
    _before, _after, call = SINGLE_SEGMENT_SITES[site]
    _run(call(DOT_SEGMENTS[kind]))

    assert sent == []


@pytest.mark.parametrize("kind", sorted({**HOSTILE, **DOT_SEGMENTS}))
def test_a_tool_name_outside_the_safe_alphabet_is_never_looked_up(kind, sent):
    """The tool name comes from the model's tool call."""
    value = {**HOSTILE, **DOT_SEGMENTS}[kind]

    assert _run(_admin_method("get_tool_api_key_requirements")(value)) == []
    assert _run(_admin_method("get_api_keys_for_tool")(value)) == {}
    assert sent == []


def test_get_secret_refuses_a_dot_segment_and_encodes_the_rest(sent):
    with pytest.raises(ValueError):
        _run(_admin_method("get_secret")(".."))
    assert sent == []

    assert _run(_admin_method("get_secret")("../x")) is None
    assert [r.url.raw_path for r in sent] == [b"/api/secrets/service/..%2Fx"]


# The {model_name:path} route keeps its slashes ------------------------------

MODEL_PREFIX = "/api/model-configs/public/"


def _model_config(value):
    return _run(_router_method("_get_model_config")(value))


@pytest.mark.parametrize("model, path", [
    ("qwen3:8b", MODEL_PREFIX + "qwen3%3A8b"),
    ("mlx-community/Qwen3-8B", MODEL_PREFIX + "mlx-community/Qwen3-8B"),
    ("/models/local", MODEL_PREFIX + "/models/local"),
    ("a?x=1", MODEL_PREFIX + "a%3Fx%3D1"),
    ("a#frag", MODEL_PREFIX + "a%23frag"),
])
def test_a_model_name_keeps_its_slashes_and_nothing_else(model, path, sent):
    _model_config(model)

    assert [r.url.raw_path.decode() for r in sent] == [path]


@pytest.mark.parametrize("model", ["..", "../secrets", "a/../../b", "a/./b", "org/.."])
def test_a_model_name_with_a_dot_segment_is_never_sent(model, sent):
    config = _model_config(model)

    assert sent == []
    assert config["model_name"] == model and config["ollama_options"] == {}, "the documented default"


# The helpers ----------------------------------------------------------------

def test_path_segment_encodes_everything_reserved():
    assert admin_url.path_segment("plain_value-1.x~") == "plain_value-1.x~"
    assert admin_url.path_segment("a/b?c#d%e f") == "a%2Fb%3Fc%23d%25e%20f"
    assert admin_url.path_segment(42) == "42"


@pytest.mark.parametrize("value", ["", ".", ".."])
def test_path_segment_refuses_an_empty_or_dot_segment(value):
    with pytest.raises(ValueError) as refused:
        admin_url.path_segment(value)
    assert "segment" in str(refused.value)


def test_a_refusal_never_quotes_the_value():
    """Callers log the exception text."""
    with pytest.raises(ValueError) as refused:
        admin_url.path_segments("zz-sentinel/../x")
    assert "zz-sentinel" not in str(refused.value)


# The source rule ------------------------------------------------------------

SCANNER = Path(__file__).with_name("test_admin_guest_route_callers_send_service_key.py")
ENCODERS = {"path_segment", "path_segments"}

# Interpolated path values outside the trees this rule was written for:
# {(file, function, expression): why it is left}. Compared exactly, so a new
# site fails and a fixed one must be removed.
UNENCODED_ELSEWHERE = {
    ("src/rag/sports/main.py", "get_api_key_config", "service_name"):
        "a keyed call whose value is a constant chosen in that module; converting it belongs to the edge callers",
    ("src/sms/scheduler.py", "_get_template", "template_id"):
        "sends no service key; an integer id read from the admin database",
    ("src/sms/tips.py", "get_tips_for_stay", "calendar_event_id"):
        "sends no service key; an integer id read from the admin database",
}


def _scanner():
    spec = importlib.util.spec_from_file_location("_callers_scanner", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _url_expressions(index, call):
    node = call.node
    arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "url"), None)
    if isinstance(arg, ast.Name):
        for func in reversed(call.chain):
            values = index.assigned(func).get(arg.id)
            if values:
                return values
        return []
    return [arg] if arg is not None else []


def _is_encoded(index, call, expression):
    """A direct ``path_segment(...)`` / ``path_segments(...)`` call, or a
    name whose every assignment in the function is one."""
    def encoder_call(node):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ENCODERS

    if encoder_call(expression):
        return True
    if isinstance(expression, ast.Name) and call.chain:
        values = index.assigned(call.chain[-1]).get(expression.id, [])
        return bool(values) and all(encoder_call(v) for v in values)
    return False


def unencoded_path_values(scanner, source, admin_only=True):
    """[(lineno, function, expression)] for every value interpolated into the
    path of an HTTP call's f-string URL (after the base, before any "?")
    that isn't encoded."""
    index = scanner.index_of(source)
    found = []
    for call in index.calls:
        for url in _url_expressions(index, call):
            if not isinstance(url, ast.JoinedStr):
                continue
            if admin_only and not scanner.ADMIN_BASE.search(index.seg(url)):
                continue
            in_path = False
            for part in url.values:
                if isinstance(part, ast.Constant):
                    text = str(part.value)
                    in_path = in_path or "/" in text
                    if "?" in text:
                        break
                elif in_path and not _is_encoded(index, call, part.value):
                    found.append((
                        call.node.lineno,
                        call.chain[-1].name if call.chain else "<module>",
                        index.seg(part.value),
                    ))
    return found


def _tree_findings():
    scanner = _scanner()
    findings, examined = [], 0
    for rel in scanner._source_files():
        text = scanner._read(rel)
        if "/api/" not in text:
            continue
        try:
            scanner.index_of(text)
        except SyntaxError:
            continue  # src/jetson holds a file that isn't valid Python 3
        examined += 1
        findings.extend((rel, name, expr) for _line, name, expr in unencoded_path_values(scanner, text))
    return scanner, findings, examined


@pytest.mark.parametrize("area", ["shared", "orchestrator"])
def test_every_interpolated_admin_path_value_is_encoded(area):
    scanner, findings, examined = _tree_findings()
    assert examined >= 40, examined
    mine = sorted(f for f in findings if scanner.area_of(f[0]) == area)
    assert not mine, f"{len(mine)} path value(s) on an admin call without path_segment(): {mine}"

    # The rule sees the sites it was written for: each uses an encoder.
    for rel, marker in (
        ("src/shared/admin_config.py", "path_segment(tool_name)"),
        ("src/shared/llm_router.py", "path_segments(model)"),
        ("src/shared/service_registry.py", "path_segment(service_name)"),
        ("src/orchestrator/main.py", "path_segment(cache_key)"),
    ):
        assert marker in scanner._read(rel), (rel, marker)


def test_unencoded_values_elsewhere_are_the_known_ones():
    scanner, findings, _examined = _tree_findings()
    elsewhere = {f for f in findings if scanner.area_of(f[0]) == "edge"}
    assert elsewhere == set(UNENCODED_ELSEWHERE), sorted(elsewhere ^ set(UNENCODED_ELSEWHERE))
    for reason in UNENCODED_ELSEWHERE.values():
        assert len(reason.split()) >= 8


PLANTED = '''
async def raw(client, admin_url, name):
    return await client.get(f"{admin_url}/api/things/{name}/detail")

async def encoded(client, admin_url, name):
    return await client.get(f"{admin_url}/api/things/{path_segment(name)}/detail")

async def encoded_through_a_name(client, admin_url, name):
    safe = path_segment(name)
    url = f"{admin_url}/api/things/{safe}"
    return await client.get(url)

async def reassigned(client, admin_url, name):
    safe = path_segment(name)
    safe = name
    return await client.get(f"{admin_url}/api/things/{safe}")

async def quoted_some_other_way(client, admin_url, name):
    return await client.get(f"{admin_url}/api/things/{urllib.parse.quote(name)}")

async def only_in_the_query(client, admin_url, name):
    return await client.get(f"{admin_url}/api/things?name={name}")

async def a_multi_segment_value(client, admin_url, model):
    return await client.get(f"{admin_url}/api/model-configs/public/{path_segments(model)}")

async def another_host(client, ha_url, entity):
    return await client.get(f"{ha_url}/api/states/{entity}")
'''


def test_source_rule_planted_self_test():
    found = {name for _line, name, _expr in unencoded_path_values(_scanner(), PLANTED)}
    assert found == {"raw", "reassigned", "quoted_some_other_way"}
