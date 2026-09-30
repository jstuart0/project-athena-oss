"""Qdrant runs one pod on a ReadWriteOnce volume, so it must roll with
Recreate; admin-backend's memory budget covers the embedder (D16)."""
from __future__ import annotations

from pathlib import Path

import yaml

MANIFESTS = Path(__file__).resolve().parents[2] / "manifests" / "athena-prod"
UNITS = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3}


def _deployment(filename, name):
    for doc in yaml.safe_load_all((MANIFESTS / filename).read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"Deployment {name} not in {filename}")


def _bytes(quantity):
    for suffix, factor in UNITS.items():
        if str(quantity).endswith(suffix):
            return int(str(quantity)[: -len(suffix)]) * factor
    return int(quantity)


def test_qdrant_uses_recreate():
    assert _deployment("qdrant.yaml", "qdrant")["spec"]["strategy"] == {"type": "Recreate"}


def test_admin_backend_memory_budget():
    spec = _deployment("admin-backend.yaml", "athena-admin-backend")["spec"]["template"]["spec"]
    [container] = [c for c in spec["containers"] if c["name"] == "admin-backend"]
    resources = container["resources"]
    assert _bytes(resources["limits"]["memory"]) >= _bytes("1Gi")
    assert _bytes(resources["requests"]["memory"]) >= _bytes("512Mi")
