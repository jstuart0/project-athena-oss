#!/usr/bin/env python3
"""
check-jwt-algorithm-guard.py — mechanical precondition for scripts/audit-images.sh's
PYSEC-2026-1325 (ecdsa, transitive via python-jose[cryptography]) allowlist.

That allowlist's reasoning is: this codebase's only python-jose usage is HS256
(symmetric HMAC), which never calls ecdsa's signing API, so PYSEC-2026-1325 is
unreachable. `grep "algorithms=\\["` cannot verify this on its own — python-jose's
jwt.decode() defaults `algorithms=None` (jose/jwt.py:66) and jws.py:258 only
enforces the allowlist `if algorithms is not None`, so a call site that omits
`algorithms=` entirely accepts whatever `alg` the token header claims, and a
grep for the literal string `algorithms=[` cannot see a value that isn't a
list literal, or a JWT_ALGORITHM reassignment to an EC algorithm.

This script asserts, via AST (not text matching, so a match inside a comment
or a string never counts):
  1. admin/backend/app/auth/oidc.py defines a module-level
     `JWT_ALGORITHM = "HS256"` (a literal string, not a computed value).
  2. Every call to python-jose's jwt.decode(...) under admin/backend/app
     passes `algorithms=` explicitly.
  3. That `algorithms=` value is a list literal whose every element is either
     the literal string "HS256", or a reference to the JWT_ALGORITHM name
     already verified in (1) — anything else (an EC algorithm, or a value
     this script cannot statically resolve) fails closed.

Only python-jose's `jwt` (bound via `from jose import jwt[, ...]` or
`from jose import jwt as X`) is in scope — this repo's other JWT calls
(PyJWT, used elsewhere for WS tickets) are a different library with a
different default-deny posture and are not part of PYSEC-2026-1325's
reachability argument.

Exit codes:
  0 — guard holds; the allowlist may be applied.
  1 — guard failed (a real finding: the HS256-only assumption no longer
      holds, or is unverifiable). The caller must refuse the allowlist.
  2 — the check itself could not run (file missing, unparsable). Never
      treated as passing.

Usage:
  python3 scripts/check-jwt-algorithm-guard.py [--root PATH]
"""
import argparse
import ast
import os
import sys

OIDC_REL_PATH = "admin/backend/app/auth/oidc.py"
APP_REL_DIR = "admin/backend/app"
EC_ALGORITHMS = {"ES256", "ES384", "ES512"}
REQUIRED_ALGORITHM = "HS256"


def find_jose_jwt_local_names(tree: ast.AST) -> set[str]:
    """Names in this module bound to python-jose's `jwt` submodule."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "jose":
            for alias in node.names:
                if alias.name == "jwt":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jose.jwt":
                    names.add(alias.asname or "jose")
    return names


def find_jwt_algorithm_value(tree: ast.AST) -> "str | object":
    """Return the literal string assigned to a module-level JWT_ALGORITHM,
    or a sentinel object if it isn't found or isn't a plain string literal."""
    _MISSING = object()
    value = _MISSING
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "JWT_ALGORITHM":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        value = node.value.value
                    else:
                        value = _MISSING  # computed/non-literal — cannot verify
    return value


def algorithms_list_is_safe(node: ast.expr, algorithm_name_ok: bool) -> "list[str]":
    """Return a list of error strings; empty list means the algorithms= value
    is provably HS256-only."""
    errors = []
    if not isinstance(node, ast.List):
        errors.append("algorithms= is not a list literal — cannot statically verify its contents")
        return errors
    if not node.elts:
        errors.append("algorithms= is an empty list — no algorithm would validate, but this still fails closed as unverifiable intent")
        return errors
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            if elt.value in EC_ALGORITHMS:
                errors.append(f"algorithms= includes EC algorithm {elt.value!r}")
            elif elt.value != REQUIRED_ALGORITHM:
                errors.append(f"algorithms= includes non-HS256 algorithm {elt.value!r}")
        elif isinstance(elt, ast.Name) and elt.id == "JWT_ALGORITHM" and algorithm_name_ok:
            pass  # already independently verified == "HS256"
        else:
            errors.append(
                "algorithms= contains a value this check cannot resolve statically "
                "(expected the literal \"HS256\" or the JWT_ALGORITHM name)"
            )
    return errors


def check(root: str) -> "tuple[list[str], str | None]":
    """Returns (guard_errors, tool_error). Exactly one of the two is meaningful:
    tool_error set means the check itself could not run (exit 2); otherwise
    guard_errors is the list of real findings (exit 1 if non-empty)."""
    oidc_path = os.path.join(root, OIDC_REL_PATH)
    if not os.path.isfile(oidc_path):
        return [], f"{OIDC_REL_PATH} not found under {root!r}"
    try:
        with open(oidc_path, encoding="utf-8") as f:
            oidc_tree = ast.parse(f.read(), filename=oidc_path)
    except SyntaxError as e:
        return [], f"could not parse {OIDC_REL_PATH}: {e}"

    errors: list[str] = []

    algorithm_value = find_jwt_algorithm_value(oidc_tree)
    algorithm_name_ok = algorithm_value == REQUIRED_ALGORITHM
    if not algorithm_name_ok:
        shown = repr(algorithm_value) if isinstance(algorithm_value, str) else "undefined or not a literal string"
        errors.append(f"{OIDC_REL_PATH}: JWT_ALGORITHM is {shown}, expected {REQUIRED_ALGORITHM!r}")

    app_dir = os.path.join(root, APP_REL_DIR)
    if not os.path.isdir(app_dir):
        return [], f"{APP_REL_DIR} not found under {root!r}"

    for dirpath, _dirnames, filenames in sorted(os.walk(app_dir)):
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(path, root)
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                tree = ast.parse(src, filename=path)
            except SyntaxError as e:
                return [], f"could not parse {rel_path}: {e}"

            jose_jwt_names = find_jose_jwt_local_names(tree)
            if not jose_jwt_names:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_jose_jwt_decode = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "decode"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in jose_jwt_names
                )
                if not is_jose_jwt_decode:
                    continue

                algorithms_kw = next((kw for kw in node.keywords if kw.arg == "algorithms"), None)
                site = f"{rel_path}:{node.lineno}"
                if algorithms_kw is None:
                    errors.append(f"{site}: jwt.decode(...) call has no algorithms= argument")
                    continue
                for err in algorithms_list_is_safe(algorithms_kw.value, algorithm_name_ok):
                    errors.append(f"{site}: {err}")

    return errors, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="Repo root to check (default: cwd).")
    args = parser.parse_args()
    root = os.path.abspath(args.root)

    errors, tool_error = check(root)

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
