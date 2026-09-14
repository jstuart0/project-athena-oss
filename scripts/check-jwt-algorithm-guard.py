#!/usr/bin/env python3
"""
check-jwt-algorithm-guard.py — mechanical precondition for scripts/audit-images.sh's
PYSEC-2026-1325 (ecdsa, transitive via python-jose[cryptography]) allowlist.

That allowlist's reasoning is: this codebase's only JWT usage that could reach
ecdsa's signing API is python-jose, and every python-jose call site here is
HS256 (symmetric HMAC) — which never touches ecdsa. `grep "algorithms=\\["`
cannot verify this on its own: it is blind to a call that omits `algorithms=`
entirely (python-jose's jwt.decode() defaults algorithms=None and only
enforces the allowlist when it is not None, so an omitted keyword accepts
whatever `alg` the token header claims), a `JWT_ALGORITHM` reassignment, an
aliased import (`import jose.jwt`, `from jose.jwt import decode as d`,
`getattr(jwt, "decode")`), or the `jws` module's lower-level `verify`/`sign`
(the actual ecdsa-reachability primitive `jwt.decode`/`jwt.encode` wrap).

This script asserts, via AST (never text matching — a match inside a comment
or a string never counts, and every call target is resolved to a fully
qualified python-jose name rather than matched by attribute spelling alone):

  1. `JWT_ALGORITHM` is bound exactly once across the scanned tree: a
     top-level (module-body) literal string `"HS256"` in
     admin/backend/app/auth/oidc.py. Any other assignment (`=`, augmented,
     or annotated), any `global JWT_ALGORITHM` declaration in a function
     (a rebinding vector even before the reassignment it enables), and any
     attribute assignment (`x.JWT_ALGORITHM = ...`) are all failures.
  2. Every call to a tracked python-jose callable passes the required
     keyword explicitly, with a value this script can prove is HS256-only:
       - `jose.jwt.decode` / `jose.jws.verify` require `algorithms=`, a
         list literal whose every element is the literal `"HS256"` and/or
         the `JWT_ALGORITHM` name (only once (1) has verified what that
         name is bound to).
       - `jose.jwt.encode` / `jose.jws.sign` require `algorithm=`, either
         `"HS256"` or the `JWT_ALGORITHM` name.
     `**kwargs`, positional use, an empty list, a non-list value, or any
     other algorithm name (RS*, PS*, ES*, EdDSA, "none", ...) all fail.
  3. Any of the above tracked callables *referenced without being called*
     (assigned to a variable, passed as an argument, returned) fails —
     a reference this script cannot prove is inert.
  4. `getattr(x, ...)` where `x` resolves to `jose`, `jose.jwt`, or
     `jose.jws` (however it was imported or aliased) fails — dynamic
     attribute access defeats every check above.

Every python-jose import form is resolved to a canonical dotted name before
any of the above runs: `import jose`; `import jose.jwt [as x]`;
`import jose.jws [as x]`; `from jose import jwt/jws [as x]`;
`from jose.jwt import decode/encode [as x]`;
`from jose.jws import verify/sign [as x]` — including arbitrary attribute
chains built on any of these (`jose.jwt.decode`, `x.decode`, ...).

Scan scope: all of admin/backend EXCLUDING admin/backend/tests/ (test
fixtures legitimately construct jose calls with dangerous algorithms to
prove other code paths reject them — treating those as production findings
would make the guard permanently red), plus src/shared, since that package
is installed into the admin-backend image and the reachability argument
covers everything the image ships.

Scope note (ian I2b): this repo has no direct PyJWT usage in admin/backend
or main.py — PyJWT-*named* imports appear only inside admin/backend/tests
fixtures. Both of the WS-ticket call sites are python-jose end to end:
main.py:985 mints the ticket via oidc.create_access_token (jose jwt.encode),
and oidc.py:294 (`decode_ws_ticket`'s underlying call) decodes it back
(jose jwt.decode). This guard's HS256-only claim is therefore a claim about
the *only* JWT library this codebase's shipped code calls, not one of two.

Exit codes:
  0 — guard holds; the allowlist may be applied.
  1 — guard failed (a real finding: the HS256-only assumption no longer
      holds, or is unverifiable). The caller must refuse the allowlist.
  2 — the check itself could not run (missing directory, unreadable or
      unparsable file — including a non-UTF-8 file under the scanned
      tree). Never treated as passing.

Usage:
  python3 scripts/check-jwt-algorithm-guard.py [--root PATH]
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass, field

ADMIN_BACKEND_REL_DIR = "admin/backend"
ADMIN_BACKEND_TESTS_REL_DIR = "admin/backend/tests"
SHARED_REL_DIR = "src/shared"
OIDC_REL_PATH = "admin/backend/app/auth/oidc.py"
REQUIRED_ALGORITHM = "HS256"

# Canonical dotted python-jose names this guard tracks.
DECODE_LIKE = {"jose.jwt.decode", "jose.jws.verify"}   # require algorithms=
ENCODE_LIKE = {"jose.jwt.encode", "jose.jws.sign"}     # require algorithm=
TRACKED_CALLABLES = DECODE_LIKE | ENCODE_LIKE
JOSE_MODULE_QUALNAMES = {"jose", "jose.jwt", "jose.jws"}


class ToolError(Exception):
    """Raised when the check itself cannot run (exit 2, never a pass)."""


@dataclass
class ScannedFile:
    rel_path: str
    tree: ast.Module
    top_level_ids: set = field(default_factory=set)


def _iter_target_files(root: str) -> list:
    """Absolute paths of every .py file in scope, tests excluded."""
    admin_backend_dir = os.path.join(root, ADMIN_BACKEND_REL_DIR)
    tests_dir = os.path.join(root, ADMIN_BACKEND_TESTS_REL_DIR)
    shared_dir = os.path.join(root, SHARED_REL_DIR)

    if not os.path.isdir(admin_backend_dir):
        raise ToolError(f"{ADMIN_BACKEND_REL_DIR} not found under {root!r}")
    if not os.path.isdir(shared_dir):
        raise ToolError(f"{SHARED_REL_DIR} not found under {root!r}")

    paths = []
    for scan_root, exclude in ((admin_backend_dir, tests_dir), (shared_dir, None)):
        for dirpath, dirnames, filenames in os.walk(scan_root):
            if exclude is not None and (dirpath == exclude or dirpath.startswith(exclude + os.sep)):
                dirnames[:] = []
                continue
            for filename in filenames:
                if filename.endswith(".py"):
                    paths.append(os.path.join(dirpath, filename))
    return sorted(paths)


def _parse_file(root: str, abs_path: str) -> ScannedFile:
    rel_path = os.path.relpath(abs_path, root)
    try:
        with open(abs_path, encoding="utf-8") as f:
            src = f.read()
    except UnicodeDecodeError as e:
        raise ToolError(f"could not read {rel_path} as UTF-8: {e}") from e
    except OSError as e:
        raise ToolError(f"could not read {rel_path}: {e}") from e
    try:
        tree = ast.parse(src, filename=abs_path)
    except SyntaxError as e:
        raise ToolError(f"could not parse {rel_path}: {e}") from e
    top_level_ids = {id(stmt) for stmt in tree.body}
    return ScannedFile(rel_path=rel_path, tree=tree, top_level_ids=top_level_ids)


def _find_jose_bindings(tree: ast.Module) -> dict:
    """Local name -> canonical dotted python-jose name, for every import
    form this guard resolves. Scans the whole file (not just module level)
    so a local `import` inside a function is still caught."""
    bindings: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("jose", "jose.jwt", "jose.jws"):
                    if alias.asname:
                        bindings[alias.asname] = alias.name
                    else:
                        # `import jose[.jwt|.jws]` without `as` binds only
                        # the root name; attribute chains off it resolve
                        # further via _resolve_expr.
                        bindings["jose"] = "jose"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "jose":
                for alias in node.names:
                    if alias.name in ("jwt", "jws"):
                        bindings[alias.asname or alias.name] = f"jose.{alias.name}"
            elif node.module == "jose.jwt":
                for alias in node.names:
                    if alias.name in ("decode", "encode"):
                        bindings[alias.asname or alias.name] = f"jose.jwt.{alias.name}"
            elif node.module == "jose.jws":
                for alias in node.names:
                    if alias.name in ("verify", "sign"):
                        bindings[alias.asname or alias.name] = f"jose.jws.{alias.name}"
    return bindings


def _resolve_expr(node: ast.expr, bindings: dict) -> "str | None":
    """Resolve a Name/Attribute chain to a canonical dotted name, or None
    if it isn't rooted in a known jose binding."""
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve_expr(node.value, bindings)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _value_is_hs256_or_name(node: ast.expr, algorithm_name_ok: bool) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == REQUIRED_ALGORITHM
    if isinstance(node, ast.Name) and node.id == "JWT_ALGORITHM":
        return algorithm_name_ok
    return False


