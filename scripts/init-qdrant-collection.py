#!/usr/bin/env python3
"""
Idempotent initializer for the ``athena_memories`` Qdrant collection.

WHY THIS SCRIPT EXISTS
----------------------
The production collection ``athena_memories`` is created out-of-band — nothing
in the application code creates it on startup.  ``admin/backend/app/routes/memories.py``
wraps every Qdrant operation in a try/except and falls back gracefully:

* On *write* (POST /api/memories): the exception is caught, a warning is logged,
  and the memory is stored in PostgreSQL only (Qdrant vector storage is silently
  skipped).  No 500 error, but semantic search returns empty results.
* On *search* (POST /api/memories/search): returns
  ``{"results": [], "qdrant_available": True, "error": "..."}``.

**Run this script before serving production traffic** or after any Qdrant
pod restart that wiped collection state (e.g. the PVC was deleted).  It is
safe to run any number of times — it never mutates an existing collection.

USAGE
-----
    # Default (connects to http://localhost:6333)
    python3 scripts/init-qdrant-collection.py

    # Target a remote / in-cluster Qdrant
    QDRANT_URL=http://qdrant.athena-prod.svc.cluster.local:6333 \\
        python3 scripts/init-qdrant-collection.py

    # Create collection with scalar int8 quantization (reduces RAM ~4x at
    # slight recall cost; requires server >= 1.18 for TurboQuant support)
    python3 scripts/init-qdrant-collection.py --quantize

COLLECTION PARAMETERS
---------------------
* size=384    : all-MiniLM-L6-v2 embedding dimension (fastembed in memories.py)
* COSINE      : conventional metric for MiniLM-L6-v2 sentence embeddings.
                An existing production collection's metric takes precedence —
                this script never overwrites a collection that already exists.
"""
from __future__ import annotations

import argparse
import os
import sys

COLLECTION_NAME = "athena_memories"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dimension
DEFAULT_QDRANT_URL = "http://localhost:6333"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Idempotently create the athena_memories Qdrant collection."
    )
    parser.add_argument(
        "--quantize",
        action="store_true",
        default=False,
        help=(
            "Enable scalar int8 quantization on the new collection "
            "(reduces memory ~4x at slight recall cost). "
            "No effect if the collection already exists. "
            "Requires Qdrant server >= 1.18 for TurboQuant support."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    qdrant_url = os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL)

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
    except ImportError:
        print("ERROR: qdrant-client is not installed. Run: pip install qdrant-client", file=sys.stderr)
        return 1

    print(f"Connecting to Qdrant at {qdrant_url} ...")
    try:
        client = QdrantClient(url=qdrant_url, timeout=10)
    except Exception as exc:
        print(f"ERROR: Could not create Qdrant client: {exc}", file=sys.stderr)
        return 1

    # Check whether the collection already exists.
    try:
        existing = {c.name for c in client.get_collections().collections}
    except Exception as exc:
        print(f"ERROR: Could not list collections (is Qdrant reachable?): {exc}", file=sys.stderr)
        return 1

    if COLLECTION_NAME in existing:
        print(
            f"Collection '{COLLECTION_NAME}' already exists — nothing to do. "
            "This script never mutates an existing collection."
        )
        return 0

    # Build quantization config when --quantize is requested.
    quantization_config = None
    if args.quantize:
        try:
            from qdrant_client.models import ScalarQuantization, ScalarQuantizationConfig, ScalarType
            quantization_config = ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=True,
                )
            )
            print("Scalar int8 quantization enabled (requires server >= 1.18).")
        except ImportError as exc:
            print(
                f"WARNING: Could not import quantization models ({exc}). "
                "Creating collection without quantization.",
                file=sys.stderr,
            )

    print(f"Creating collection '{COLLECTION_NAME}' (size={VECTOR_SIZE}, distance=COSINE) ...")
    try:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
            quantization_config=quantization_config,
        )
    except Exception as exc:
        print(f"ERROR: Failed to create collection: {exc}", file=sys.stderr)
        return 1

    print(f"Collection '{COLLECTION_NAME}' created successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
