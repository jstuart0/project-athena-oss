"""ATHENA-89 Phase 3b — AU4: every in-repo caller of an orchestrator route
that requires `X-Service-Key` (D10/DC12) actually sends it.

Static AST scan, no mocking, no git dependency (walks the tree directly
rather than shelling out to `git ls-files`, per tessa's P3 FIX).

r2 (tessa's P3b/P4 FIX): the original implementation checked "does
X-Service-Key appear anywhere in this FILE" per matched literal. Tessa
mutation-tested it by planting an unauthenticated
`httpx.AsyncClient().post(f"{ORCHESTRATOR_URL}/query", json=...)` next to
a real, correctly-headered call in src/gateway/main.py -- the file-level
check still passed, because *some* call elsewhere in the same file did send
the header. That's a false green: the planted call itself sends nothing.

This version scopes the check to each actual HTTP call site's enclosing
function/method: for every `<obj>.get/post/put/delete/stream/request(...)`
call whose target resolves (directly, or via a same-scope variable/
module-level constant) to one of the gated route paths, `X-Service-Key`
must appear either (a) in the source text of that call's nearest enclosing
FunctionDef/AsyncFunctionDef (module scope if none), or (b) on the
construction of the client object the call is made through (e.g.
`orchestrator_client = httpx.AsyncClient(..., headers={"X-Service-Key":
...})` at module scope in gateway/main.py, or `self._http_client = ...` set
once in `LiveKitIntegration.initialize()` and reused by every other method
on that instance) -- a client "constructed with the header" satisfies every
call made through it, matching mozart's DC12 carve-out.

D44/P3 (2026-09-27-deliver-athena-transit-and-base-knowledge): extended to
cover `/api/base-knowledge/public`, gated the same way after xander's diff
review found it serving a home street address with no auth. Widening
_SCAN_ROOTS to src/shared surfaced one unrelated false positive
(llm_router.py calling an MLX server's own /v1/chat/completions) --
excluded rather than special-cased in the matcher, since the collision is
this repo's, not the scanner's.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOTS = (
    REPO_ROOT / "src" / "gateway",
    REPO_ROOT / "apps" / "jarvis-web" / "backend",
    REPO_ROOT / "admin" / "backend" / "app",
    REPO_ROOT / "apps" / "chat-embed",
    # D44/P3: /api/base-knowledge/public gained the same X-Service-Key-or-
    # session gate as the orchestrator ingress routes above. Its two
    # callers live outside the roots this file originally scanned --
    # shared.admin_config.AdminConfigClient (src/shared) and the one RAG
    # service that calls it directly (src/rag/directions). Confirmed by
    # grep that no other src/rag/*/main.py references this route.
    REPO_ROOT / "src" / "shared",
    REPO_ROOT / "src" / "rag" / "directions",
    # ATHENA-114: config_loader.py's ConversationConfig.log_analytics_event
    # calls /api/internal/analytics/log with a literal path argument,
    # matched below. Its GET-based siblings (get_conversation_settings etc.)
    # route through _fetch_from_api(endpoint) -- endpoint is a call-site
    # parameter, not a local binding this scanner's per-call-site text
    # resolution can see through (same documented limitation as
    # model_downloads.py's call_control_agent indirection above). That path
    # is proven instead by a runtime MockTransport test:
    # tests/unit/test_orchestrator_config_loader_service_key.py.
    REPO_ROOT / "src" / "orchestrator",
)

# Files that *define* /sessions-shaped routes of their own rather than call
# the orchestrator's -- excluded so they don't false-positive the scan.
_EXCLUDED = {
    REPO_ROOT / "src" / "gateway" / "livekit_routes.py",
    REPO_ROOT / "admin" / "backend" / "app" / "routes" / "pipeline_events.py",
    # ATHENA-114: widening _SCAN_ROOTS to all of src/orchestrator pulls in
    # main.py (9k+ lines) and smart_home_controller.py (4.5k+ lines) -- both
    # confirmed (grep) to contain zero calls matching _ROUTE_PATTERN (their
    # /query, /sessions, etc. hits are this orchestrator's OWN @app.post/
    # @app.get route *definitions*, already excluded via
    # _ROUTE_DEFINITION_OBJECTS). _local_name_bindings walks every HTTP-
    # method call site's enclosing function unconditionally, before checking
    # whether it's gated -- across these two files' many dict.get()-style
    # calls inside multi-hundred-line functions, that walk cost turns into
    # minutes of wall-clock for zero possible true positives. Excluded for
    # scan performance, not correctness.
    REPO_ROOT / "src" / "orchestrator" / "main.py",
    REPO_ROOT / "src" / "orchestrator" / "smart_home_controller.py",
}

# codex P3b FIX: llm_router.py's httpx calls target an MLX/OpenAI-compatible
# LLM server's OWN "/v1/chat/completions" -- a generic API-shape literal
# that collides with _ROUTE_PATTERN's orchestrator-route entry of the same
# path, but is an unrelated third-party endpoint (found only after widening
# _SCAN_ROOTS to src/shared for the base-knowledge extension below).
# Narrowed to the specific call PATTERN (an httpx.AsyncClient constructed
# with base_url=endpoint_url) rather than excluding the whole file, so a
# real, unrelated, unheadered gated call added to llm_router.py later would
# still be caught.
_THIRD_PARTY_BASE_URL_MARKER = "base_url=endpoint_url"

# f-string-aware: matches the route literal whether it's a plain string or
# embedded in an f-string next to ORCHESTRATOR_URL/GATEWAY_URL-style bases.
#
# ATHENA-110: the Control Agent's mutating routes joined this list --
# /docker/{action} and /process/{action} match service_control.py's
# f-string endpoint-building (action is start/stop/restart), and the three
# literal /ollama/start|stop|restart calls are matched directly. This does
# NOT reach model_downloads.py's /huggingface/* calls: those go through a
# shared call_control_agent(method, endpoint, ...) helper whose actual
# httpx call builds its URL from an `endpoint` PARAMETER, not a literal in
# the call's own text -- this scanner's per-call-site text resolution
# can't see through that indirection (interprocedural, not in scope here).
# That path is proven instead by a runtime MockTransport test:
# admin/backend/tests/test_control_agent_caller_headers.py.
_ROUTE_PATTERN = re.compile(
    r"""/query(?:/stream(?:/v2)?)?["'?]"""
    r"""|/v1/chat/completions"""
    r"""|/sessions"""
    r"""|/session/[^"']*?/warmup"""
    r"""|/admin/(?:invalidate-feature-cache|invalidate-model-cache"""
    r"""|reset-circuit-breaker|reset-all-circuits)"""
    r"""|/api/base-knowledge/public["'?]"""
    r"""|/docker/\{action\}"""
    r"""|/process/\{action\}"""
    r"""|/ollama/(?:restart|start|stop)['"]"""
    # ATHENA-114: config_loader.py's /api/internal/* calls. Scoped to the
    # specific sub-paths this ticket's caller actually uses (config/*,
    # analytics/log) rather than a blanket /api/internal/ -- src/orchestrator/
    # intent_discovery.py separately calls /api/internal/emerging-intents
    # with no X-Service-Key, a real but pre-existing, out-of-scope bug this
    # scan must not newly surface as a widened-_SCAN_ROOTS side effect.
    r"""|/api/internal/config/[a-zA-Z\-]+"""
    r"""|/api/internal/analytics/log"""
    # ATHENA-108: shared.service_registry.register_service() POSTs here to
    # self-register every RAG/service process. Scoped to the bare
    # "/services" literal (trailing quote/`?`) so it matches only this call
    # site, not the sibling GET .../url (intentionally unauthenticated,
    # see service_registry.py's routes docstring) or POST .../toggle route
    # on the same router.
    r"""|/api/service-registry/services["'?]"""
    # xander diff-review Medium (2026-09-28): shared.service_registry.
    # unregister_service() POSTs .../toggle to disable a service at
    # shutdown -- also gated by verify_service_or_oidc, also needs the
    # header. GET .../url (service_registry.py line ~60) stays deliberately
    # unmatched -- it is documented as intentionally unauthenticated.
    r"""|/api/service-registry/services/\{service_name\}/toggle"""
    # Guest-data hardening: the admin-backend routes it gated for a service
    # caller (room groups, guest sessions by device, voice automations,
    # emerging-intent discovery, house layout, origin placeholders). The
    # intent_discovery.py calls the ATHENA-114 note above left out are
    # covered now that they send the key. The function-scoped fast check is
    # tests/unit/test_admin_guest_route_callers_send_service_key.py.
    r"""|/api/room-groups(?:/resolve/)?"""
    r"""|/api/user-sessions/device/"""
    r"""|/api/voice-automations"""
    r"""|/api/internal/emerging-intents"""
    r"""|/api/internal/intent-metrics"""
    r"""|/api/settings/house-layout"""
    r"""|/api/settings/directions-origin-placeholders"""
)

