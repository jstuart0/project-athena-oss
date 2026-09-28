"""ATHENA-108 follow-up 2: every src/rag/*/main.py call site that actually
registers with the service registry must pass a service_name that
shared.service_registry.to_rag_registry_name()/to_rag_host_label() normalise
to an EXISTING admin/backend/app/database.py::OSS_SERVICE_REGISTRY seed row.

price_compare and site_scraper 422'd because their SERVICE_NAME constants
("price-compare"/"site-scraper") carried a hyphen the seeded row's
name/host never had ("pricecompare"/"sitescraper") -- neither `name` nor
the `host_label` fallback matched an existing row, so the admin upsert
treated it as a brand-new row with no endpoint_url and rejected it.

Static AST scan, no imports of admin/backend: resolves the first positional
argument of every register_service()/startup_service() call site (a literal
string or a module-level SERVICE_NAME assignment) and statically parses
OSS_SERVICE_REGISTRY out of admin/backend/app/database.py (never imports/
executes it -- that module creates a SQLAlchemy engine at import time), so a
future RAG connector with the same hyphen/underscore mismatch fails CI
instead of 422ing in production.

codex follow-up (2026-09-28) on 4db4236 tightened two things:
  - the "is every call site's argument statically resolvable" guard now
    tracks (path, lineno) per call site rather than diffing at file
    granularity -- a computed argument sitting next to a resolvable literal
    in the same file no longer slips through unflagged.
  - normalisation can alias two distinct connectors onto one registry row
    (e.g. a hypothetical "site-scraper" and "sitescraper" both -> the same
    "sitescraper" base). Two new tests assert no collision exists, both
    across real call sites and across the seed list itself.
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

from shared.service_registry import _normalize_rag_base, to_rag_host_label  # noqa: E402

_REGISTER_CALL_NAMES = {"register_service", "startup_service"}


class CallSite:
    """One register_service()/startup_service() call site. `name` is the
    statically-resolved first positional argument, or None when it couldn't
    be resolved (a computed expression)."""

    __slots__ = ("lineno", "name")

    def __init__(self, lineno: int, name: str | None):
        self.lineno = lineno
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"CallSite(lineno={self.lineno}, name={self.name!r})"


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


def _registration_call_sites(source: str) -> list[CallSite]:
    """Every register_service()/startup_service() call in `source`, one
    CallSite per call with its line number and resolved argument (or None
    when the first positional argument is neither a literal string nor a
    module-level string constant like SERVICE_NAME)."""
    tree = ast.parse(source)
    constants = _module_level_string_constants(tree)
    sites: list[CallSite] = []
    for node in ast.walk(tree):
        if _call_name(node) is None:
            continue
        assert isinstance(node, ast.Call)
        if not node.args:
            sites.append(CallSite(node.lineno, None))
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            sites.append(CallSite(node.lineno, arg.value))
        elif isinstance(arg, ast.Name) and arg.id in constants:
            sites.append(CallSite(node.lineno, constants[arg.id]))
        else:
            sites.append(CallSite(node.lineno, None))
    return sites


def _all_rag_call_sites() -> dict[Path, list[CallSite]]:
    out: dict[Path, list[CallSite]] = {}
    for path in sorted(_RAG_ROOT.glob("*/main.py")):
        sites = _registration_call_sites(path.read_text())
        if sites:
            out[path] = sites
    return out


def _rag_files_with_registration_calls() -> dict[Path, list[str]]:
    """Path -> resolved service_name args only (unresolved call sites
    dropped) -- what the seed-match and collision checks below iterate."""
    out: dict[Path, list[str]] = {}
    for path, sites in _all_rag_call_sites().items():
        names = [s.name for s in sites if s.name is not None]
        if names:
            out[path] = names
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
    """Every register_service()/startup_service() call site must have had
    its argument resolved -- tracked per (path, lineno), not per file, so a
    computed argument next to a resolvable literal in the SAME file still
    fails instead of being masked by the file's other, resolvable call
    site. Otherwise test_every_rag_service_name_normalises_to_a_seeded_row()
    would silently skip that specific call instead of checking it."""
    unresolved: list[str] = []
    for path, sites in _all_rag_call_sites().items():
        rel = path.relative_to(REPO_ROOT)
        for site in sites:
            if site.name is None:
                unresolved.append(f"{rel}:{site.lineno}")
    assert unresolved == [], (
        f"{unresolved} call register_service()/startup_service() with an "
        "argument this scanner could not statically resolve to a string "
        "(not a literal or a module-level SERVICE_NAME constant) -- extend "
        "_registration_call_sites() rather than silently skipping it"
    )


def test_scanner_flags_a_computed_argument_next_to_a_resolvable_literal():
    """Positive control for the per-(path, lineno) tracking above: a file
    with one resolvable call site and one computed-argument call site must
    flag the second, not be masked by the first resolving fine."""
    mixed_source = '''
SERVICE_NAME = "weather"

async def lifespan(app):
    await startup_service(SERVICE_NAME, SERVICE_PORT, "x")
    await startup_service(f"{SERVICE_NAME}-extra", SERVICE_PORT, "y")
'''
    sites = _registration_call_sites(mixed_source)
    assert len(sites) == 2
    resolved = [s for s in sites if s.name is not None]
    unresolved = [s for s in sites if s.name is None]
    assert len(resolved) == 1 and resolved[0].name == "weather"
    assert len(unresolved) == 1, "the f-string argument must be flagged unresolved, not silently dropped"


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


# ---------------------------------------------------------------------------
# codex follow-up (2026-09-28, Medium): normalisation can alias two distinct
# connectors onto the same registry row (e.g. "site-scraper" and
# "sitescraper" both -> base "sitescraper"). A collision here means one of
# the two connectors silently never gets its own row -- worse than the
# original 422, because it succeeds while clobbering the other's row.
# ---------------------------------------------------------------------------

def test_no_seed_name_base_collisions():
    """No two OSS_SERVICE_REGISTRY seed names may normalise to the same
    base -- that would mean two seeded rows are indistinguishable to every
    RAG connector's self-registration, and only one could ever be reached."""
    by_base: dict[str, list[str]] = {}
    for name in _seed_names():
        by_base.setdefault(_normalize_rag_base(name), []).append(name)
    failures = [
        f"base {base!r} is produced by multiple seed names: {sorted(names)}"
        for base, names in by_base.items()
        if len(names) > 1
    ]
    assert failures == [], "\n".join(failures)


