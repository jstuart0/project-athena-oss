#!/usr/bin/env python3
"""
Audit env example files against runtime/deploy usage of environment vars.

Cross-references three example files (.env.example, config.env.example,
.env.secrets.example — both active and `# COMMENTED=` forms count as
documented) against:

* Python code under `src/`, `admin/`, `apps/`, parsed via AST:
    `os.getenv('NAME', ...)`, `os.environ.get('NAME', ...)`,
    `os.environ['NAME']`.  Only string-literal names are tracked;
    dynamic names like `os.getenv(var)` are skipped (can't be resolved
    statically).
* Deploy tooling — shell scripts, Makefiles, Dockerfiles, Helm/Kustomize
  YAML, Terraform — scanned with regex for `$NAME` / `${NAME}` refs and
  Kubernetes ConfigMap-style `INDENTED_KEY:` entries.

Two outputs:

* `missing` — read at runtime but not declared in any example file.
* `unused` — declared but never referenced anywhere actionable
  (Python or deploy tooling).  Usually stale doc — but check before
  deleting; the var may be consumed by something the scanner doesn't
  cover yet (e.g. `.tpl` Helm templates, ad-hoc scripts).

Exits non-zero if either list is non-empty so CI / pre-commit can gate
on the drift.

Usage:
    python3 scripts/check-env-example.py
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

# Names that are read by the runtime but are NOT user-configurable — they
# are auto-injected by Kubernetes / the process supervisor / the per-service
# launcher.  Documenting them in .env.example would mislead users.
RUNTIME_INJECTED_NAMES = frozenset({
    "IN_CLUSTER",                # set by entrypoints when running under K8s
    "KUBERNETES_SERVICE_HOST",   # auto-injected by Kubernetes
    "PORT",                      # set per-service by deployment manifests
})

# Roots and globs scanned for non-Python references.  Many env vars are
# consumed by deploy tooling (shell scripts, Helm/Kustomize manifests,
# docker-compose, Dockerfiles) — without scanning those, the audit
# produces false-positive "unused" entries.
NON_PYTHON_GLOBS = (
    "*.sh", "*.bash", "*.zsh", "Makefile", "*.mk",
    "*.yml", "*.yaml",
    "*.tf", "*.tfvars",
    "Dockerfile", "Dockerfile.*", "*.dockerfile",
    "docker-compose*.yml", "docker-compose*.yaml",
)
NON_PYTHON_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "thoughts"}


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
# Commented form: "# NAME=value" or "#NAME=value" — treated as documentation.
# Standalone "# This is prose" comments don't match because they lack the "=".
_COMMENTED_ENV_LINE = re.compile(r"^\s*#\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=")

# References inside shell / YAML / Helm / Dockerfile sources:
#   $NAME  ${NAME}  ${NAME:-default}  $env:NAME (PowerShell — rare here)
# The trailing boundary `(?![A-Z0-9_])` keeps "$ATHENA" from matching
# "$ATHENA_DB_HOST".
_SHELL_VAR_REF = re.compile(
    r"\$\{?\s*([A-Z_][A-Z0-9_]*)\s*[:?\-+}\s]"
    r"|"
    r"\$([A-Z_][A-Z0-9_]+)(?![A-Z0-9_])"
)
# Kubernetes ConfigMap-style env keys: indented "  CAPS_NAME: value".
# The leading indent is what distinguishes a ConfigMap data entry from
# a top-level YAML key (which we'd want to ignore — top-level keys in
# Helm values, Compose, etc. are config schema, not env vars).
_YAML_ENV_KEY = re.compile(r"^[ \t]+([A-Z_][A-Z0-9_]+):\s*[\"']?[^\s#]")


def collect_env_example_names(paths: Iterable[Path]) -> set[str]:
    names: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _ENV_LINE.match(line) or _COMMENTED_ENV_LINE.match(line)
            if m:
                names.add(m.group(1))
    return names


def collect_non_python_refs() -> set[str]:
    """Find $NAME / ${NAME} references in shell, YAML, Makefile, Dockerfile, etc.

    The audit's "unused" list is otherwise full of false positives because
    deploy tooling consumes many vars that Python never sees (REGISTRY,
    NAMESPACE, ATHENA_DOMAIN, …).
    """
    names: set[str] = set()
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in NON_PYTHON_SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if not any(path.match(g) for g in NON_PYTHON_GLOBS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in _SHELL_VAR_REF.finditer(text):
            names.add(m.group(1) or m.group(2))
        # Only inspect ConfigMap-style keys inside files that look like
        # K8s manifests (`kind:` somewhere in the doc).  This keeps the
        # heuristic from latching onto unrelated CAPS_KEY: lines in,
        # say, Compose's "environment:" inline-list mappings.
        if path.suffix in {".yml", ".yaml"} and "\nkind:" in ("\n" + text):
            for line in text.splitlines():
                m = _YAML_ENV_KEY.match(line)
                if m:
                    names.add(m.group(1))
    return names


def main() -> int:
    code_names: set[str] = set()
    for sub in SCAN_ROOTS:
        d = REPO_ROOT / sub
        if d.is_dir():
            code_names |= collect_getenv_names(d)

    code_names -= RUNTIME_INJECTED_NAMES
    deploy_refs = collect_non_python_refs()

    example_paths = [REPO_ROOT / f for f in ENV_EXAMPLE_FILES]
    example_names = collect_env_example_names(example_paths)

    missing = sorted(code_names - example_names)
    # A var is "unused" only if neither Python code nor deploy tooling
    # references it.  Suppress entries that match the example file's own
    # name conventions (the example files reference some vars in their
    # own commentary, which would otherwise self-suppress).
    unused = sorted((example_names - code_names) - deploy_refs)

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