_HTTP_METHODS = {"get", "post", "put", "delete", "stream", "request"}
_HEADER_LITERAL = "X-Service-Key"
# ATHENA-110: service_control.py's three Control Agent client constructions
# use `async with httpx.AsyncClient(..., headers=control_agent_headers())`
# -- the literal header string lives inside that helper, not in the call
# site's own text. Treated as an equivalent marker everywhere
# _HEADER_LITERAL is checked below.
# Guest-data hardening: AdminConfigClient's scoped voice-automation calls
# build their headers with shared.admin_config.voice_automation_headers(),
# which always sets X-Service-Key (and the caller scope) -- same kind of
# helper as control_agent_headers().
# Route-auth review: the gateway and jarvis-web build the header with
# shared.service_key.service_key_headers() / jarvis-web's own
# _service_key_headers(), which send the key or, when it is unset or can't
# be a header value, no header at all.
_HEADER_MARKERS = (
    _HEADER_LITERAL, "control_agent_headers(", "voice_automation_headers(", "service_key_headers(",
)


def _has_header_marker(text: str) -> bool:
    return any(marker in text for marker in _HEADER_MARKERS)

# Route-*definition* decorators (`@app.post(...)`, `@router.get(...)`) are
# Call nodes too, but they define this file's own endpoints -- not a call
# out to the orchestrator. Excluded by callee object name.
_ROUTE_DEFINITION_OBJECTS = {"app", "router"}


