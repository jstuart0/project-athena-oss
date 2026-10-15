"""Drift guard: household preferences are read only through the household readers.

A preference that admin-backend owns (home city, Amtrak station, transit feeds,
lamp exclusions, ...) resolves admin value, then environment, then default, in
one place per process: ``shared.household``, jarvis-web's ``household.py`` or
admin-backend's ``services/household_settings.py``. This scan fails when any
other module in ``src``, ``apps`` or ``admin/backend/app`` reads one of those
settings straight from the environment or ``AthenaConfig``, and when a section
has a resolver but no production process that configures its reader.

Receiver-agnostic on purpose: it looks at attribute *names* and string
*literals*, not at what the object is called, so ``c = get_config();
c.default_amtrak_station`` and ``os.environ["GATEWAY_FALLBACK_MODEL"]`` are
caught as well as ``get_config().default_city``.

Stdlib only: this runs on the unit-min CI requirements.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "apps", "admin/backend/app")

# Not a runtime image: a syntax error at :134 (an unclosed call), so it parses on no Python version.
UNPARSEABLE = frozenset({"src/jetson/athena_lite_llm.py"})

FIELD_NAMES = frozenset({
    "default_city", "default_amtrak_station", "transit_region_name", "transit_gtfs_feeds",
    "transit_static_services", "community_events_sources", "gateway_fallback_model",
    "sms_default_country_code", "ha_room_light_exclude_entities", "ha_bed_warmer_entities",
})
SERVICE_ENV_NAMES = frozenset(n.upper() for n in FIELD_NAMES) | {"DEFAULT_STATE"}
JARVIS_ENV_NAMES = frozenset({
    "DEFAULT_ROOM", "CLIMATE_ENTITY", "OVEN_ENTITY_ID", "FRIDGE_ENTITY_ID", "FREEZER_ENTITY_ID",
    "STOVE_COOK_MODE_SENSOR_ID", "STOVE_DISPLAY_TEMP_SENSOR_ID", "STOVE_TIMER_SENSOR_ID",
    "FRIDGE_DOOR_SENSOR_ID", "MIN_TEMP", "MAX_TEMP",
})
ENV_NAMES = SERVICE_ENV_NAMES | JARVIS_ENV_NAMES

# The modules that own the resolution. Whole files.
READERS = (
    "src/shared/config.py",
    "src/shared/household.py",
    "src/shared/household_settings.py",
    "admin/backend/app/services/household_settings.py",
    "apps/jarvis-web/backend/household.py",
    "apps/jarvis-web/backend/household_settings.py",
)

# (path, kind, name) -> reason. kind is "attr" or "literal". Every entry is a
# spot that mentions a setting without reading it for a decision.
LABEL = "a label naming the env var in an error or hint, not a read"
ALLOWLIST: Dict[Tuple[str, str, str], str] = {
    ("src/rag/transportation/main.py", "literal", "TRANSIT_GTFS_FEEDS"): LABEL + " (the item's `where` text)",
    ("src/rag/transportation/main.py", "literal", "TRANSIT_STATIC_SERVICES"): LABEL + " (the item's `where` text)",
    ("src/rag/community_events/main.py", "literal", "COMMUNITY_EVENTS_SOURCES"): LABEL + " (the item's `where` text)",
    ("src/rag/community_events/main.py", "attr", "default_city"): "EventSource.default_city, a per-source field of the scraper config, not the household city",
}

# Sections a resolver serves that no process configures an HTTP reader for,
# with the reason and a source needle the test pins in the file named.
UNCONFIGURED_SECTIONS: Dict[str, Tuple[str, str, str]] = {
    "sms": (
        "admin-backend reads the row straight from the database (it is the writer)",
        "admin/backend/app/services/household_settings.py",
        'load(db, "sms")',
    ),
}


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _python_files() -> List[Path]:
    files: List[Path] = []
    for root in SCAN_ROOTS:
        files.extend(
            p for p in (REPO_ROOT / root).rglob("*.py")
            if "__pycache__" not in p.parts and p.relative_to(REPO_ROOT).as_posix() not in UNPARSEABLE
        )
    return sorted(files)


def reads_in(tree: ast.AST) -> Set[Tuple[str, str]]:
    """``{(kind, name)}`` for every attribute named like a setting field and
    every string literal equal to a setting's env name."""
    found: Set[Tuple[str, str]] = set()
    resolver_calls = {
        id(node.func) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "household"
    }  # household.gateway_fallback_model() is the resolver, which shares the field's name
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FIELD_NAMES and id(node) not in resolver_calls:
            found.add(("attr", node.attr))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ENV_NAMES:
            found.add(("literal", node.value))
    return found


