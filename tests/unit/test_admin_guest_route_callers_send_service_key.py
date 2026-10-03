"""Every service caller of a gated admin-backend route sends
``X-Service-Key`` on that call, verifies TLS while it does, and says so when
admin-backend refuses it (stdlib only).

The scan is function-scoped: for each named caller, every HTTP call whose
URL (the literal, or a same-function variable it was assigned from) names a
gated path must carry a ``headers=`` whose source mentions
``X-Service-Key`` or calls ``service_key_headers()``, or go through a client
constructed with it in the same function. A helper called in the
``headers=`` expression counts when it is defined in the same module (its
source is searched too).

The six scoped voice-automation methods must also send
``X-Athena-Caller-Mode``: admin-backend returns 400 without it.

``AdminConfigClient``'s own default header is ``X-API-Key``, which
admin-backend reads as a *user* API key and rejects for the service key, so
a call that relies on the client defaults does not count.

These tests pin populations and catch drift in the source text. They are not
evidence that a header reaches the wire: ``test_reviewed_route_callers_wire.py``
is.

Areas: ``shared`` is ``src/shared``, ``orchestrator`` is ``src/orchestrator``,
``edge`` is everything else under ``src/`` and ``apps/``. Three tests are
parametrized by area, so ``-k area_<name>`` selects exactly three.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "apps")
POPULATION_TEST = REPO_ROOT / "admin" / "backend" / "tests" / "test_route_auth_population.py"

# Sub-paths, not family prefixes: a family also holds routes a service must
# not call (user-only), legacy routes outside the review, and routes whose
# callers were already conforming.
GATED_PATH = re.compile(
    r"/api/(?:room-groups|user-sessions/device|voice-automations"
    r"|internal/emerging-intents|internal/intent-metrics"
    r"|settings/house-layout|settings/directions-origin-placeholders"
    r"|alerts/public/(?:create|resolve-by-entity)"
    r"|cloud-llm-usage(?![\w/-])"
    r"|cloud-providers/pricing/"
    r"|component-models/(?:public|component/)"
    r"|directions-settings/public"
    r"|escalation/(?:presets/active/public|presets/public|state/|events/internal)"
    r"|features/public"
    r"|follow-me/internal/config"
    r"|gateway-config/public"
    r"|intent-routing/(?:routing/public|providers/public|strategy/configs)"
    r"|llm-backends/(?:public(?!/mlx)|metrics)"
    r"|mcp-security/(?:public|check-domain)"
    r"|model-configs/public"
    r"|music-config/internal"
    r"|presets/public/active"
    r"|room-audio/internal"
    r"|room-tv/(?:internal|apps|features)"
    r"|service-registry/services/[^/\s\"']+/url"
    r"|tool-calling/(?:settings/public|triggers/public|tools/stats/public|tools/[^\s\"']*api-keys/public)"
    r"|tool-proposals(?![\w/-])"
    r"|voice-config/(?:internal/|health)"
    r"|voice-interfaces/(?:engines/public/|internal/config/|public))"
    # The Control Agent's callback URL is built on a base that already ends
    # in /api/model-downloads, so the literal has no /api/ in it.
    r"|/internal/\{download_id\}/progress"
)

# Every family that holds one of the 93 reviewed routes. Wider than
# GATED_PATH on purpose: the route-mapping tests look at every call into
# these families and decide by the route it maps to.
REVIEWED_FAMILY = re.compile(
    r"/api/(?:alerts/public|cloud-llm-usage|cloud-providers|component-models|directions-settings"
    r"|escalation|features/public|follow-me/internal|gateway-config|ha-pipelines|intent-routing"
    r"|llm-backends|mcp-security|model-configs|model-downloads/internal|modules|music-config"
    r"|presets/public|rag-service-bypass|room-audio|room-tv|service-registry/services"
    r"|tool-calling|tool-proposals|voice-config|voice-interfaces)"
    r"|/internal/\{download_id\}/progress"
)
CONTROL_AGENT_URL = re.compile(r"/internal/\{download_id\}/progress")

KEY_MARKER = re.compile(r"X-Service-Key|service_key_headers\(")
ADMIN_BASE = re.compile(
    r"admin_url|ADMIN_API_URL|ADMIN_BACKEND_URL|ADMIN_INTERNAL_URL|internal_url|get_admin_url\(\)"
)

# The callers the guest-data hardening named.
PREDECESSOR_TARGETS = {
    "src/shared/admin_config.py": {
        "resolve_room_group", "get_room_groups", "get_user_session_by_device",
        "create_voice_automation", "get_voice_automations", "archive_voice_automation",
        "restore_voice_automation", "delete_voice_automation",
        "archive_guest_automations", "restore_guest_automations",
    },
    "src/orchestrator/intent_discovery.py": {
        "find_similar_emerging_intent", "create_emerging_intent",
        "increment_intent_count", "record_intent_metric",
    },
    "src/orchestrator/smart_home_controller.py": {"_get_house_layout"},
    "src/orchestrator/main.py": {"get_origin_placeholder_patterns"},
}

# The callers of the 93 reviewed routes.
REVIEWED_TARGETS = {
    "src/shared/admin_config.py": {
        "get_intent_routing", "get_provider_routing", "get_llm_backends", "get_feature_flags",
        "get_tool_api_key_requirements", "get_tool_calling_settings", "get_fallback_triggers",
        "get_active_escalation_preset", "get_escalation_state", "update_escalation_state",
        "get_component_model", "get_all_component_models", "get_gateway_config",
        "get_voice_config_stt", "get_voice_config_tts", "get_voice_config_all",
        "get_voice_interface_config", "check_voice_services_health",
    },
    "src/shared/llm_router.py": {
        "_get_backend_config", "_get_model_config", "_get_model_pricing", "_track_cloud_usage",
        "_persist_metric",
    },
    "src/shared/service_registry.py": {"get_service_url"},
    "src/shared/tool_registry.py": {"_get_mcp_security"},
    "src/shared/voice_config.py": {"_load_engines"},
    "src/orchestrator/main.py": {
        "get_feature_flag", "get_intent_routing_strategy", "_do_prewarm", "lifespan",
        "log_escalation_audit", "list_models",
    },
    "src/orchestrator/helpers.py": {
        "get_feature_config", "get_automation_system_mode", "get_weather_provider_mode",
    },
    "src/orchestrator/self_building_tools.py": {"check_enabled", "_save_proposal"},
    "src/orchestrator/smart_home_controller.py": {
        "_create_stuck_sensor_alert", "_resolve_stuck_sensor_alert",
    },
    "src/orchestrator/tv_handler.py": {"get_tv_configs", "get_app_configs", "get_feature_flag"},
    "src/orchestrator/music_handler.py": {"get_room_configs"},
    "src/gateway/main.py": {"get_feature_flag", "_log_metric_to_db", "list_models"},
    "src/gateway/livekit_service.py": {"_refresh_feature_flags"},
    "src/gateway/wyoming_bridge.py": {"_refresh_feature_flags"},
    "apps/jarvis-web/backend/main.py": {"get_persistent_sessions_config", "get_room_tv_configs"},
    "src/rag/directions/main.py": {"lifespan"},
    "src/control_agent/huggingface.py": {"send_progress_callback"},
}

TARGETS = {
    rel: PREDECESSOR_TARGETS.get(rel, set()) | REVIEWED_TARGETS.get(rel, set())
    for rel in sorted(set(PREDECESSOR_TARGETS) | set(REVIEWED_TARGETS))
}

SCOPED = {
    "create_voice_automation", "get_voice_automations", "archive_voice_automation",
    "restore_voice_automation", "archive_guest_automations", "restore_guest_automations",
}

# Calls into a reviewed family that map to no reviewed route. Each already
# sends the key to a route that was gated before the review, except the last.
MAPS_TO_NO_REVIEWED_ROUTE = {
    ("src/shared/admin_config.py", "get_enabled_tools"),
    ("src/shared/admin_config.py", "record_tool_metric"),
    ("src/shared/bypass_cache.py", "get_bypass_config"),
    ("src/orchestrator/main.py", "check_service_bypass"),
    ("src/shared/service_registry.py", "register_service"),
    ("src/shared/service_registry.py", "unregister_service"),
    ("src/control_agent/main.py", "_upsert_all_services"),
    # GET /api/intent-routing/patterns: a legacy route outside the 93, whose
    # caller sends no key. Not touched by the review.
    ("src/shared/admin_config.py", "get_intent_patterns"),
}

# The one caller of a user-only route. It can't run: it reaches for
# `admin_client._client`, which AdminConfigClient doesn't have. Repairing it
# means pointing it at the service-only provider-config route, not sending
# the key to a user-only one.
DEAD_USER_ROUTE_CALLER = ("src/orchestrator/main.py", "_get_preferred_cloud_model")

AREAS = ("shared", "orchestrator", "edge")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request", "stream"}
_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def area_of(rel: str) -> str:
    if rel.startswith("src/shared/"):
        return "shared"
    if rel.startswith("src/orchestrator/"):
        return "orchestrator"
    return "edge"


def is_keyed(text: str) -> bool:
    return bool(KEY_MARKER.search(text))


# ---------------------------------------------------------------------------
# One parsed index per source text, built once.
# ---------------------------------------------------------------------------

class Call:
    """One HTTP call: its node, the functions it sits in (outermost first)
    and the class it sits in, if any."""

    __slots__ = ("node", "chain", "cls")

    def __init__(self, node, chain, cls):
        self.node, self.chain, self.cls = node, chain, cls

    @property
    def innermost(self):
        return self.chain[-1] if self.chain else None


class Index:
    def __init__(self, source: str):
        self.lines = source.split("\n")
        self.tree = ast.parse(source)
        self.functions = {}       # name -> first definition (breadth-first, like ast.walk)
        self.calls = []           # every HTTP call
        self.note_calls = []      # (node, chain) for every note_admin_refusal(...) call
        self._assigned = {}
        for node in ast.walk(self.tree):
            if isinstance(node, _FUNCTION):
                self.functions.setdefault(node.name, node)
        self._visit(self.tree, (), None)

    def _visit(self, node, chain, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTION):
                self._visit(child, chain + (child,), cls)
                continue
            if isinstance(child, ast.ClassDef):
                self._visit(child, chain, child)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr in _HTTP_METHODS:
                    self.calls.append(Call(child, chain, cls))
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "note_admin_refusal":
                    self.note_calls.append((child, chain))
            self._visit(child, chain, cls)

    def seg(self, node) -> str:
        """The node's source, by slicing the pre-split lines (column offsets
        are UTF-8 byte offsets)."""
        first, last = node.lineno - 1, node.end_lineno - 1
        if first == last:
            return self.lines[first].encode()[node.col_offset:node.end_col_offset].decode()
        parts = [self.lines[first].encode()[node.col_offset:].decode()]
        parts.extend(self.lines[first + 1:last])
        parts.append(self.lines[last].encode()[:node.end_col_offset].decode())
        return "\n".join(parts)

    def assigned(self, func):
        """name -> [value node of every plain assignment to it in func]."""
        cached = self._assigned.get(func)
        if cached is None:
            cached = {}
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            cached.setdefault(target.id, []).append(node.value)
            self._assigned[func] = cached
        return cached

    def url_text(self, call: Call) -> str:
        node = call.node
        arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "url"), None)
        if arg is None:
            return ""
        if isinstance(arg, ast.Name):
            for func in reversed(call.chain):
                values = self.assigned(func).get(arg.id)
                if values:
                    return " ".join(self.seg(v) for v in values)
            return ""
        return self.seg(arg)

    def expand_helpers(self, expr) -> str:
        """The expression's source plus the source of any same-module
        function it calls (by bare name or self.<name>)."""
        text = self.seg(expr)
        for node in ast.walk(expr):
            if isinstance(node, ast.Call):
                callee = node.func
                name = callee.id if isinstance(callee, ast.Name) else (
                    callee.attr if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
                    and callee.value.id == "self" else None)
                if name and name in self.functions:
                    text += "\n" + self.seg(self.functions[name])
        return text

    def _bound_values(self, scope, receiver):
        """Value nodes bound to the bare name `receiver` in `scope`:
        `x = <value>` and `with <value> as x`."""
        for node in ast.walk(scope):
            pairs = []
            if isinstance(node, ast.Assign):
                pairs = [(t, node.value) for t in node.targets]
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                pairs = [(item.optional_vars, item.context_expr) for item in node.items]
            for target, value in pairs:
                if isinstance(target, ast.Name) and target.id == receiver:
                    yield value

    def client_headers_text(self, func, receiver) -> str:
        texts = []
        for value in self._bound_values(func, receiver):
            if isinstance(value, ast.Call):
                for kw in value.keywords:
                    if kw.arg == "headers":
                        texts.append(self.expand_helpers(kw.value))
        return "\n".join(texts)

    def header_text(self, call: Call) -> str:
        node, func = call.node, call.innermost
        header = next((k.value for k in node.keywords if k.arg == "headers"), None)
        text = self.expand_helpers(header) if header is not None else ""
        if func is None:
            return text
        if isinstance(header, ast.Name):
            text += "\n" + "\n".join(self.expand_helpers(v) for v in self.assigned(func).get(header.id, []))
        receiver = node.func.value
        if isinstance(receiver, ast.Name):
            text += "\n" + self.client_headers_text(func, receiver.id)
        return text

    def function_text(self, func) -> str:
        """The function's source plus every same-module function it calls."""
        return self.expand_helpers(func)

    def client_values(self, call: Call):
        """The expressions that build the client this call goes through: a
        constructor in the call's own receiver, a local or `with ... as`
        binding in an enclosing function, a module global of that name
        (wherever it is assigned), or `self.<attr>` assigned in the class."""
        receiver = call.node.func.value
        if isinstance(receiver, ast.Call):
            return [receiver]
        if isinstance(receiver, ast.Name):
            for func in reversed(call.chain):
                values = list(self._bound_values(func, receiver.id))
                if values:
                    return values
            return list(self._bound_values(self.tree, receiver.id))
        if (isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name)
                and receiver.value.id == "self" and call.cls is not None):
            found = []
            for node in ast.walk(call.cls):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (isinstance(target, ast.Attribute) and target.attr == receiver.attr
                                and isinstance(target.value, ast.Name) and target.value.id == "self"):
                            found.append(node.value)
            return found
        return []


