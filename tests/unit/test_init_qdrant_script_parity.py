"""scripts/init-qdrant-collection.py creates the collection exactly as
admin-backend's memory_vectors module would, and no longer claims the app
never creates it."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "init-qdrant-collection.py"
MODULE = REPO_ROOT / "admin" / "backend" / "app" / "services" / "memory_vectors.py"


def _module_constants():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _script():
    spec = importlib.util.spec_from_file_location("init_qdrant_collection", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constants_and_metadata_match_the_module():
    consts = _module_constants()
    script = _script()
    assert script.COLLECTION_NAME == consts["COLLECTION_NAME"] == "athena_memories"
    assert script.VECTOR_SIZE == consts["EMBEDDING_DIM"] == 384
    assert script.collection_metadata() == {
        "embedding_model": consts["EMBEDDING_MODEL"],
        "embedding_dim": consts["EMBEDDING_DIM"],
        "distance": consts["DISTANCE"],
        "payload_schema": consts["PAYLOAD_SCHEMA"],
    }


def test_create_passes_metadata_and_skips_compat_check():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""): n
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    create_kwargs = {k.arg for k in calls["create_collection"].keywords}
    assert "metadata" in create_kwargs
    client_kwargs = {k.arg: k.value for k in calls["QdrantClient"].keywords}
    assert isinstance(client_kwargs.get("check_compatibility"), ast.Constant)
    assert client_kwargs["check_compatibility"].value is False


def test_docstring_describes_self_creation():
    doc = ast.get_docstring(ast.parse(SCRIPT.read_text(encoding="utf-8")))
    assert "stored in PostgreSQL only" not in doc
    assert "nothing in the application code creates it" not in doc
    assert "creates and validates the collection itself" in doc
    assert "pending" in doc