def violations(files: Iterable[Path], root: Path = REPO_ROOT, allowlist=ALLOWLIST,
               readers=READERS) -> List[Tuple[str, str, str]]:
    out = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if rel in readers:
            continue
        for kind, name in sorted(reads_in(_parse(path))):
            if (rel, kind, name) not in allowlist:
                out.append((rel, kind, name))
    return out


def test_no_module_outside_the_readers_reads_a_household_setting_from_the_environment():
    found = violations(_python_files())
    assert not found, (
        "read these through shared.household (or jarvis-web's household.py), not the environment: "
        + "; ".join(f"{p} {k} {n}" for p, k, n in found)
    )


def test_every_allowlist_entry_is_still_needed():
    seen = set()
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        seen.update((rel, kind, name) for kind, name in reads_in(_parse(path)))
    stale = sorted(set(ALLOWLIST) - seen)
    assert not stale, f"remove from ALLOWLIST: {stale}"


def test_the_scan_reaches_every_root_and_finds_the_readers():
    files = {p.relative_to(REPO_ROOT).as_posix() for p in _python_files()}
    assert len(files) > 300
    for root in SCAN_ROOTS:
        assert any(f.startswith(root + "/") for f in files), root
    for reader in READERS:
        if reader != "apps/jarvis-web/backend/household_settings.py":  # copied in at image build
            assert reader in files, reader


def test_the_planted_negatives_are_flagged(tmp_path):
    cases = {
        "attr_via_alias": "c = get_config()\nx = c.default_amtrak_station\n",
        "environ_subscript": 'import os\nx = os.environ["GATEWAY_FALLBACK_MODEL"]\n',
        "getenv_single_quotes": "import os\nx = os.getenv('TRANSIT_GTFS_FEEDS')\n",
        "settings_attr": "x = settings.sms_default_country_code\n",
        "state": 'import os\nx = os.getenv("DEFAULT_STATE", "")\n',
        "jarvis_env": 'import os\nx = os.environ.get("OVEN_ENTITY_ID")\n',
    }
    for label, source in cases.items():
        assert reads_in(ast.parse(source)), label


def test_a_planted_file_outside_the_readers_fails_the_scan(tmp_path):
    planted = tmp_path / "src" / "orchestrator" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("def f(c):\n    return c.default_city\n", encoding="utf-8")
    assert violations([planted], root=tmp_path) == [("src/orchestrator/planted.py", "attr", "default_city")]
    reader = tmp_path / "src" / "shared" / "household.py"
    reader.parent.mkdir(parents=True)
    reader.write_text("def f(c):\n    return c.default_city\n", encoding="utf-8")
    assert violations([reader], root=tmp_path) == []


def test_a_call_to_the_household_resolver_is_not_a_read_but_the_config_field_is():
    assert not reads_in(ast.parse("x = household.gateway_fallback_model()\n"))
    assert reads_in(ast.parse("x = household.gateway_fallback_model\n"))
    assert reads_in(ast.parse("x = cfg.gateway_fallback_model()\n"))


def test_prose_that_merely_mentions_a_name_is_not_a_read():
    assert not reads_in(ast.parse('"""Falls back to DEFAULT_CITY when unset."""\nx = "set TRANSIT_GTFS_FEEDS"\n'))


