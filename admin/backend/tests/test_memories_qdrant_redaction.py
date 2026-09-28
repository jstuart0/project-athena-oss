"""codex r3 diff-review Low (2026-09-28): app.routes.memories.get_qdrant()'s
qdrant_client_initialized info log printed the raw configured QDRANT_URL
verbatim -- a URL shaped http://user:pass@host:port would put the
credential straight into structured log output. Now redacted via
app.utils.url_validators.redact_url_userinfo().

QdrantClient(...) does not make a network call at construction time (only
on first real operation), so this exercises the real get_qdrant() code
path with no mocking of the client itself.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

pytest.importorskip("qdrant_client")

import app.routes.memories as memories_module


@pytest.fixture(autouse=True)
def _reset_qdrant_singleton(monkeypatch):
    monkeypatch.setattr(memories_module, "_qdrant_client", None)
    yield
    monkeypatch.setattr(memories_module, "_qdrant_client", None)


def test_get_qdrant_never_logs_url_userinfo(monkeypatch, capsys):
    monkeypatch.setattr(memories_module, "QDRANT_URL", "http://qadmin:qsecret@localhost:6333")

    capsys.readouterr()
    client = memories_module.get_qdrant()
    out = capsys.readouterr().out

    assert client is not None
    assert "qdrant_client_initialized" in out, out
    assert "http://localhost:6333" in out, out
    assert "qadmin:qsecret" not in out, out
    assert "qsecret" not in out, out