@functools.lru_cache(maxsize=None)
def index_of(source: str) -> Index:
    return Index(source)


@functools.lru_cache(maxsize=None)
def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


@functools.lru_cache(maxsize=None)
def _source_files():
    """Every *.py under src/ and apps/, tests and vendored trees excluded."""
    out = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = path.relative_to(REPO_ROOT).parts
            if "tests" in parts or "node_modules" in parts or "__pycache__" in parts:
                continue
            out.append("/".join(parts))
    return tuple(out)


def _files_matching(pattern):
    """Parsed indexes for the files whose text matches; the regex runs
    before any parse."""
    return [(rel, index_of(_read(rel))) for rel in _source_files() if pattern.search(_read(rel))]


def _in_function(call: Call, func) -> bool:
    return func in call.chain


def scan(source: str, names: set):
    """{function: [(lineno, header_text)]} for every gated HTTP call in the
    named functions (a nested function's calls count for the outer one too)."""
    index = index_of(source)
    results = {}
    for name in names:
        func = index.functions.get(name)
        if func is None:
            continue
        results[name] = [
            (call.node.lineno, index.header_text(call))
            for call in index.calls
            if _in_function(call, func) and GATED_PATH.search(index.url_text(call))
        ]
    return results


@functools.lru_cache(maxsize=None)
def _all_results():
    out = {}
    for rel, names in TARGETS.items():
        for name, calls in scan(_read(rel), names).items():
            out[(rel, name)] = calls
    return out