def test_the_reader_names_default_state_and_every_setting():
    resolver_files = ("src/shared/household.py", "src/shared/household_settings.py",
                      "admin/backend/app/services/household_settings.py")
    mentioned: Set[Tuple[str, str]] = set()
    for rel in resolver_files:
        mentioned |= reads_in(_parse(REPO_ROOT / rel))
    assert ("literal", "DEFAULT_STATE") in reads_in(_parse(REPO_ROOT / "src/shared/household.py"))
    names = {n for _k, n in mentioned}
    for field_name in FIELD_NAMES:
        assert field_name in names or field_name.upper() in names, f"no resolver reads {field_name}"
    for env_name in JARVIS_ENV_NAMES:
        assert env_name in names, f"jarvis-web resolver does not read {env_name}"


# ---------------------------------------------------------------------------
# Every section with a resolver is configured by a production process
# ---------------------------------------------------------------------------

def registry_sections(source: str) -> Set[str]:
    """Names passed first to ``_spec(...)`` in the section registry."""
    return {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_spec"
        and node.args and isinstance(node.args[0], ast.Constant)
    }


def resolver_sections(source: str) -> Set[str]:
    """Keys of the ``RESOLVERS`` mapping in ``shared.household``."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "RESOLVERS" for t in targets):
                dicts = [n for n in ast.walk(node.value) if isinstance(n, ast.Dict)]
                return {k.value for d in dicts for k in d.keys if isinstance(k, ast.Constant)}
    raise AssertionError("RESOLVERS not found")


def _string_tuple(node: ast.AST) -> Set[str] | None:
    if isinstance(node, ast.Tuple | ast.List) and all(isinstance(e, ast.Constant) for e in node.elts):
        return {e.value for e in node.elts}
    return None


def configured_sections(source: str) -> Tuple[Set[str], int]:
    """Sections named by ``configure(sections=...)`` calls in one file, and how
    many such calls there are. A name is resolved to a module-level tuple."""
    tree = ast.parse(source)
    constants: Dict[str, Set[str]] = {}
    for node in tree.body:
        value, targets = None, []
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        names = _string_tuple(value) if value is not None else None
        if names is not None:
            constants.update({t.id: names for t in targets if isinstance(t, ast.Name)})
    sections: Set[str] = set()
    calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name != "configure":
            continue
        for kw in node.keywords:
            if kw.arg != "sections":
                continue
            calls += 1
            resolved = _string_tuple(kw.value)
            if resolved is None and isinstance(kw.value, ast.Name):
                resolved = constants.get(kw.value.id)
            assert resolved is not None, "configure(sections=...) must be a literal tuple or a module-level constant"
            sections |= resolved
    return sections, calls


def _production_configured() -> Tuple[Set[str], int]:
    sections: Set[str] = set()
    total = 0
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in READERS:
            continue
        text = path.read_text(encoding="utf-8")
        if "configure" not in text:
            continue
        found, calls = configured_sections(text)
        sections |= found
        total += calls
    return sections, total


def _jarvis_web_sections() -> Set[str]:
    """jarvis-web's reader takes an injected fetch, not a section list; its
    module-level ``SECTIONS`` and the ``configure(fetch=...)`` call in main.py
    are what make it a configured process."""
    tree = _parse(REPO_ROOT / "apps/jarvis-web/backend/household.py")
    sections: Set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SECTIONS" for t in node.targets):
            sections |= _string_tuple(node.value) or set()
    main = (REPO_ROOT / "apps/jarvis-web/backend/main.py").read_text(encoding="utf-8")
    return sections if "household.configure(fetch=" in main else set()


def test_every_resolver_section_is_configured_by_a_production_process():
    household = (REPO_ROOT / "src/shared/household.py").read_text(encoding="utf-8")
    registry = registry_sections((REPO_ROOT / "src/shared/household_settings.py").read_text(encoding="utf-8"))
    resolvers = resolver_sections(household)
    assert resolvers == registry, "RESOLVERS and the section registry disagree"
    assert len(resolvers) >= 9

    configured, calls = _production_configured()
    assert calls >= 6, "the scan should see the orchestrator, gateway and four RAG configure calls"
    covered = configured | _jarvis_web_sections()

    for section, (_reason, rel, needle) in UNCONFIGURED_SECTIONS.items():
        assert needle in (REPO_ROOT / rel).read_text(encoding="utf-8"), f"{section}: {rel} no longer has {needle}"
    missing = sorted(resolvers - covered - set(UNCONFIGURED_SECTIONS))
    assert not missing, (
        f"{missing} have a resolver but no production configure(sections=...) names them; add the section "
        "to the consuming process's tuple in the same change (a test's install_for_test does not count)"
    )
    assert configured <= registry, f"configure() names unknown sections: {sorted(configured - registry)}"


def test_the_named_processes_configure_their_sections():
    expected = {
        "src/orchestrator/main.py": {"location", "lights", "bed_warmer"},
        "src/gateway/main.py": {"gateway", "location"},
        "src/rag/directions/main.py": {"location"},
        "src/rag/transportation/main.py": {"transit"},
        "src/rag/community_events/main.py": {"community_events"},
        "src/rag/amtrak/main.py": {"amtrak"},
    }
    for rel, sections in expected.items():
        found, calls = configured_sections((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert calls == 1 and found == sections, (rel, found, calls)
    assert _jarvis_web_sections() == {"jarvis_web"}


_WARMING_LIFESPANS = (
    "src/gateway/main.py", "src/rag/directions/main.py", "src/rag/transportation/main.py",
    "src/rag/community_events/main.py", "src/rag/amtrak/main.py",
)


def unbounded_first_loads(source: str) -> int:
    """Calls of ``<x>.current()`` awaited on a household reader in one file."""
    return len(re.findall(r"await\s+household_reader\.current\(\)", source))


def test_the_gateway_and_rag_lifespans_bound_their_first_load():
    for rel in _WARMING_LIFESPANS:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "await household.warm()" in text, f"{rel} must warm the reader through the bounded helper"
        assert unbounded_first_loads(text) == 0, f"{rel} awaits the reader without a total bound"


def test_a_planted_unbounded_first_load_is_caught():
    assert unbounded_first_loads("    await household_reader.current()\n") == 1
    assert unbounded_first_loads("    await household.warm()\n") == 0


def test_a_planted_resolver_without_a_configured_section_is_caught():
    source = 'from shared import household\nhousehold.configure(sections=("location",))\n'
    configured, calls = configured_sections(source)
    assert calls == 1
    assert "lights" not in configured  # so a resolver for "lights" would be reported missing
    renamed = 'S = ("lights",)\nhousehold.configure(sections=S)\n'
    assert configured_sections(renamed) == ({"lights"}, 1)


def test_a_configure_call_whose_sections_cannot_be_read_statically_is_refused():
    try:
        configured_sections("household.configure(sections=compute())\n")
    except AssertionError:
        return
    raise AssertionError("an opaque sections argument must fail the scan")


def test_admin_backends_household_service_imports_only_the_pure_module():
    """admin-backend is the writer: its household_settings service reads the
    database and imports only the pure module (D17)."""
    tree = _parse(REPO_ROOT / "admin/backend/app/services/household_settings.py")
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert "shared.household" not in imported and "shared.admin_config" not in imported
    assert "shared.household_settings" in imported


def test_admin_backend_app_never_imports_the_http_reader():
    offenders = []
    for path in (REPO_ROOT / "admin/backend/app").rglob("*.py"):
        tree = _parse(path)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            if any(m == "shared.household" or m.startswith("shared.household.") for m in modules):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, offenders


def test_the_env_example_audit_sees_the_names_the_household_registry_reads():
    """scripts/check-env-example.py can't see names handed to a pure module as an
    ``env`` mapping; without its household collector the jarvis-web variables
    would all read as documented-but-unused."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_env_example_hh", REPO_ROOT / "scripts" / "check-env-example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = module.collect_household_env_names()
    assert {
        "DEFAULT_ROOM", "CLIMATE_ENTITY", "OVEN_ENTITY_ID", "FRIDGE_ENTITY_ID", "FREEZER_ENTITY_ID",
        "STOVE_COOK_MODE_SENSOR_ID", "STOVE_DISPLAY_TEMP_SENSOR_ID", "STOVE_TIMER_SENSOR_ID",
        "FRIDGE_DOOR_SENSOR_ID", "MIN_TEMP", "MAX_TEMP",
    } <= names
    example_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in names:
        assert name in example_text, f"{name} is read by the registry but not documented in .env.example"
