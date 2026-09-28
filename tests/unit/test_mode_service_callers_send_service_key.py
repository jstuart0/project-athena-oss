"""ATHENA-127 Phase 3/4 -- every in-repo caller of a `X-Service-Key`-gated
mode-service/guest-mode route actually sends it.

Modelled on tests/unit/test_orchestrator_callers_send_service_key.py's
per-call-site (not per-file) AST scan, trimmed to this narrower surface:
`src/mode_service` and `admin/backend/app`. Gated targets are matched
path-exact on string literals/f-strings: `/api/internal/guest-mode/...`,
`/api/guest-mode/config`, and `/mode` or `/mode/...` -- the `/mode-status`
proxy route DECORATOR string must not match (it's this repo's own route
definition, not a call out to the mode service).

Phase 3 floor: >= 3 gated call sites (`load_config`, `_verify_owner_pin`,
the bookings.py admin fetch). Phase 4 raises the floor to >= 4 with the
admin `GET /api/guest-mode/mode-status` proxy (guest_mode.py) as a second
named member.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOTS = (
    REPO_ROOT / "src" / "mode_service",
    REPO_ROOT / "admin" / "backend" / "app",
)

# Path-exact: a bare "/mode-status" (the admin proxy's OWN route decorator
# string) must not match "/mode" or "/mode/...". The negative lookahead
# excludes "-status" (and any other non-slash, non-quote suffix) right
# after "/mode".
_ROUTE_PATTERN = re.compile(
    r"""/api/internal/guest-mode/[a-zA-Z\-]+"""
    r"""|/api/guest-mode/config["'?]"""
    r"""|/mode(?:/[a-zA-Z\-]*)?["'?]"""
)

_HTTP_METHODS = {"get", "post", "put", "delete", "stream", "request"}
_HEADER_LITERAL = "X-Service-Key"
_ROUTE_DEFINITION_OBJECTS = {"app", "router", "guest_mode_pin_router"}


def _iter_py_files():
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _dotted_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _is_asyncclient_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == "AsyncClient"
    if isinstance(func, ast.Name):
        return func.id == "AsyncClient"
    return False


class _CallSiteScanner(ast.NodeVisitor):
    def __init__(self):
        self._scope_stack: list[ast.AST] = []
        self.call_sites: list[tuple[ast.Call, ast.AST | None, str | None]] = []
        self.client_constructions: dict[str, ast.Call] = {}

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


def _scope_text(scope: ast.AST | None, source: str) -> str:
    if scope is None:
        return source
    return ast.get_source_segment(source, scope) or source


def _local_name_bindings(scope: ast.AST | None, source: str) -> dict[str, str]:
    """name -> source text of every RHS it's ever assigned in `scope`
    (module-level when scope is None). Deliberately permissive (unlike the
    per-call-site orchestrator scanner): this file's known real sites
    include `headers["X-Service-Key"] = key` (a Subscript mutation of a
    dict bound earlier as `headers = {}`), which a single-Assign-per-name
    capture would miss. Concatenating every RHS assigned to that name
    catches it, at the cost of being scope-wide rather than call-site-exact
    for this narrow surface."""
    out: dict[str, list[str]] = {}
    nodes = ast.walk(scope) if scope is not None else ast.walk(ast.parse(source))
    for node in nodes:
        if isinstance(node, ast.Assign):
            text = ast.get_source_segment(source, node.value) or ""
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(text)
    return {name: " ".join(texts) for name, texts in out.items()}


