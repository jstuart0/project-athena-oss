#!/usr/bin/env python3
"""
Audit env example files against os.getenv / os.environ usage in code.

Scans `src/`, `admin/`, and `apps/` for every reference to an environment
variable whose name is a string literal:

* `os.getenv('NAME', ...)`
* `os.environ.get('NAME', ...)`
* `os.environ['NAME']` (subscript)

Cross-references against `.env.example`, `config.env.example`, and
`.env.secrets.example` (this repo uses all three) to surface drift:

* `missing` — variable is read at runtime but not declared in any example
  file. Users won't know to set it until something fails.
* `unused` — variable sits in an example file but no Python code ever reads
  it. Usually a safe-to-remove stale doc entry.

Exits non-zero if either list is non-empty so CI can catch regressions.

Usage:
    python3 scripts/check-env-example.py

Only string-literal names are tracked; `os.getenv(var_name)` (non-string)
is skipped silently because we can't resolve the name statically.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("src", "admin", "apps")
ENV_EXAMPLE_FILES = (".env.example", "config.env.example", ".env.secrets.example")


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def collect_getenv_names(root: Path) -> set[str]:
    names: set[str] = set()
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            # os.environ["NAME"] subscript reads (and writes — we can't
            # distinguish reliably without dataflow, but a name in the
            # subscript indicates the var is referenced either way).
            if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                key = node.slice
                # Py3.9+: subscript.slice is the expression directly.
                literal = _string_literal(key)
                if literal is not None:
                    names.add(literal)
                continue

            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            # os.getenv(...) or os.environ.get(...) — both accept a name arg.
            match_os_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            match_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_environ(func.value)
            )
            if not (match_os_getenv or match_environ_get):
                continue
            literal = _string_literal(node.args[0])
            if literal is not None:
                names.add(literal)
    return names


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=")


def collect_env_example_names(paths: Iterable[Path]) -> set[str]:
    names: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _ENV_LINE.match(line)
            if m:
                names.add(m.group(1))
    return names


def main() -> int:
    code_names: set[str] = set()
    for sub in SCAN_ROOTS:
        d = REPO_ROOT / sub
        if d.is_dir():
            code_names |= collect_getenv_names(d)

    example_paths = [REPO_ROOT / f for f in ENV_EXAMPLE_FILES]
    example_names = collect_env_example_names(example_paths)

    missing = sorted(code_names - example_names)
    unused = sorted(example_names - code_names)

    print("=== .env.example drift audit ===")
    print(f"scan roots:        {', '.join(SCAN_ROOTS)}")
    print(f"example files:     {', '.join(f for f in ENV_EXAMPLE_FILES)}")
    print(f"code env reads:    {len(code_names)}")
    print(f"documented in env: {len(example_names)}")
    print()

    if missing:
        print(f"Missing from example files ({len(missing)}):")
        for name in missing:
            print(f"  - {name}")
        print()
    else:
        print("Missing from example files: none")

    if unused:
        print(f"In example files but never referenced ({len(unused)}):")
        for name in unused:
            print(f"  - {name}")
    else:
        print("Unused entries in example files: none")

    return 1 if (missing or unused) else 0


if __name__ == "__main__":
    sys.exit(main())