def _reviewed_keys():
    return {(rel, name) for rel, names in REVIEWED_TARGETS.items() for name in names}


def _predecessor_keys():
    return {(rel, name) for rel, names in PREDECESSOR_TARGETS.items() for name in names}


# C1 -----------------------------------------------------------------------

def test_population_is_the_sixty_eight_callers():
    results = _all_results()
    every = {(rel, name) for rel, names in TARGETS.items() for name in names}
    assert len(results) == 68, sorted(every - set(results))
    assert len(TARGETS) == 18
    assert not _reviewed_keys() & _predecessor_keys()
    empty = sorted(k for k, calls in results.items() if not calls)
    assert not empty, f"named callers with no gated call found: {empty}"
    names = {n for _r, n in results}
    for named in ("get_user_session_by_device", "create_voice_automation", "record_intent_metric"):
        assert named in names
    for named in (
        ("src/gateway/main.py", "get_feature_flag"),
        ("src/shared/llm_router.py", "_get_backend_config"),
        ("src/control_agent/huggingface.py", "send_progress_callback"),
        ("src/orchestrator/main.py", "_do_prewarm"),
    ):
        assert named in results
    assert DEAD_USER_ROUTE_CALLER not in results

    def count(keys):
        return {area: sum(1 for rel, _n in keys if area_of(rel) == area) for area in AREAS}

    assert count(_reviewed_keys()) == {"shared": 26, "orchestrator": 17, "edge": 9}
    assert count(_predecessor_keys()) == {"shared": 10, "orchestrator": 6, "edge": 0}


