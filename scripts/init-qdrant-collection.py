#!/usr/bin/env python3
"""
Idempotent initializer for the ``athena_memories`` Qdrant collection.

YOU USUALLY DON'T NEED THIS
---------------------------
admin-backend creates and validates the collection itself
(``admin/backend/app/services/memory_vectors.py``): on startup and on
every revalidation it creates the collection when it's absent, records the
embedding model in the collection's metadata, and refuses to use a
collection with the wrong shape or embedding model. PostgreSQL is the
source of truth for memories. A memory saved while the vector store is
down is kept and marked pending, and admin-backend embeds pending memories
automatically once the store is back — including after the collection
itself was lost (for example a Qdrant storage change).

Use this script only to pre-create the collection with non-default options
(``--quantize``) before admin-backend first starts. It never mutates an
existing collection, and it creates the collection exactly as admin-backend
would: same name, size, distance and model metadata (a parity test,
``tests/unit/test_init_qdrant_script_parity.py``, keeps the two in step).

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
* size=384    : sentence-transformers/all-MiniLM-L6-v2 embedding dimension
* COSINE      : the metric admin-backend validates
* metadata    : {embedding_model, embedding_dim, distance, payload_schema}.
                Qdrant servers older than 1.16 accept and silently drop
                collection metadata; admin-backend then checks the
                per-point model stamp instead.
"""
from __future__ import annotations

import argparse
import os
import sys

COLLECTION_NAME = "athena_memories"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dimension
DISTANCE = "Cosine"
PAYLOAD_SCHEMA = 1
DEFAULT_QDRANT_URL = "http://localhost:6333"


def collection_metadata() -> dict:
    return {
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": VECTOR_SIZE,
        "distance": DISTANCE,
        "payload_schema": PAYLOAD_SCHEMA,
    }


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

    print("Connecting to Qdrant (QDRANT_URL) ...")
    try:
        client = QdrantClient(url=qdrant_url, timeout=10, check_compatibility=False)
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
            metadata=collection_metadata(),
        )
    except Exception as exc:
        print(f"ERROR: Failed to create collection: {exc}", file=sys.stderr)
        return 1

    print(f"Collection '{COLLECTION_NAME}' created successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
