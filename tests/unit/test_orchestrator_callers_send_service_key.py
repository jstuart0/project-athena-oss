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
)

# Files that *define* /sessions-shaped routes of their own rather than call
# the orchestrator's -- excluded so they don't false-positive the scan.
_EXCLUDED = {
    REPO_ROOT / "src" / "gateway" / "livekit_routes.py",
    REPO_ROOT / "admin" / "backend" / "app" / "routes" / "pipeline_events.py",
}

# f-string-aware: matches the route literal whether it's a plain string or
# embedded in an f-string next to ORCHESTRATOR_URL/GATEWAY_URL-style bases.
_ROUTE_PATTERN = re.compile(
    r"""/query(?:/stream(?:/v2)?)?["'?]"""
    r"""|/v1/chat/completions"""
    r"""|/sessions"""
    r"""|/session/[^"']*?/warmup"""
    r"""|/admin/(?:invalidate-feature-cache|invalidate-model-cache"""
    r"""|reset-circuit-breaker|reset-all-circuits)"""
)

_HTTP_METHODS = {"get", "post", "put", "delete", "stream", "request"}
_HEADER_LITERAL = "X-Service-Key"

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
    for every HTTP-method call, and (dotted_name -> assign_node) for every
    assignment whose RHS constructs an httpx.AsyncClient."""

    def __init__(self):
        self._scope_stack: list[ast.AST] = []
        self.call_sites: list[tuple[ast.Call, ast.AST | None, str | None]] = []
        self.client_constructions: dict[str, ast.Assign] = {}

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
                    self.client_constructions[name] = node
        self.generic_visit(node)


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
        for name, assign_node in scanner.client_constructions.items()
        if _HEADER_LITERAL in (ast.get_source_segment(source, assign_node.value) or "")
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

        # DC14 item 6: the header must be within THIS call expression's own
        # argument list (or resolve via the client it's made through) --
        # NOT "anywhere in the enclosing function's text". The prior
        # scope-text check missed a mutation with two gated calls in one
        # function, one headered and one not: the unheadered call read as
        # "headered" purely because the OTHER call's header text appeared
        # somewhere else in the same function body.
        call_own_text = ast.get_source_segment(source, call_node) or ""
        header_directly_present = _HEADER_LITERAL in call_own_text

        header_via_binding = False
        if not header_directly_present:
            # Support `headers=headers` where `headers = {"X-Service-Key":
            # ...}` was bound earlier in the same scope -- still THIS
            # call's own argument (a Name), just one hop of resolution,
            # not a scope-wide text scan.
            for kw in call_node.keywords:
                if kw.arg == "headers" and isinstance(kw.value, ast.Name):
                    bound_text = bindings.get(kw.value.id)
                    if bound_text and _HEADER_LITERAL in bound_text:
                        header_via_binding = True
                        break

        headered = header_directly_present or header_via_binding or (callee in preauth_clients)
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