# C2 -----------------------------------------------------------------------

@pytest.mark.parametrize("area", AREAS, ids=lambda a: f"area_{a}")
def test_every_gated_call_sends_the_service_key(area):
    missing = sorted(
        f"{rel}:{lineno} {name}"
        for (rel, name), calls in _all_results().items()
        if area_of(rel) == area
        for lineno, text in calls
        if not is_keyed(text)
    )
    assert not missing, f"{len(missing)} gated call(s) without X-Service-Key: {missing}"


# C3 / C4 ------------------------------------------------------------------

def closed_world(sources: dict, targets: dict):
    """(matched, outside): every HTTP call whose URL names a gated path, and
    the ones that sit in no named caller of their file."""
    matched, outside = [], []
    for rel, source in sources.items():
        index = index_of(source)
        named = targets.get(rel, set())
        for call in index.calls:
            if not GATED_PATH.search(index.url_text(call)):
                continue
            functions = [f.name for f in call.chain]
            owners = [name for name in functions if name in named]
            entry = (rel, functions[-1] if functions else "<module>", call.node.lineno)
            matched.append((entry, owners))
            if not owners:
                outside.append(entry)
    return matched, outside


def test_no_gated_call_outside_the_named_callers():
    sources = {rel: _read(rel) for rel in _source_files() if GATED_PATH.search(_read(rel))}
    matched, outside = closed_world(sources, TARGETS)
    assert not outside, f"{len(outside)} gated call(s) in a function that isn't a named caller: {sorted(outside)}"
    owners = {(entry[0], name) for entry, names in matched for name in names}
    files = {entry[0] for entry, _ in matched}
    assert len(matched) >= 70, len(matched)
    assert len(owners) >= 68, len(owners)
    assert len(files) >= 18, len(files)
    assert ("src/gateway/main.py", "get_feature_flag") in owners


CLOSED_WORLD_PLANTED = '''
async def listed(client, u):
    return await client.get(f"{u}/api/features/public", headers={"X-Service-Key": "k"})

async def unlisted(client, u):
    return await client.get(f"{u}/api/llm-backends/public")

async def elsewhere(client, u):
    return await client.get(f"{u}/health")
'''


def test_closed_world_self_test():
    matched, outside = closed_world({"planted.py": CLOSED_WORLD_PLANTED}, {"planted.py": {"listed"}})
    assert [entry[1] for entry, _ in matched] == ["listed", "unlisted"]
    assert [entry[1] for entry in outside] == ["unlisted"]


# C5 -----------------------------------------------------------------------

def test_scoped_voice_calls_send_the_caller_mode():
    missing = sorted(
        f"{rel}:{lineno} {name}"
        for (rel, name), calls in _all_results().items()
        if name in SCOPED
        for lineno, text in calls
        if "X-Athena-Caller-Mode" not in text or "X-Athena-Guest-Stay" not in text
    )
    assert not missing, f"{len(missing)} scoped call(s) without X-Athena-Caller-Mode: {missing}"


