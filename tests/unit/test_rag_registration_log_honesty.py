"""ATHENA-108 follow-up: every RAG connector's registration call site must
log success only when the client actually reports it.

register_service()/startup_service() (src/shared/service_registry.py) both
return bool honestly. The bug this guards against lives at the *call
site*: src/rag/flights/main.py's lifespan discarded register_service()'s
return value and logged "Service registered: ..." at info level
unconditionally, in the same try block, regardless of whether the POST
actually succeeded (401/422/connection error all logged the identical
false-positive success line). Static AST scan, no mocking -- catches the
call-site shape, not the client's own (already-honest) internals.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_RAG_ROOT = REPO_ROOT / "src" / "rag"

_REGISTER_CALL_NAMES = {"register_service", "startup_service"}
_LOG_OBJECT_NAMES = {"logger"}


def _register_call_name(node: ast.AST) -> str | None:
    """Returns the callee name if `node` is `[await] register_service(...)`
    or `[await] startup_service(...)`, else None."""
    call = node.value if isinstance(node, ast.Await) else node
    if not isinstance(call, ast.Call):
        return None
    func = call.func
    if isinstance(func, ast.Name) and func.id in _REGISTER_CALL_NAMES:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _REGISTER_CALL_NAMES:
        return func.attr
    return None


def _logger_success_call(node: ast.stmt) -> ast.Call | None:
    """Returns the Call node if `node` is a bare `logger.info(...)`/
    `logger.debug(...)` expression statement whose message text mentions
    registration and doesn't itself read like a failure message. None
    otherwise."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("info", "debug")):
        return None
    base = func.value
    if not (isinstance(base, ast.Name) and base.id in _LOG_OBJECT_NAMES):
        return None
    args = list(call.args) + [kw.value for kw in call.keywords]
    text = " ".join(ast.dump(a) for a in args).lower()
    if "regist" not in text:
        return None
    if "fail" in text or "error" in text:
        return None
    return call


def _iter_stmt_lists(tree: ast.Module):
    """Yields every list-of-statements body in the module: function bodies,
    if/for/while/with/try bodies and orelse/finalbody branches, except
    handler bodies -- everywhere a register call could sit next to a log
    call in the same block."""
    for node in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            value = getattr(node, attr, None)
            if isinstance(value, list) and (not value or isinstance(value[0], ast.stmt)):
                yield value


def find_unconditional_success_logs(source: str, label: str) -> list[str]:
    """Returns human-readable findings for every register_service()/
    startup_service() call whose return value is discarded (a bare
    expression statement, not assigned) and is immediately followed -- in
    the same statement block, within 2 statements -- by an unconditional
    logger.info/debug call whose message reads as registration success.

    A call site that captures the return value (`ok = await
    register_service(...)`) is never flagged here: guarding the log behind
    that captured value is exactly the fix, and startup_service() already
    does this correctly internally."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    findings: list[str] = []
    for body in _iter_stmt_lists(tree):
        for i, stmt in enumerate(body):
            if not (isinstance(stmt, ast.Expr) and _register_call_name(stmt.value)):
                continue
            for nxt in body[i + 1 : i + 3]:
                log_call = _logger_success_call(nxt)
                if log_call is not None:
                    findings.append(
                        f"{label}:{stmt.lineno} discards the register call's return "
                        f"value, then {label}:{nxt.lineno} logs registration success "
                        "unconditionally"
                    )
    return findings


def _rag_main_files_with_register_calls() -> list[Path]:
    out = []
    for path in sorted(_RAG_ROOT.glob("*/main.py")):
        source = path.read_text()
        if any(name in source for name in _REGISTER_CALL_NAMES):
            out.append(path)
    return out


def test_population_floor_met():
    files = _rag_main_files_with_register_calls()
    assert len(files) >= 15, (
        f"expected register_service/startup_service call sites in >=15 "
        f"src/rag/*/main.py files, found {len(files)}: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in files]}"
    )


def test_named_member_present():
    files = {str(p.relative_to(REPO_ROOT)) for p in _rag_main_files_with_register_calls()}
    assert "src/rag/weather/main.py" in files
    assert "src/rag/flights/main.py" in files


def test_no_rag_main_logs_registration_success_unconditionally():
    findings: list[str] = []
    for path in _rag_main_files_with_register_calls():
        rel = str(path.relative_to(REPO_ROOT))
        findings.extend(find_unconditional_success_logs(path.read_text(), rel))
    assert findings == [], "\n".join(findings)


def test_scanner_catches_the_original_flights_bug_shape():
    """Positive control: proves the scanner has teeth against the exact
    pattern that shipped in src/rag/flights/main.py before this fix, not
    just that the (now-fixed) real files happen to pass."""
    bad_source = '''
import structlog
logger = structlog.get_logger()

async def lifespan(app):
    try:
        await register_service(SERVICE_NAME, SERVICE_PORT, "Flight tracking")
        logger.info(f"Service registered: {SERVICE_NAME} on port {SERVICE_PORT}")
    except Exception as e:
        logger.error(f"Failed to register service: {e}")
'''
    findings = find_unconditional_success_logs(bad_source, "synthetic.py")
    assert findings, "scanner must flag a discarded register call followed by an unconditional success log"


def test_scanner_allows_a_guarded_success_log():
    """A call site that captures the return value and logs conditionally
    (the fixed shape) must never be flagged."""
    good_source = '''
import structlog
logger = structlog.get_logger()

async def lifespan(app):
    try:
        registered = await register_service(SERVICE_NAME, SERVICE_PORT, "Flight tracking")
        if registered:
            logger.info(f"Service registered: {SERVICE_NAME} on port {SERVICE_PORT}")
        else:
            logger.warning(f"Service registration failed: {SERVICE_NAME} on port {SERVICE_PORT}")
    except Exception as e:
        logger.error(f"Failed to register service: {e}")
'''
    findings = find_unconditional_success_logs(good_source, "synthetic.py")
    assert findings == []
