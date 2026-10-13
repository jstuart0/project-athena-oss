"""Authorization never depends on the room modules: mode_permission, the
jarvis-web caller_auth and the semantic cache import none of them, and the
room modules import only what their contracts allow."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = {
    "orchestrator.room_context", "orchestrator.room_choice", "orchestrator.room_directory",
    "shared.ha_registry", "room_context", "room_choice", "room_directory", "ha_registry",
}
AUTHORIZATION_FILES = (
    "src/orchestrator/mode_permission.py",
    "apps/jarvis-web/backend/caller_auth.py",
    "src/orchestrator/semantic_cache.py",
)


def imported_names(source: str):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            names.add(module.lstrip("."))
            names.update(f"{node.module}.{a.name}" if node.module else a.name for a in node.names)
            names.update(a.name for a in node.names)
    return names


def offending(source: str):
    return sorted(imported_names(source) & FORBIDDEN)


def test_the_authorization_modules_import_none_of_the_room_modules():
    for rel in AUTHORIZATION_FILES:
        assert offending((ROOT / rel).read_text(encoding="utf-8")) == [], rel


def test_a_planted_import_is_caught():
    assert offending("from orchestrator import room_context\n")
    assert offending("import shared.ha_registry\n") == ["shared.ha_registry"]
    assert "shared.ha_registry" in offending("from shared.ha_registry import room_key\n")
    assert offending("from .room_choice import ask\n")
    assert offending("import os\n") == []


def test_room_choice_imports_only_what_its_contract_allows():
    names = imported_names((ROOT / "src/orchestrator/room_choice.py").read_text())
    internal = {n for n in names if n.startswith(("orchestrator", "shared"))}
    allowed = ("orchestrator.room_context", "orchestrator.mode_permission", "orchestrator.access_target")
    assert all(n == "orchestrator" or n.startswith(allowed) for n in internal), internal
    assert "orchestrator.main" not in names


def test_the_room_modules_never_import_main():
    for rel in ("room_context", "room_choice", "room_directory"):
        names = imported_names((ROOT / f"src/orchestrator/{rel}.py").read_text())
        assert not any(n == "orchestrator.main" or n.endswith(".main") for n in names), rel