PLANTED = '''
async def unheadered(client, u):
    return await client.get(f"{u}/api/room-groups")

async def headered(client, u):
    return await client.get(f"{u}/api/room-groups", headers={"X-Service-Key": "k"})

async def via_client(u):
    async with httpx.AsyncClient(headers={"X-Service-Key": "k"}) as client:
        return await client.get(f"{u}/api/settings/house-layout")

async def via_variable(self, u):
    url = f"{u}/api/voice-automations"
    headers = self._voice_headers("owner", None)
    return await self.client.get(url, headers=headers)

def _voice_headers(self, mode, name):
    return {"X-Service-Key": self.api_key, "X-Athena-Caller-Mode": mode, "X-Athena-Guest-Stay": "1"}

async def ungated(client, u):
    return await client.get(f"{u}/health")

async def via_shared_helper(client, u):
    return await client.get(f"{u}/api/features/public", headers=service_key_headers())

async def merged_helper(client, u):
    return await client.get(f"{u}/api/features/public", headers={**service_key_headers(), "X-Other": "1"})

async def other_headers(client, u):
    return await client.get(f"{u}/api/features/public", headers={"Accept": "application/json"})

async def two_calls(client, u):
    url = f"{u}/api/llm-backends/metrics"
    first = await client.post(url, json={}, headers={"X-Service-Key": "k"})
    return await client.post(url, json={})
'''


def test_planted_self_test():
    names = {
        "unheadered", "headered", "via_client", "via_variable", "ungated",
        "via_shared_helper", "merged_helper", "other_headers", "two_calls",
    }
    results = scan(PLANTED, names)
    assert set(results) == names

    def keyed(name):
        return [is_keyed(text) for _l, text in results[name]]

    assert keyed("unheadered") == [False]
    assert keyed("headered") == [True]
    assert keyed("via_client") == [True]
    assert [("X-Athena-Caller-Mode" in t) for _l, t in results["via_variable"]] == [True]
    assert results["ungated"] == []
    assert keyed("via_shared_helper") == [True]
    assert keyed("merged_helper") == [True]
    assert keyed("other_headers") == [False]
    assert keyed("two_calls") == [True, False]


# C6 / C10: which reviewed route a call lands on ---------------------------

@functools.lru_cache(maxsize=None)
def reviewed_routes():
    """{(method, template): (kind, permission)}, read from the route test's
    literal without importing it (it imports the app)."""
    tree = ast.parse(POPULATION_TEST.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REVIEWED_BY_FILE" for t in node.targets
        ):
            by_file = ast.literal_eval(node.value)
            return {op: pin for ops in by_file.values() for op, pin in ops.items()}
    raise AssertionError("REVIEWED_BY_FILE not found as a literal")


_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_URL_TAIL = re.compile(r"(/api/[^\s\"'`?]*|/internal/\{download_id\}/progress)")


def normalise_call(url_text: str):
    """The path a call's URL text names, placeholders reduced to `{}`: cut
    to `/api/...`, query string dropped. None when the text has no path."""
    found = _URL_TAIL.findall(url_text)
    if not found:
        return None
    path = found[-1]
    if CONTROL_AGENT_URL.fullmatch(path):
        path = "/api/model-downloads" + path
    return _PLACEHOLDER.sub("{}", path)


def _template_regex(template: str):
    parts = [re.escape(part) for part in _PLACEHOLDER.split(template)]
    return re.compile("[^/]+".join(parts) + r"/?")


def map_to_routes(method: str, url_text: str, routes) -> list:
    """The reviewed operations this call lands on. An exact template match
    wins over a parameter match (`/api/modules/enabled` is its own route,
    not `/api/modules/{module_id}`)."""
    path = normalise_call(url_text)
    if path is None:
        return []
    same_method = [op for op in routes if op[0] == method.upper()]
    exact = [op for op in same_method if _PLACEHOLDER.sub("{}", op[1]).rstrip("/") == path.rstrip("/")]
    if exact:
        return exact
    return [op for op in same_method if _template_regex(op[1]).fullmatch(path)]


@functools.lru_cache(maxsize=None)
def _family_calls():
    """[(rel, function, lineno, method, mapped operations)] for every HTTP
    call into a reviewed family."""
    routes = reviewed_routes()
    out = []
    for rel, index in _files_matching(REVIEWED_FAMILY):
        for call in index.calls:
            text = index.url_text(call)
            if not REVIEWED_FAMILY.search(text):
                continue
            name = call.innermost.name if call.innermost else "<module>"
            method = call.node.func.attr
            out.append((rel, name, call.node.lineno, method, tuple(map_to_routes(method, text, routes))))
    return tuple(out)


