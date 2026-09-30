"""The admin-backend image bakes the embedding model: pinned to one
snapshot revision, hash-verified at build, offline at runtime (D11)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "admin" / "backend"
DOCKERFILE = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
MANIFEST = BACKEND / "embedding-model.sha256"
MODULE = BACKEND / "app" / "services" / "memory_vectors.py"
REVISION = "8f518e882455312b086101e60691f5e6e2f05c3c"


def _instructions():
    """Dockerfile instructions with line continuations joined."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def _index(predicate):
    for i, line in enumerate(_instructions()):
        if predicate(line):
            return i
    raise AssertionError("instruction not found")


def _bake_run():
    return next(line for line in _instructions() if line.startswith("RUN") and "TextEmbedding" in line)


def _module_model():
    for node in ast.parse(MODULE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "EMBEDDING_MODEL":
            return ast.literal_eval(node.value)
    raise AssertionError("EMBEDDING_MODEL not found")


def test_cache_path_before_bake_and_offline_after():
    bake = _index(lambda l: l.startswith("RUN") and "TextEmbedding" in l)
    assert _index(lambda l: l == "ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache") < bake
    assert _index(lambda l: l == "ENV HF_HUB_OFFLINE=1") > bake


def test_bake_run_is_strict_pinned_and_verified():
    run = _bake_run()
    assert "set -eu" in run
    assert "set -- /opt/fastembed_cache/models--qdrant--all-MiniLM-L6-v2-onnx/snapshots/*/" in run
    assert '[ "$#" -eq 1 ]' in run
    assert f'= "{REVISION}" ]' in run
    assert "sha256sum -c /opt/embedding-model.sha256" in run
    assert _index(lambda l: l.startswith("COPY embedding-model.sha256")) < _index(
        lambda l: l.startswith("RUN") and "TextEmbedding" in l)


def test_bake_precedes_app_copy():
    assert _index(lambda l: l.startswith("RUN") and "TextEmbedding" in l) < _index(lambda l: l.startswith("COPY app/"))


def test_baked_model_is_the_module_model():
    [model] = re.findall(r"TextEmbedding\('([^']+)'", _bake_run())
    assert model == _module_model() == "sentence-transformers/all-MiniLM-L6-v2"


def test_manifest_lists_the_five_model_files():
    lines = [l for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    entries = [re.fullmatch(r"([0-9a-f]{64})  (\S+)", l) for l in lines]
    assert all(entries), lines
    assert sorted(m.group(2) for m in entries) == sorted([
        "config.json", "model.onnx", "special_tokens_map.json", "tokenizer_config.json", "tokenizer.json",
    ])


def test_embedder_is_offline_and_single_threaded():
    calls = [n for n in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8")))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "TextEmbedding"]
    assert len(calls) == 1
    kwargs = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kwargs["local_files_only"], ast.Constant) and kwargs["local_files_only"].value is True
    assert isinstance(kwargs["threads"], ast.Constant) and kwargs["threads"].value == 1


def test_gate_script_ships_in_image():
    assert (BACKEND / "scripts" / "embed_rss_gate.py").is_file()
    assert "COPY scripts/ ./scripts/" in _instructions()