def _validate_algorithms_list(node: ast.expr, algorithm_name_ok: bool) -> list:
    if not isinstance(node, ast.List):
        return ["algorithms= is not a list literal — cannot statically verify its contents"]
    if not node.elts:
        return ["algorithms= is an empty list — fails closed as unverifiable"]
    errors = []
    for elt in node.elts:
        if isinstance(elt, ast.Starred):
            errors.append("algorithms= contains a starred/unpacked element — cannot statically verify")
            continue
        if not _value_is_hs256_or_name(elt, algorithm_name_ok):
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                errors.append(f"algorithms= includes disallowed algorithm {elt.value!r}")
            else:
                errors.append(
                    "algorithms= contains a value this check cannot resolve statically "
                    "(expected the literal \"HS256\" or the JWT_ALGORITHM name)"
                )
    return errors


def _validate_algorithm_scalar(node: ast.expr, algorithm_name_ok: bool) -> list:
    if _value_is_hs256_or_name(node, algorithm_name_ok):
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [f"algorithm= is {node.value!r}, expected {REQUIRED_ALGORITHM!r} or the JWT_ALGORITHM name"]
    return ["algorithm= contains a value this check cannot resolve statically"]


def _check_jwt_algorithm_binding(files: list) -> list:
    """Exactly one binding, top-level, a literal 'HS256', in oidc.py."""
    errors = []
    bindings = []  # (rel_path, lineno, top_level, literal_or_None)

    for sf in files:
        for node in ast.walk(sf.tree):
            if isinstance(node, ast.Global) and "JWT_ALGORITHM" in node.names:
                errors.append(
                    f"{sf.rel_path}:{node.lineno}: `global JWT_ALGORITHM` declared — "
                    "JWT_ALGORITHM must not be rebindable from a function scope"
                )
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == "JWT_ALGORITHM":
                        literal = None
                        value_node = getattr(node, "value", None)
                        if isinstance(node, ast.AugAssign):
                            literal = None  # `+=` etc. is never a valid literal binding
                        elif isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                            literal = value_node.value
                        bindings.append((sf.rel_path, node.lineno, id(node) in sf.top_level_ids, literal))
                    elif isinstance(target, ast.Attribute) and target.attr == "JWT_ALGORITHM":
                        errors.append(
                            f"{sf.rel_path}:{node.lineno}: attribute assignment to "
                            "JWT_ALGORITHM is not allowed"
                        )

    if len(bindings) != 1:
        if not bindings:
            errors.append("JWT_ALGORITHM is not assigned anywhere in the scanned tree")
        else:
            locs = ", ".join(f"{r}:{l}" for r, l, _, _ in bindings)
            errors.append(f"expected exactly one JWT_ALGORITHM binding, found {len(bindings)}: {locs}")
    else:
        rel_path, lineno, top_level, literal = bindings[0]
        if rel_path != OIDC_REL_PATH or not top_level or literal != REQUIRED_ALGORITHM:
            shown = repr(literal) if literal is not None else "a non-literal value"
            errors.append(
                f"{rel_path}:{lineno}: JWT_ALGORITHM binding is {shown} (top_level={top_level}) — "
                f"expected a single top-level JWT_ALGORITHM = {REQUIRED_ALGORITHM!r} in {OIDC_REL_PATH}"
            )

    return errors


