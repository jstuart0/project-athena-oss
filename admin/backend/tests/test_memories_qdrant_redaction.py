"""codex r3 diff-review Low (2026-09-28): the Qdrant client's
qdrant_client_initialized info log printed the raw configured QDRANT_URL
verbatim -- a URL shaped http://user:pass@host:port would put the
credential straight into structured log output. Now redacted via
app.utils.url_validators.redact_url_userinfo().

The client is built in app.services.memory_vectors (the one module that
owns Qdrant access). QdrantClient(...) makes no network call at
construction when check_compatibility=False, which the module always
passes: the default starts a version-check thread that hung the combined
memory test run.
"""
from __future__ import annotations

from app.services import memory_vectors


def test_client_never_logs_url_userinfo(monkeypatch, capsys):
    memory_vectors.set_client_for_tests(None)
    monkeypatch.setattr(memory_vectors, "QDRANT_URL", "http://qadmin:qsecret@localhost:6333")

    capsys.readouterr()
    client = memory_vectors._get_client()
    out = capsys.readouterr().out

    assert client is not None
    assert "qdrant_client_initialized" in out, out
    assert "http://localhost:6333" in out, out
    assert "qadmin:qsecret" not in out, out
    assert "qsecret" not in out, out


def test_client_built_without_compatibility_check(monkeypatch):
    import qdrant_client

    captured = {}

    class _Spy:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    memory_vectors.set_client_for_tests(None)
    monkeypatch.setattr(qdrant_client, "QdrantClient", _Spy)
    memory_vectors._get_client()
    assert captured.get("check_compatibility") is False
    assert captured.get("url") == memory_vectors.QDRANT_URL
    assert captured.get("timeout") == 10