def test_each_scanned_call_maps_to_a_service_reachable_route():
    routes = reviewed_routes()
    assert len(routes) == 93
    problems, mapped = [], {}
    for rel, name, lineno, method, ops in _family_calls():
        if (rel, name) == DEAD_USER_ROUTE_CALLER:
            continue  # owned by the user-only test below
        where = f"{rel}:{lineno} {name}"
        if not ops:
            if (rel, name) not in MAPS_TO_NO_REVIEWED_ROUTE:
                problems.append(f"{where}: {method.upper()} maps to no reviewed route")
            continue
        if len(ops) != 1:
            problems.append(f"{where}: maps to {len(ops)} routes {ops}")
            continue
        kind = routes[ops[0]][0]
        if kind not in ("service_or_user", "service"):
            problems.append(f"{where}: calls the {kind}-only route {ops[0]}")
            continue
        mapped[(rel, name, lineno)] = ops[0]
    assert not problems, f"{len(problems)} call(s) a service can't make: {problems}"
    # 54 call sites: the 55 per-function entries count lifespan's nested
    # _do_prewarm call under both functions.
    assert len(mapped) >= 54, len(mapped)
    proposal = [op for (rel, name, _l), op in mapped.items() if name == "_save_proposal"]
    assert proposal == [("POST", "/api/tool-proposals")]
    progress = [op for (rel, name, _l), op in mapped.items() if name == "send_progress_callback"]
    assert progress == [("POST", "/api/model-downloads/internal/{download_id}/progress")]
    unlisted = {(rel, name) for rel, name, _l in mapped} - _reviewed_keys()
    assert not unlisted, f"mapped caller(s) that aren't named targets: {sorted(unlisted)}"


def test_route_mapping_self_test():
    routes = reviewed_routes()
    assert map_to_routes("post", 'f"{u}/api/tool-proposals"', routes) == [("POST", "/api/tool-proposals")]
    # The same path with GET is the user-only list.
    assert map_to_routes("get", 'f"{u}/api/tool-proposals"', routes) == [("GET", "/api/tool-proposals")]
    assert routes[("GET", "/api/tool-proposals")][0] == "user"
    assert map_to_routes("get", 'f"{u}/api/features/public?enabled_only=false"', routes) == [
        ("GET", "/api/features/public")]
    assert map_to_routes("get", 'f"{u}/api/escalation/state/{session_id}/public"', routes) == [
        ("GET", "/api/escalation/state/{session_id}/public")]
    assert map_to_routes("get", 'f"{u}/api/model-configs/public/{model}"', routes) == [
        ("GET", "/api/model-configs/public/{model_name:path}")]
    assert map_to_routes("get", 'f"{u}/api/modules/enabled"', routes) == [("GET", "/api/modules/enabled")]
    assert map_to_routes("get", 'f"{u}/api/modules/weather"', routes) == [("GET", "/api/modules/{module_id}")]
    assert map_to_routes("post", 'f"{callback_url}/internal/{download_id}/progress"', routes) == [
        ("POST", "/api/model-downloads/internal/{download_id}/progress")]
    assert map_to_routes("get", 'f"{u}/api/intent-routing/patterns"', routes) == []
    assert map_to_routes("get", 'f"{u}/health"', routes) == []


def test_the_only_caller_of_a_user_only_route_is_the_dead_one():
    routes = reviewed_routes()
    user_only = {op for op, (kind, _p) in routes.items() if kind == "user"}
    assert len(user_only) == 42
    callers = {}
    for rel, name, lineno, _method, ops in _family_calls():
        if any(op in user_only for op in ops):
            callers.setdefault((rel, name), []).append(lineno)
    assert set(callers) == {DEAD_USER_ROUTE_CALLER}, callers
    assert len(callers[DEAD_USER_ROUTE_CALLER]) == 2
    # Still dead: the attribute it reaches for doesn't exist.
    assert "self._client" not in _read("src/shared/admin_config.py")


# C8 / C8p -----------------------------------------------------------------

def _unsafe_keywords(index: Index, call: Call):
    """`verify=False` / `follow_redirects=True` on the call itself or on any
    constructor the client it goes through was built with."""
    found = []
    nodes = [call.node]
    for value in index.client_values(call):
        nodes.extend(n for n in ast.walk(value) if isinstance(n, ast.Call))
    for node in nodes:
        for kw in node.keywords:
            if not isinstance(kw.value, ast.Constant):
                continue
            if (kw.arg, kw.value.value) in {("verify", False), ("follow_redirects", True)}:
                found.append((node.lineno, f"{kw.arg}={kw.value.value}"))
    return found


def keyed_admin_callers(sources: dict, targets: dict):
    """{(rel, function): [(lineno, problem)]} for every function that sends
    the service key to an admin URL, and every named caller.

    A named caller is examined on its gated calls. Any other function is
    examined when its source (with the same-module helpers it calls) carries
    the key and it makes an HTTP call whose URL is built on an admin base;
    each such call is checked through the client it goes through."""
    examined = {}
    for rel, source in sources.items():
        index = index_of(source)
        named = {index.functions[name]: name for name in targets.get(rel, set()) if name in index.functions}
        keyed_text = {}
        for call in index.calls:
            text = index.url_text(call)
            owners = []
            if GATED_PATH.search(text):
                owners.extend(func for func in call.chain if func in named)
            if ADMIN_BASE.search(text):
                for func in call.chain:
                    if func not in keyed_text:
                        keyed_text[func] = is_keyed(index.function_text(func))
                    if keyed_text[func] and func not in owners:
                        owners.append(func)
            if not owners:
                continue
            problems = _unsafe_keywords(index, call)
            for func in owners:
                examined.setdefault((rel, func.name), [])
                for lineno, what in problems:
                    if (lineno, what) not in examined[(rel, func.name)]:
                        examined[(rel, func.name)].append((lineno, what))
    return examined


@functools.lru_cache(maxsize=None)
def _tls_examined():
    wanted = re.compile(f"{KEY_MARKER.pattern}|{GATED_PATH.pattern}")
    sources = {rel: _read(rel) for rel in _source_files() if wanted.search(_read(rel))}
    return keyed_admin_callers(sources, TARGETS)