def _check_calls_in_file(sf: ScannedFile, algorithm_name_ok: bool) -> list:
    bindings = _find_jose_bindings(sf.tree)
    if not bindings:
        return []

    errors = []
    call_target_ids = {id(call.func) for call in ast.walk(sf.tree) if isinstance(call, ast.Call)}

    for node in ast.walk(sf.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # Dynamic access: getattr(<jose binding>, ...).
        if isinstance(func, ast.Name) and func.id == "getattr" and node.args:
            base_q = _resolve_expr(node.args[0], bindings)
            if base_q is not None and (base_q in JOSE_MODULE_QUALNAMES or base_q in TRACKED_CALLABLES):
                errors.append(
                    f"{sf.rel_path}:{node.lineno}: getattr(...) used against jose binding "
                    f"{base_q!r} — dynamic access cannot be statically verified"
                )
            continue

        qualname = _resolve_expr(func, bindings)
        if qualname not in TRACKED_CALLABLES:
            continue

        site = f"{sf.rel_path}:{node.lineno}"
        has_double_star = any(kw.arg is None for kw in node.keywords)
        required_kw = "algorithms" if qualname in DECODE_LIKE else "algorithm"
        kw = next((k for k in node.keywords if k.arg == required_kw), None)

        if kw is None:
            if has_double_star:
                errors.append(
                    f"{site}: {qualname}(...) call uses **kwargs — {required_kw}= "
                    "cannot be statically verified"
                )
            else:
                errors.append(f"{site}: {qualname}(...) call has no {required_kw}= argument")
            continue

        if qualname in DECODE_LIKE:
            errs = _validate_algorithms_list(kw.value, algorithm_name_ok)
        else:
            errs = _validate_algorithm_scalar(kw.value, algorithm_name_ok)
        errors.extend(f"{site}: {e}" for e in errs)

    # Tracked callables referenced without being called anywhere in the file.
    for node in ast.walk(sf.tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        qualname = _resolve_expr(node, bindings)
        if qualname in TRACKED_CALLABLES and id(node) not in call_target_ids:
            errors.append(
                f"{sf.rel_path}:{node.lineno}: {qualname} referenced without being called "
                "— cannot statically verify how it will be used"
            )

    return errors


def check(root: str) -> "tuple[list[str], str | None]":
    """Returns (guard_errors, tool_error). Exactly one of the two is
    meaningful: tool_error set means the check itself could not run
    (exit 2); otherwise guard_errors is the list of real findings
    (exit 1 if non-empty)."""
    try:
        abs_paths = _iter_target_files(root)
        files = [_parse_file(root, p) for p in abs_paths]
    except ToolError as e:
        return [], str(e)

    errors: list = []
    algorithm_binding_errors = _check_jwt_algorithm_binding(files)
    errors.extend(algorithm_binding_errors)
    algorithm_name_ok = not algorithm_binding_errors

    for sf in files:
        errors.extend(_check_calls_in_file(sf, algorithm_name_ok))

    return errors, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="Repo root to check (default: cwd).")
    args = parser.parse_args()
    root = os.path.abspath(args.root)

    try:
        errors, tool_error = check(root)
    except Exception as e:  # last-resort safety net — never an uncaught exception (rule 6)
        print(f"TOOL_ERROR: unexpected failure: {e!r}")
        return 2

    if tool_error:
        print(f"TOOL_ERROR: {tool_error}")
        return 2

    if errors:
        for e in errors:
            print(f"GUARD_FAIL: {e}")
        print("JWT ALGORITHM GUARD FAILED — refusing PYSEC-2026-1325 allowlist")
        return 1

    print("JWT ALGORITHM GUARD OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
