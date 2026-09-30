"""Peak-memory gate for the memory embedder, run inside the admin-backend
image (it bakes the model and runs offline):

    docker run --rm --network none --memory 1g <image> python scripts/embed_rss_gate.py

With the app's modules imported, it embeds 32 texts in EMBED_BATCH chunks
while 8 threads each embed one maximum-length text (8192 characters,
truncated to EMBED_MAX_CHARS and then to the model's 256 tokens), and fails
when peak RSS exceeds the budget. The batch mixes lengths so every chunk
holds a maximum-length text, a mid-length one over 130 tokens and a short
one: a tokenizer that pads to a fixed length shorter than it truncates
(the model's revision before d139546) raises on such a batch, and that
fails the gate too. The gating run is CI's native x86_64 runner; an
emulated run (QEMU) is informational only.
"""
from __future__ import annotations

import json
import os
import platform
import resource
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

PEAK_RSS_BUDGET_MIB = 700
TEXT_CHARS = 8192
BATCH_TEXTS = 32
CONCURRENT_SINGLES = 8
MID_WORDS = 180
LONG_BATCH_TOKENS = 130
SHORT_BATCH_TOKENS = 32


def _peak_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _text(seed: int) -> str:
    words = " ".join(f"memory{seed} word{i} about the house and its routines" for i in range(400))
    return words[:TEXT_CHARS]


def _batch_text(index: int) -> str:
    kind = index % 3
    if kind == 0:
        return _text(index)
    if kind == 1:
        return " ".join(["word"] * MID_WORDS)
    return f"short memory {index}"


def _tokens(tokenizer, text: str) -> int:
    return sum(tokenizer.encode(text).attention_mask)


def main() -> int:
    import app.routes.memories  # noqa: F401  (measure with the app's modules loaded)
    from app.services import memory_vectors as mv

    batch_texts = [_batch_text(i) for i in range(BATCH_TEXTS)]
    single_texts = [_text(1000 + i) for i in range(CONCURRENT_SINGLES)]
    singles: dict = {}
    errors: list = []

    def _single(index: int) -> None:
        try:
            singles[index] = mv.embed([single_texts[index]])[0]
        except Exception as exc:  # reported, and fails the gate
            errors.append(repr(exc))

    threads = [threading.Thread(target=_single, args=(i,)) for i in range(CONCURRENT_SINGLES)]
    for thread in threads:
        thread.start()
    try:
        batch = mv.embed(batch_texts)
    except Exception as exc:  # reported, and fails the gate
        batch = []
        errors.append(repr(exc))
    for thread in threads:
        thread.join()

    tokenizer = mv._get_embedder().model.tokenizer
    tokens = _tokens(tokenizer, batch_texts[0][: mv.EMBED_MAX_CHARS])
    chunk_tokens = []
    for start in range(0, BATCH_TEXTS, mv.EMBED_BATCH):
        counts = [_tokens(tokenizer, t[: mv.EMBED_MAX_CHARS]) for t in batch_texts[start:start + mv.EMBED_BATCH]]
        chunk_tokens.append([min(counts), max(counts)])
    mixed_ok = all(low <= SHORT_BATCH_TOKENS and high > LONG_BATCH_TOKENS for low, high in chunk_tokens)
    vectors = list(batch) + list(singles.values())
    dims_ok = len(vectors) == BATCH_TEXTS + CONCURRENT_SINGLES and all(len(v) == mv.EMBEDDING_DIM for v in vectors)
    peak = round(_peak_rss_mib(), 1)
    ok = dims_ok and mixed_ok and not errors and peak <= PEAK_RSS_BUDGET_MIB
    print(json.dumps({
        "peak_rss_mib": peak,
        "budget_mib": PEAK_RSS_BUDGET_MIB,
        "machine": platform.machine(),
        "model_revision": os.environ.get("EMBEDDING_MODEL_REVISION"),
        "truncated_tokens": tokens,
        "chunk_min_max_tokens": chunk_tokens,
        "mixed_ok": mixed_ok,
        "embed_batch": mv.EMBED_BATCH,
        "vectors": len(vectors),
        "dims_ok": dims_ok,
        "errors": errors,
        "ok": ok,
    }))
    sys.stdout.flush()
    return 0 if ok else 1


if __name__ == "__main__":
    # os._exit: skip interpreter teardown (onnxruntime's thread pool can
    # abort during it on some platforms), so the exit code is the verdict.
    os._exit(main())
