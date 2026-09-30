"""Peak-memory gate for the memory embedder, run inside the admin-backend
image (it bakes the model and runs offline):

    docker run --rm --network none --memory 1g <image> python scripts/embed_rss_gate.py

With the app's modules imported, it embeds 32 maximum-length texts (8192
characters, truncated to EMBED_MAX_CHARS and then to the model's 256
tokens) in EMBED_BATCH chunks while 8 threads each embed one more, and
fails when peak RSS exceeds the budget. The gating run is CI's native
x86_64 runner; an emulated run (QEMU) is informational only.
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


def _peak_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _text(seed: int) -> str:
    words = " ".join(f"memory{seed} word{i} about the house and its routines" for i in range(400))
    return words[:TEXT_CHARS]


def main() -> int:
    import app.routes.memories  # noqa: F401  (measure with the app's modules loaded)
    from app.services import memory_vectors as mv

    batch_texts = [_text(i) for i in range(BATCH_TEXTS)]
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
    batch = mv.embed(batch_texts)
    for thread in threads:
        thread.join()

    tokenizer = mv._get_embedder().model.tokenizer
    tokens = len(tokenizer.encode(batch_texts[0][: mv.EMBED_MAX_CHARS]).ids)
    vectors = list(batch) + list(singles.values())
    dims_ok = len(vectors) == BATCH_TEXTS + CONCURRENT_SINGLES and all(len(v) == mv.EMBEDDING_DIM for v in vectors)
    peak = round(_peak_rss_mib(), 1)
    ok = dims_ok and not errors and peak <= PEAK_RSS_BUDGET_MIB
    print(json.dumps({
        "peak_rss_mib": peak,
        "budget_mib": PEAK_RSS_BUDGET_MIB,
        "machine": platform.machine(),
        "truncated_tokens": tokens,
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