def _iter_py_files():
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path in _EXCLUDED:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def _dotted_name(node) -> str | None:
    """`client` -> 'client'; `self._http_client` -> 'self._http_client'."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _is_asyncclient_call(call: ast.Call) -> bool:
    """Matches `httpx.AsyncClient(...)` or a bare `AsyncClient(...)`."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == "AsyncClient"
    if isinstance(func, ast.Name):
        return func.id == "AsyncClient"
    return False


class _CallSiteScanner(ast.NodeVisitor):
    """Collects (call_node, enclosing_scope_node_or_None, callee_dotted_name)
    for every HTTP-method call; (dotted_name -> constructor_call_node) for
    every module-visible name (module constant or `self.X` instance
    attribute) bound to an httpx.AsyncClient(...) construction; and
    ((scope_id, name) -> constructor_call_node) for every LOCAL `with`/
    `async with httpx.AsyncClient(...) as name:` binding (ATHENA-110 --
    service_control.py's Control Agent callers use this shape).

    The `with`-binding carve-out is deliberately scoped per enclosing
    function: a bare local name like `client` is reused across many
    unrelated functions in the same file (some Control-Agent-headered,
    some not, e.g. service_control.py's Ollama-direct calls at lines
    ~271/312/353 alongside its Control-Agent calls) -- a file-global name
    match would let one function's real header cover another function's
    unheadered call. The pre-existing Assign-based carve-out keeps its
    original file-global behaviour, unscoped, because it's what makes the
    legitimate cross-method case work (`self._http_client` set once in
    `LiveKitIntegration.initialize()`, reused by every other method on
    that instance -- there is no single enclosing function to scope to)."""

    def __init__(self):
        self._scope_stack: list[ast.AST] = []
        self.call_sites: list[tuple[ast.Call, ast.AST | None, str | None]] = []
        self.client_constructions: dict[str, ast.Call] = {}
        self.with_client_constructions: dict[tuple[int, str], ast.Call] = {}

    def _visit_scope(self, node):
        self._scope_stack.append(node)
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _HTTP_METHODS:
            callee = _dotted_name(func.value)
            if callee not in _ROUTE_DEFINITION_OBJECTS:
                scope = self._scope_stack[-1] if self._scope_stack else None
                self.call_sites.append((node, scope, callee))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.value, ast.Call) and _is_asyncclient_call(node.value):
            for target in node.targets:
                name = _dotted_name(target)
                if name is not None:
                    self.client_constructions[name] = node.value
        self.generic_visit(node)

    def _visit_with(self, node):
        scope = self._scope_stack[-1] if self._scope_stack else None
        for item in node.items:
            if (
                isinstance(item.context_expr, ast.Call)
                and _is_asyncclient_call(item.context_expr)
                and isinstance(item.optional_vars, ast.Name)
            ):
                self.with_client_constructions[(id(scope), item.optional_vars.id)] = item.context_expr
        self.generic_visit(node)

    visit_With = _visit_with
    visit_AsyncWith = _visit_with