def _analyse_source(source: str, path_label: str = "<memory>"):
    """Returns list of {line, callee, headered} for every HTTP-method call
    whose own argument list resolves to a gated route. Works on raw source
    text (not just a file on disk), so a planted-mutant test can exercise
    the detector without writing to the real tree."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    scanner = _CallSiteScanner()
    scanner.visit(tree)

    preauth_clients = {
        name
        for name, ctor_call in scanner.client_constructions.items()
        if _HEADER_LITERAL in (ast.get_source_segment(source, ctor_call) or "")
    }

    results = []
    for call_node, scope, callee in scanner.call_sites:
        arg_nodes = list(call_node.args) + [kw.value for kw in call_node.keywords]
        bindings_for_gate = _local_name_bindings(scope, source)
        gated = False
        for arg in arg_nodes:
            arg_text = ast.get_source_segment(source, arg) or ""
            if _ROUTE_PATTERN.search(arg_text):
                gated = True
                break
            # A bare Name argument (e.g. `url` built as
            # f"{mode_service_url}/mode" a few lines earlier) -- resolve
            # one hop against every RHS ever assigned to that name in this
            # scope (guest_mode.py's mode-status proxy builds its URL this
            # way, rather than inlining the literal into the call).
            if isinstance(arg, ast.Name):
                bound_text = bindings_for_gate.get(arg.id, "")
                if _ROUTE_PATTERN.search(bound_text):
                    gated = True
                    break
        if not gated:
            continue

        call_own_text = ast.get_source_segment(source, call_node) or ""
        headered = _HEADER_LITERAL in call_own_text or callee in preauth_clients

        if not headered:
            # Fallback: a `headers=<Name>` argument whose name was ever
            # assigned (directly or via subscript mutation) a value
            # containing the marker, anywhere in the enclosing scope.
            for kw in call_node.keywords:
                if kw.arg == "headers" and isinstance(kw.value, ast.Name):
                    bindings = _local_name_bindings(scope, source)
                    bound_text = bindings.get(kw.value.id, "")
                    scope_text = _scope_text(scope, source)
                    if _HEADER_LITERAL in bound_text or (
                        _HEADER_LITERAL in scope_text and f'{kw.value.id}["' in scope_text
                    ):
                        headered = True
                        break

        results.append({"line": call_node.lineno, "callee": callee, "headered": headered, "file": path_label})
    return results


def _find_gated_call_sites():
    by_file: dict[Path, list[dict]] = {}
    flat: list[tuple[Path, dict]] = []
    for path in _iter_py_files():
        results = _analyse_source(path.read_text(), path_label=str(path))
        if results:
            by_file[path] = results
            flat.extend((path, r) for r in results)
    return by_file, flat


def test_population_floor_met():
    by_file, flat = _find_gated_call_sites()
    assert len(flat) >= 4, [(str(p.relative_to(REPO_ROOT)), r["line"]) for p, r in flat]


def test_named_members_present():
    by_file, _ = _find_gated_call_sites()
    rel_names = {str(p.relative_to(REPO_ROOT)) for p in by_file}
    expected_named = {
        "src/mode_service/bookings.py",
        "src/mode_service/main.py",
        "admin/backend/app/routes/guest_mode.py",
    }
    missing = expected_named - rel_names
    assert not missing, f"expected named callers not matched by the scan: {missing}"


def test_mode_status_decorator_string_is_not_matched():
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/mode-status')\n"
        "async def mode_status():\n"
        "    return {}\n"
    )
    results = _analyse_source(source)
    assert results == []


def test_every_gated_call_site_sends_service_key_header():
    _, flat = _find_gated_call_sites()
    unauthenticated = [
        f"{p.relative_to(REPO_ROOT)}:{r['line']} (via {r['callee']!r})"
        for p, r in flat
        if not r["headered"]
    ]
    assert not unauthenticated, (
        "these call sites build a request to a gated mode-service/guest-mode "
        f"route but never send X-Service-Key: {unauthenticated}"
    )


def test_planted_unheadered_call_is_flagged():
    """Proves the detector actually fires: a synthetic, deliberately
    unheadered call to a gated route must be reported unheadered."""
    source = (
        "import httpx\n"
        "\n"
        "async def caller():\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        return await client.get('http://x/api/internal/guest-mode/bookings')\n"
    )
    results = _analyse_source(source)
    assert len(results) == 1
    assert results[0]["headered"] is False