@pytest.mark.parametrize("area", AREAS, ids=lambda a: f"area_{a}")
def test_keyed_callers_verify_tls_and_do_not_follow_redirects(area):
    examined = _tls_examined()
    assert len(examined) >= 90, len(examined)
    assert len({rel for rel, _n in examined}) >= 25
    for named in (
        ("src/orchestrator/tv_handler.py", "get_tv_configs"),
        ("src/gateway/livekit_service.py", "fetch_livekit_credentials"),
        ("src/rag/site_scraper/main.py", "load_config"),
        ("src/mode_service/main.py", "_verify_owner_pin"),
    ):
        assert named in examined, named
    every_target = {(rel, name) for rel, names in TARGETS.items() for name in names}
    assert every_target <= set(examined), sorted(every_target - set(examined))
    unsafe = sorted(
        f"{rel}:{lineno} {name} {what}"
        for (rel, name), problems in examined.items()
        if area_of(rel) == area
        for lineno, what in problems
    )
    assert not unsafe, (
        f"{len(unsafe)} client(s) that carry the service key to admin-backend without "
        f"verifying TLS, or follow redirects: {unsafe}"
    )


TLS_PLANTED = '''
admin_client = None
ha_client = None
orchestrator_client = None

async def case_1_local(u):
    admin_url = get_admin_url()
    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
        return await client.get(f"{admin_url}/api/room-tv/internal", headers={"X-Service-Key": "k"})

def startup():
    global admin_client
    admin_client = httpx.AsyncClient(timeout=5.0, verify=False)

async def case_2_global():
    return await admin_client.get(f"{ADMIN_API_URL}/api/room-audio/internal", headers={"X-Service-Key": "k"})

async def case_3_lifespan_shape():
    global ha_client, orchestrator_client
    ha_client = httpx.AsyncClient(timeout=3.0, verify=False)
    await ha_client.get(f"{HA_URL}/api/states")
    orchestrator_client = httpx.AsyncClient(base_url=orchestrator_url, headers={"X-Service-Key": "k"})
    await orchestrator_client.get("/health")
    async with httpx.AsyncClient(timeout=5.0) as verified:
        return await verified.get(f"{ADMIN_API_URL}/api/site-scraper/config/public", headers={"X-Service-Key": "k"})

async def case_4_not_a_target():
    async with httpx.AsyncClient(verify=False) as client:
        return await client.get(f"{ADMIN_API_URL}/api/external-api-keys/public/x", headers=service_key_headers())

async def case_5_verify_true():
    async with httpx.AsyncClient(verify=True) as client:
        return await client.get(f"{ADMIN_API_URL}/api/site-scraper/config/public", headers={"X-Service-Key": "k"})

async def case_6_redirects():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        return await client.get(f"{ADMIN_API_URL}/api/site-scraper/config/public", headers={"X-Service-Key": "k"})

async def case_7_unkeyed_admin_call():
    async with httpx.AsyncClient(verify=False) as client:
        return await client.get(f"{ADMIN_API_URL}/api/somewhere/else")

async def case_8_keyed_but_not_admin():
    async with httpx.AsyncClient(verify=False) as client:
        return await client.get(f"{ORCHESTRATOR_URL}/query", headers={"X-Service-Key": "k"})
'''


def test_tls_rule_planted_self_test():
    examined = keyed_admin_callers({"planted.py": TLS_PLANTED}, {"planted.py": {"case_1_local"}})
    flagged = sorted(name for (_rel, name), problems in examined.items() if problems)
    assert flagged == ["case_1_local", "case_2_global", "case_4_not_a_target", "case_6_redirects"]
    assert sorted(name for _rel, name in examined) == [
        "case_1_local", "case_2_global", "case_3_lifespan_shape", "case_4_not_a_target",
        "case_5_verify_true", "case_6_redirects",
    ]
    assert [what for _l, what in examined[("planted.py", "case_6_redirects")]] == ["follow_redirects=True"]


# C9 -----------------------------------------------------------------------

CONTROL_AGENT_CALLER = ("src/control_agent/huggingface.py", "send_progress_callback")


def _own_body_nodes(func):
    """Every node of the function except those inside a nested function or
    lambda: a note in a nested function is that function's, not this one's."""
    pending = list(ast.iter_child_nodes(func))
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, _FUNCTION + (ast.Lambda,)):
            continue
        pending.extend(ast.iter_child_nodes(node))


def notes_a_refusal(func, literal_only: bool) -> bool:
    for node in _own_body_nodes(func):
        if literal_only:
            if isinstance(node, ast.Constant) and node.value == "admin_backend_refused":
                return True
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if name == "note_admin_refusal":
                return True
    return False


def _without_a_note(keys):
    missing = []
    for rel, name in sorted(keys):
        func = index_of(_read(rel)).functions.get(name)
        # The Control Agent host gets no shared module: its note is inline.
        if func is None or not notes_a_refusal(func, literal_only=(rel, name) == CONTROL_AGENT_CALLER):
            missing.append(f"{rel} {name}")
    return missing


