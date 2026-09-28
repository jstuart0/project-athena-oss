"""ATHENA-108 follow-up 2: every src/rag/*/main.py call site that actually
registers with the service registry must pass a service_name that
shared.service_registry.to_rag_registry_name()/to_rag_host_label() normalise
to an EXISTING admin/backend/app/database.py::OSS_SERVICE_REGISTRY seed row.

price_compare and site_scraper 422'd because their SERVICE_NAME constants
("price-compare"/"site-scraper") carried a hyphen the seeded row's
name/host never had ("pricecompare"/"sitescraper") -- neither `name` nor
the `host_label` fallback matched an existing row, so the admin upsert
treated it as a brand-new row with no endpoint_url and rejected it.

Static AST scan, no imports of either module under test: resolves the
first positional argument of every register_service()/startup_service()
call site (a literal string or a module-level SERVICE_NAME assignment) and
statically parses OSS_SERVICE_REGISTRY out of admin/backend/app/database.py
(never imports/executes it -- that module creates a SQLAlchemy engine at
import time), so a future RAG connector with the same hyphen/underscore
mismatch fails CI instead of 422ing in production.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_RAG_ROOT = REPO_ROOT / "src" / "rag"
_DATABASE_PY = REPO_ROOT / "admin" / "backend" / "app" / "database.py"
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shared.service_registry import to_rag_host_label  # noqa: E402

_REGISTER_CALL_NAMES = {"register_service", "startup_service"}


def _call_name(node: ast.AST) -> str | None:
    """Returns the callee name if `node` is a Call to register_service()/
    startup_service() (however it's invoked -- bare, awaited, or via
    attribute access), else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id in _REGISTER_CALL_NAMES:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _REGISTER_CALL_NAMES:
        return func.attr
    return None


def _module_level_string_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            out[target.id] = node.value.value
    return out


def _resolve_registration_service_names(source: str) -> list[str]:
    """Returns the resolved service_name argument for every
    register_service()/startup_service() call in `source` whose first
    positional argument is a literal string or a module-level string
    constant (e.g. SERVICE_NAME). A call whose argument isn't statically
    resolvable (a computed expression) is skipped, not silently ignored --
    see test_every_registration_arg_is_statically_resolvable below."""
    tree = ast.parse(source)
    constants = _module_level_string_constants(tree)
    names: list[str] = []
    for node in ast.walk(tree):
        if _call_name(node) is None or not node.args:  # type: ignore[union-attr]
            continue
        arg = node.args[0]  # type: ignore[union-attr]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.append(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in constants:
            names.append(constants[arg.id])
    return names


def _rag_files_with_registration_calls() -> dict[Path, list[str]]:
    out: dict[Path, list[str]] = {}
    for path in sorted(_RAG_ROOT.glob("*/main.py")):
        names = _resolve_registration_service_names(path.read_text())
        if names:
            out[path] = names
    return out


def _rag_files_with_any_registration_call_text() -> list[Path]:
    """Every file mentioning register_service/startup_service at all
    (including a statically-unresolvable call, or a bare import). Used only
    to detect a call whose argument this scanner failed to resolve."""
    out = []
    for path in sorted(_RAG_ROOT.glob("*/main.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if _call_name(node) is not None:
                out.append(path)
                break
    return out


def _seed_names() -> set[str]:
    """OSS_SERVICE_REGISTRY's seed `name` values, parsed statically -- the
    ground truth this campaign's registration derivation must match."""
    tree = ast.parse(_DATABASE_PY.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "OSS_SERVICE_REGISTRY"):
            continue
        if not isinstance(node.value, ast.List):
            continue
        names = set()
        for elt in node.value.elts:
            if isinstance(elt, ast.Tuple) and elt.elts:
                first = elt.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
        return names
    raise AssertionError(f"OSS_SERVICE_REGISTRY assignment not found in {_DATABASE_PY}")


def test_seed_extraction_population_floor_met():
    """Named member + floor on the parser itself, so a change to
    database.py's shape that silently empties _seed_names() fails loudly
    here rather than making test_every_rag_service_name_normalises_to_a_
    seeded_row() vacuously pass."""
    seeds = _seed_names()
    assert len(seeds) >= 20, f"expected >=20 OSS_SERVICE_REGISTRY seed rows, found {len(seeds)}: {sorted(seeds)}"
    assert "weather" in seeds
    assert "pricecompare" in seeds
    assert "sitescraper" in seeds


def test_registration_call_population_floor_met():
    files = _rag_files_with_registration_calls()
    assert len(files) >= 8, (
        f"expected register_service/startup_service call sites resolvable in "
        f">=8 src/rag/*/main.py files, found {len(files)}: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in files]}"
    )


def test_registration_call_named_members_present():
    files = {str(p.relative_to(REPO_ROOT)) for p in _rag_files_with_registration_calls()}
    assert "src/rag/weather/main.py" in files
    assert "src/rag/price_compare/main.py" in files
    assert "src/rag/site_scraper/main.py" in files


def test_every_registration_arg_is_statically_resolvable():
    """Every file that calls register_service()/startup_service() at all
    must have had its argument resolved by _resolve_registration_service_names()
    -- otherwise test_every_rag_service_name_normalises_to_a_seeded_row()
    would silently skip a real call site instead of checking it."""
    resolved = set(_rag_files_with_registration_calls())
    mentioned = set(_rag_files_with_any_registration_call_text())
    unresolved = mentioned - resolved
    assert unresolved == set(), (
        f"{sorted(str(p.relative_to(REPO_ROOT)) for p in unresolved)} call "
        "register_service()/startup_service() with an argument this scanner "
        "could not statically resolve to a string (not a literal or a "
        "module-level SERVICE_NAME constant) -- extend "
        "_resolve_registration_service_names() rather than silently skipping it"
    )


def test_every_rag_service_name_normalises_to_a_seeded_row():
    seeds = _seed_names()
    failures: list[str] = []
    for path, names in _rag_files_with_registration_calls().items():
        rel = path.relative_to(REPO_ROOT)
        for name in names:
            host_label = to_rag_host_label(name)
            matched = any(host_label == f"athena-rag-{seed}" for seed in seeds)
            if not matched:
                failures.append(
                    f"{rel}: registration service_name {name!r} normalises to "
                    f"host_label {host_label!r}, which matches no "
                    f"OSS_SERVICE_REGISTRY seed row's athena-rag-<name> host "
                    f"(seed names: {sorted(seeds)})"
                )
    assert failures == [], "\n".join(failures)


def test_scanner_catches_a_synthetic_hyphen_mismatch():
    """Positive control: proves the scanner has teeth against the exact
    shape price_compare/site_scraper shipped with, not just that the
    (now-fixed) real files happen to pass."""
    bad_source = '''
SERVICE_NAME = "totally-unseeded-connector"

async def lifespan(app):
    await startup_service(SERVICE_NAME, SERVICE_PORT, "x")
'''
    names = _resolve_registration_service_names(bad_source)
    assert names == ["totally-unseeded-connector"]
    seeds = _seed_names()
    host_label = to_rag_host_label(names[0])
    assert not any(host_label == f"athena-rag-{seed}" for seed in seeds)
