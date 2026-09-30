"""The admin-backend image bakes the embedding model: downloaded at one
pinned snapshot revision, hash-verified and load-checked offline at build,
offline at runtime (D11). The revision lives only in the Dockerfile's
EMBEDDING_MODEL_REVISION; these tests read it from there."""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "admin" / "backend"
DOCKERFILE = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
MANIFEST = BACKEND / "embedding-model.sha256"
MODULE = BACKEND / "app" / "services" / "memory_vectors.py"
GATE = BACKEND / "scripts" / "embed_rss_gate.py"
MODEL_FILES = ["config.json", "model.onnx", "special_tokens_map.json", "tokenizer_config.json", "tokenizer.json"]


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


def _env_index(key):
    return _index(lambda l: l.startswith("ENV ") and any(
        pair.split("=", 1)[0] == key for pair in l[len("ENV "):].split()))


def _revision():
    [value] = re.findall(r"\bEMBEDDING_MODEL_REVISION=(\S+)", DOCKERFILE)
    return value


def _module_constant(name):
    for node in ast.parse(MODULE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


def _module_model():
    return _module_constant("EMBEDDING_MODEL")


def test_cache_path_and_revision_before_bake_and_offline_after():
    bake = _index(lambda l: l.startswith("RUN") and "TextEmbedding" in l)
    assert "FASTEMBED_CACHE_PATH=/opt/fastembed_cache" in _instructions()[_env_index("FASTEMBED_CACHE_PATH")]
    assert _env_index("FASTEMBED_CACHE_PATH") < bake
    assert _env_index("EMBEDDING_MODEL_REVISION") < bake
    assert _index(lambda l: l == "ENV HF_HUB_OFFLINE=1") > bake


def test_revision_is_one_full_commit_sha():
    assert re.fullmatch(r"[0-9a-f]{40}", _revision())
    assert DOCKERFILE.count(_revision()) == 1


def test_bake_downloads_the_pinned_revision_not_head():
    run = _bake_run()
    assert "snapshot_download(" in run
    assert "revision=os.environ['EMBEDDING_MODEL_REVISION']" in run
    assert "cache_dir=os.environ['FASTEMBED_CACHE_PATH']" in run
    [patterns] = re.findall(r"allow_patterns=(\[[^\]]*\])", run)
    assert sorted(ast.literal_eval(patterns)) == sorted(MODEL_FILES)
    # fastembed's own download fetches upstream HEAD and ignores a revision.
    assert "lazy_load" not in run


def test_bake_run_is_strict_pinned_and_verified_in_order():
    run = _bake_run()
    assert run.startswith("RUN set -eu;")
    steps = [
        "snapshot_download(",
        'set -- "$model_dir"/snapshots/*/;',
        '[ "$#" -eq 1 ];',
        '[ "$(basename "$1")" = "$EMBEDDING_MODEL_REVISION" ];',
        "(cd \"$1\" && sha256sum -c /opt/embedding-model.sha256);",
        "printf '%s' \"$EMBEDDING_MODEL_REVISION\" > \"$model_dir/refs/main\";",
        "HF_HUB_OFFLINE=1 python -c",
        "chmod -R a+rX /opt/fastembed_cache",
    ]
    positions = [run.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "model_dir=/opt/fastembed_cache/models--qdrant--all-MiniLM-L6-v2-onnx;" in run
    assert _index(lambda l: l.startswith("COPY embedding-model.sha256")) < _index(
        lambda l: l.startswith("RUN") and "TextEmbedding" in l)


def test_bake_loads_and_embeds_offline():
    offline = _bake_run().split("HF_HUB_OFFLINE=1 python -c", 1)[1].split('";', 1)[0]
    assert "local_files_only=True" in offline
    assert ".embed(" in offline
    assert f"assert len(v) == {_module_constant('EMBEDDING_DIM')}, len(v)" in offline


def test_bake_precedes_app_copy():
    assert _index(lambda l: l.startswith("RUN") and "TextEmbedding" in l) < _index(lambda l: l.startswith("COPY app/"))


def test_baked_model_is_the_module_model():
    models = re.findall(r"'(sentence-transformers/[^']+)'", _bake_run())
    assert len(models) == 2
    assert set(models) == {_module_model()} == {"sentence-transformers/all-MiniLM-L6-v2"}


def test_manifest_lists_the_five_model_files():
    lines = [l for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    entries = [re.fullmatch(r"([0-9a-f]{64})  (\S+)", l) for l in lines]
    assert all(entries), lines
    assert sorted(m.group(2) for m in entries) == sorted(MODEL_FILES)


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


def test_gate_embeds_mixed_length_batches():
    # A batch mixing a text over 130 tokens with short ones is the case a
    # fixed-128 padding tokenizer breaks; the gate must build and require it.
    source = GATE.read_text(encoding="utf-8")
    assert "batch_texts = [_batch_text(i) for i in range(BATCH_TEXTS)]" in source
    assert re.search(r"^LONG_BATCH_TOKENS = 130$", source, re.M)
    assert re.search(r"^ok = dims_ok and mixed_ok and not errors", source.replace("    ok =", "ok ="), re.M)