def _module_level_assign_texts(tree: ast.Module, source: str) -> dict[str, str]:
    """name -> source text of its RHS, for simple module-level `NAME = ...`
    assignments (e.g. CACHE_INVALIDATION_ENDPOINTS = [...]). Used to expand
    a scope's text when it references a module-level constant by name
    rather than embedding the route literal inline."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            text = ast.get_source_segment(source, node.value) or ""
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = text
    return out


def _scope_text(scope: ast.AST | None, source: str) -> str:
    if scope is None:
        return source
    return ast.get_source_segment(source, scope) or source


def _local_name_bindings(scope: ast.AST | None, source: str) -> dict[str, str]:
    """name -> source text of its RHS, for `NAME = ...` assignments AND
    `for NAME in ITERABLE:` loop targets, found anywhere inside `scope`
    (module-level scan when scope is None). Covers both the direct-literal
    case and the "loop over a pre-built list of endpoints" case
    (features.py / performance_presets.py's CACHE_INVALIDATION_ENDPOINTS)."""
    out: dict[str, str] = {}
    nodes = ast.walk(scope) if scope is not None else []
    for node in nodes:
        if isinstance(node, ast.Assign):
            text = ast.get_source_segment(source, node.value) or ""
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = text
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            out[node.target.id] = ast.get_source_segment(source, node.iter) or ""
    return out


def _resolves_to_gated_route(text: str, bindings: dict[str, str], module_assigns: dict[str, str], depth: int = 0) -> bool:
    """True if `text` either directly matches a gated route, or is a bare
    name that (transitively, up to a small depth to avoid infinite loops on
    self-referential bindings) resolves to text that does."""
    if _ROUTE_PATTERN.search(text):
        return True
    if depth >= 3:
        return False
    bare_name = text.strip()
    if bare_name.isidentifier():
        rhs = bindings.get(bare_name) or module_assigns.get(bare_name)
        if rhs is not None:
            return _resolves_to_gated_route(rhs, bindings, module_assigns, depth + 1)
    return False


def _analyse_file(path: Path):
    """Returns list of dicts: {line, callee, headered} for every HTTP-method
    call whose own argument list resolves to a gated orchestrator route."""
    source = path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    scanner = _CallSiteScanner()
    scanner.visit(tree)
    module_assigns = _module_level_assign_texts(tree, source)

    # Any name (module-level OR nested, e.g. self._http_client set inside a
    # method) constructed from httpx.AsyncClient(...) with the header baked
    # in at construction time.
    preauth_clients = {
        name
        for name, ctor_call in scanner.client_constructions.items()
        if _has_header_marker(ast.get_source_segment(source, ctor_call) or "")
    }
    # Local `with ... as name:` bindings, scoped per enclosing function --
    # see _CallSiteScanner's docstring for why this one can't be file-global.
    preauth_with_clients = {
        key
        for key, ctor_call in scanner.with_client_constructions.items()
        if _has_header_marker(ast.get_source_segment(source, ctor_call) or "")
    }

    results = []
    for call_node, scope, callee in scanner.call_sites:
        # Only the call's own arguments decide whether it targets a gated
        # route -- NOT the whole enclosing function's text (that over-broad
        # check is what let an unauthenticated planted call hide next to a
        # correctly-headered real one, and also false-positives on ordinary
        # dict.get()/list.get()-style calls that share a scope with a real
        # orchestrator call).
        arg_nodes = list(call_node.args) + [kw.value for kw in call_node.keywords]
        bindings = _local_name_bindings(scope, source)

        gated = False
        for arg in arg_nodes:
            arg_text = ast.get_source_segment(source, arg) or ""
            if _resolves_to_gated_route(arg_text, bindings, module_assigns):
                gated = True
                break
            # A bare Name argument (e.g. `endpoint` in `client.post(endpoint, ...)`)
            # -- also try resolving embedded Name subnodes for f-string/BinOp cases.
            for name_node in ast.walk(arg):
                if isinstance(name_node, ast.Name) and _resolves_to_gated_route(
                    name_node.id, bindings, module_assigns
                ):
                    gated = True
                    break
            if gated:
                break

        if not gated:
            continue

        if _THIRD_PARTY_BASE_URL_MARKER in _scope_text(scope, source):
            continue

        # DC14 item 6: the header must be within THIS call expression's own
        # argument list (or resolve via the client it's made through) --
        # NOT "anywhere in the enclosing function's text". The prior
        # scope-text check missed a mutation with two gated calls in one
        # function, one headered and one not: the unheadered call read as
        # "headered" purely because the OTHER call's header text appeared
        # somewhere else in the same function body.
        call_own_text = ast.get_source_segment(source, call_node) or ""
        header_directly_present = _has_header_marker(call_own_text)

        header_via_binding = False
        if not header_directly_present:
            # Support `headers=headers` where `headers = {"X-Service-Key":
            # ...}` was bound earlier in the same scope -- still THIS
            # call's own argument (a Name), just one hop of resolution,
            # not a scope-wide text scan.
            for kw in call_node.keywords:
                if kw.arg == "headers" and isinstance(kw.value, ast.Name):
                    bound_text = bindings.get(kw.value.id)
                    if bound_text and _has_header_marker(bound_text):
                        header_via_binding = True
                        break

        headered = (
            header_directly_present
            or header_via_binding
            or (callee in preauth_clients)
            or ((id(scope), callee) in preauth_with_clients)
        )
        results.append({
            "line": call_node.lineno,
            "callee": callee,
            "headered": headered,
        })
    return results


def _find_gated_call_sites():
    """Returns {file: [result, ...]} for every file with >=1 gated call
    site, and the flat list of (file, result) pairs."""
    by_file: dict[Path, list[dict]] = {}
    flat: list[tuple[Path, dict]] = []
    for path in _iter_py_files():
        results = _analyse_file(path)
        if results:
            by_file[path] = results
            flat.extend((path, r) for r in results)
    return by_file, flat


def test_AU4_population_floor_met():
    by_file, flat = _find_gated_call_sites()
    assert len(by_file) >= 5, sorted(str(p.relative_to(REPO_ROOT)) for p in by_file)
    assert len(flat) >= 7, [(str(p.relative_to(REPO_ROOT)), r["line"]) for p, r in flat]


def test_AU4_named_members_present():
    by_file, _ = _find_gated_call_sites()
    rel_names = {str(p.relative_to(REPO_ROOT)) for p in by_file}
    expected_named = {
        "src/gateway/wyoming_bridge.py",
        "admin/backend/app/routes/sms_webhook.py",
        "apps/jarvis-web/backend/main.py",
        "src/gateway/main.py",  # covers both orchestrator_client uses and the warmup client
        "src/gateway/livekit_integration.py",
        "src/shared/admin_config.py",  # D44/P3: /api/base-knowledge/public
        "src/rag/directions/main.py",  # D44/P3: /api/base-knowledge/public
        "src/orchestrator/config_loader.py",  # ATHENA-114: /api/internal/analytics/log
        "src/orchestrator/rag_client.py",  # ATHENA-114: /api/internal/config/rag-services
    }
    missing = expected_named - rel_names
    assert not missing, f"expected named callers not matched by the scan: {missing}"


def test_AU4_every_gated_call_site_sends_service_key_header():
    _, flat = _find_gated_call_sites()
    unauthenticated = [
        f"{p.relative_to(REPO_ROOT)}:{r['line']} (via {r['callee']!r})"
        for p, r in flat
        if not r["headered"]
    ]
    assert not unauthenticated, (
        "these call sites build a request to an orchestrator route gated by "
        "require_service_caller but never send X-Service-Key (checked per "
        "call site's enclosing function, not per file): "
        f"{unauthenticated}"
    )


def test_AU4_client_constructed_with_header_carve_out_is_exercised():
    """Sanity check that the carve-out path (client built with the header
    baked in, consumed by a call site with no local header text) is
    actually reached by real code -- otherwise the carve-out itself could
    silently rot without any test noticing."""
    gateway_main = REPO_ROOT / "src" / "gateway" / "main.py"
    results = _analyse_file(gateway_main)
    source = gateway_main.read_text()
    tree = ast.parse(source)
    scanner = _CallSiteScanner()
    scanner.visit(tree)

    # orchestrator_client.post("/query", ...) has no local X-Service-Key in
    # its own function -- it must be passing via the preauth-client carve-out.
    found_carve_out_case = False
    for call_node, scope, callee in scanner.call_sites:
        if callee == "orchestrator_client" and call_node.lineno and \
                "/query" in (ast.get_source_segment(source, call_node) or ""):
            scope_text = _scope_text(scope, source)
            if _HEADER_LITERAL not in scope_text:
                found_carve_out_case = True
                break
    assert found_carve_out_case, (
        "expected at least one orchestrator_client call site with no local "
        "X-Service-Key text, to prove the carve-out (not just the direct "
        "in-scope check) is exercised by real code"
    )
    # And it must still be reported as headered.
    matching = [r for r in results if r["callee"] == "orchestrator_client"]
    assert matching, "expected orchestrator_client call sites to be found as gated"
    assert all(r["headered"] for r in matching)