def test_no_registration_call_site_base_collisions():
    """No two distinct service_name strings actually posted by RAG
    connectors' register_service()/startup_service() calls may normalise to
    the same base -- that would mean the two connectors' self-registration
    upserts race for a single registry row."""
    by_base: dict[str, dict[str, list[Path]]] = {}
    for path, names in _rag_files_with_registration_calls().items():
        for name in names:
            base = _normalize_rag_base(name)
            by_base.setdefault(base, {}).setdefault(name, []).append(path)

    failures: list[str] = []
    for base, names_to_paths in by_base.items():
        if len(names_to_paths) <= 1:
            continue
        detail = ", ".join(
            f"{name!r} ({sorted(str(p.relative_to(REPO_ROOT)) for p in paths)})"
            for name, paths in sorted(names_to_paths.items())
        )
        failures.append(f"base {base!r} is produced by multiple distinct service names: {detail}")
    assert failures == [], "\n".join(failures)


def test_scanner_catches_a_synthetic_base_collision():
    """Positive control: proves the collision detector has teeth against
    the exact shape the codex follow-up named -- "site-scraper" and
    "sitescraper" as two distinct connector names that alias to one base."""
    by_base: dict[str, dict[str, list[Path]]] = {}
    synthetic = {
        Path("src/rag/site_scraper/main.py"): ["site-scraper"],
        Path("src/rag/synthetic_sitescraper/main.py"): ["sitescraper"],
    }
    for path, names in synthetic.items():
        for name in names:
            base = _normalize_rag_base(name)
            by_base.setdefault(base, {}).setdefault(name, []).append(path)

    collisions = {base: names for base, names in by_base.items() if len(names) > 1}
    assert collisions == {"sitescraper": {"site-scraper": [Path("src/rag/site_scraper/main.py")],
                                           "sitescraper": [Path("src/rag/synthetic_sitescraper/main.py")]}}


def test_scanner_catches_a_synthetic_hyphen_mismatch():
    """Positive control: proves the seed-match scanner has teeth against
    the exact shape price_compare/site_scraper shipped with, not just that
    the (now-fixed) real files happen to pass."""
    bad_source = '''
SERVICE_NAME = "totally-unseeded-connector"

async def lifespan(app):
    await startup_service(SERVICE_NAME, SERVICE_PORT, "x")
'''
    sites = _registration_call_sites(bad_source)
    assert [s.name for s in sites] == ["totally-unseeded-connector"]
    seeds = _seed_names()
    host_label = to_rag_host_label(sites[0].name)
    assert not any(host_label == f"athena-rag-{seed}" for seed in seeds)