@pytest.mark.parametrize("area", AREAS, ids=lambda a: f"area_{a}")
def test_new_callers_note_a_refusal(area):
    keys = _reviewed_keys()
    assert len(keys) == 52
    for named in (
        ("src/orchestrator/helpers.py", "get_feature_config"),
        ("src/orchestrator/main.py", "lifespan"),
        ("src/orchestrator/main.py", "_do_prewarm"),
        CONTROL_AGENT_CALLER,
    ):
        assert named in keys
    missing = _without_a_note({key for key in keys if area_of(key[0]) == area})
    assert not missing, f"{len(missing)} caller(s) that don't note a refusal: {missing}"


NOTE_PLANTED = '''
async def outer_without_its_own(client, u):
    async def inner_with_note():
        resp = await client.get(f"{u}/api/component-models/public")
        note_admin_refusal(resp.status_code, "/api/component-models/public")
    resp = await client.get(f"{u}/api/follow-me/internal/config")
    return resp

async def outer_with_its_own(client, u):
    resp = await client.get(f"{u}/api/follow-me/internal/config")
    if resp.status_code != 200:
        service_key.note_admin_refusal(resp.status_code, "/api/follow-me/internal/config")

async def inline_literal(client, u):
    resp = await client.post(f"{u}/internal/{download_id}/progress")
    logger.error("admin_backend_refused", status=resp.status_code, route="/api/x")

async def mentions_it_in_a_comment(client, u):
    # note_admin_refusal(...) goes here one day
    return await client.get(f"{u}/api/features/public")
'''


def test_refusal_note_planted_self_test():
    functions = index_of(NOTE_PLANTED).functions
    assert notes_a_refusal(functions["outer_without_its_own"], literal_only=False) is False
    assert notes_a_refusal(functions["inner_with_note"], literal_only=False) is True
    assert notes_a_refusal(functions["outer_with_its_own"], literal_only=False) is True
    assert notes_a_refusal(functions["inline_literal"], literal_only=True) is True
    assert notes_a_refusal(functions["inline_literal"], literal_only=False) is False
    assert notes_a_refusal(functions["mentions_it_in_a_comment"], literal_only=False) is False


# C11 ----------------------------------------------------------------------

def refusal_note_problems(source: str):
    """(calls, problems) for every note_admin_refusal(...) call: the route
    argument must be a string literal that starts with /api/ and has no
    query string. Anything computed can carry an id or a query into the log."""
    index = index_of(source)
    problems = []
    for node, chain in index.note_calls:
        route = node.args[1] if len(node.args) > 1 else next(
            (k.value for k in node.keywords if k.arg == "route"), None)
        where = f"{node.lineno} {chain[-1].name if chain else '<module>'}"
        if not (isinstance(route, ast.Constant) and isinstance(route.value, str)):
            problems.append(f"{where}: route is not a string literal")
        elif not route.value.startswith("/api/") or "?" in route.value:
            problems.append(f"{where}: route {route.value!r} is not an /api/ template")
    return index.note_calls, problems


def test_refusal_notes_pass_a_static_route_template():
    marker = re.compile(r"note_admin_refusal\(")
    calls, files, problems, owners = 0, set(), [], set()
    for rel in _source_files():
        text = _read(rel)
        if not marker.search(text):
            continue
        found, bad = refusal_note_problems(text)
        if found:
            files.add(rel)
        calls += len(found)
        owners.update((rel, chain[-1].name) for _node, chain in found if chain)
        problems.extend(f"{rel}:{p}" for p in bad)
    assert not problems, f"{len(problems)} refusal note(s) with a computed route: {problems}"
    assert calls >= 51, f"{calls} note_admin_refusal call(s) found; the 51 new callers each have one"
    assert len(files) >= 15, len(files)
    assert ("src/orchestrator/helpers.py", "get_feature_config") in owners


ROUTE_ARGUMENT_PLANTED = '''
def literal(resp):
    note_admin_refusal(resp.status_code, "/api/escalation/state/{session_id}/public")

def keyword_literal(resp):
    note_admin_refusal(resp.status_code, route="/api/features/public")

def f_string(resp, session_id):
    note_admin_refusal(resp.status_code, f"/api/escalation/state/{session_id}/public")

def variable(resp, url):
    note_admin_refusal(resp.status_code, url)

def attribute(resp):
    note_admin_refusal(resp.status_code, resp.url.path)

def concatenation(resp, name):
    note_admin_refusal(resp.status_code, "/api/modules/" + name)

def stringified_url(resp):
    note_admin_refusal(resp.status_code, str(resp.url))

def with_a_query(resp):
    note_admin_refusal(resp.status_code, "/api/features/public?enabled_only=false")

def not_an_api_path(resp):
    note_admin_refusal(resp.status_code, "features")

def missing(resp):
    note_admin_refusal(resp.status_code)
'''


def test_route_argument_planted_self_test():
    calls, problems = refusal_note_problems(ROUTE_ARGUMENT_PLANTED)
    assert len(calls) == 10
    flagged = sorted(p.split(" ", 1)[1].split(":")[0] for p in problems)
    assert flagged == [
        "attribute", "concatenation", "f_string", "missing", "not_an_api_path",
        "stringified_url", "variable", "with_a_query",
    ]
