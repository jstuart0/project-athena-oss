"""The progress-callback URL admin-backend hands the Control Agent comes
from configuration.

The Control Agent runs on a host of its own and posts download progress
back to ``<callback_url>/internal/{id}/progress`` with the service key. The
callback base used to be the literal ``http://localhost:8080``, which on a
separate agent host is that host's own loopback. It is now built from
``CONTROL_AGENT_CALLBACK_BASE_URL``; the loopback value is only the
fallback when that is unset, and the fallback says so once.

Driven through the two routes that start a download, with the Control
Agent call recorded.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import structlog

from shared.config import _clear_cache_for_tests

from app.routes import model_downloads

ROUTES_FILE = Path(model_downloads.__file__)
BODY = {"repo_id": "zz-org/zz-model", "filename": "zz.gguf"}
FALLBACK = "http://localhost:8080/api/model-downloads"


@pytest.fixture
def agent(monkeypatch):
    """Records every Control Agent call; `agent.start_ok` decides whether a
    download start succeeds."""

    class _Agent:
        calls: list = []
        start_ok = True

    recorder = _Agent()
    recorder.calls = []

    async def call_control_agent(method, endpoint, json_data=None, params=None, timeout=30.0):
        recorder.calls.append((method, endpoint, json_data))
        if endpoint == "/huggingface/download":
            return (True, {"job_id": "j1"}) if recorder.start_ok else (False, {"error": "zz"})
        return True, []

    async def no_broadcast(*args, **kwargs):
        return None

    monkeypatch.setattr(model_downloads, "call_control_agent", call_control_agent)
    monkeypatch.setattr(model_downloads, "broadcast_model_download_event", no_broadcast)
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    monkeypatch.setattr(model_downloads, "_callback_base_unset_reported", False, raising=False)
    _clear_cache_for_tests()
    yield recorder
    _clear_cache_for_tests()


def _set_base(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CONTROL_AGENT_CALLBACK_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("CONTROL_AGENT_CALLBACK_BASE_URL", value)
    _clear_cache_for_tests()


def _callback_urls(agent):
    return [body["callback_url"] for _method, endpoint, body in agent.calls if endpoint == "/huggingface/download"]


def _start_then_retry(owner_client, agent):
    """One failed start, then a retry of it: both routes that hand the
    Control Agent a callback URL."""
    agent.start_ok = False
    created = owner_client.post("/api/model-downloads", json=BODY)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "failed"
    agent.start_ok = True
    retried = owner_client.post(f"/api/model-downloads/{created.json()['id']}/retry")
    assert retried.status_code == 200, retried.text
    urls = _callback_urls(agent)
    assert len(urls) == 2, "the start route and the retry route each sent one"
    return urls


@pytest.mark.parametrize("configured,expected", [
    ("https://admin.example.org", "https://admin.example.org/api/model-downloads"),
    ("https://admin.example.org/", "https://admin.example.org/api/model-downloads"),
    ("http://athena-admin-backend.zz-ns.svc.cluster.local:8080",
     "http://athena-admin-backend.zz-ns.svc.cluster.local:8080/api/model-downloads"),
], ids=["plain", "trailing_slash", "with_port"])
def test_both_routes_build_the_callback_from_the_configured_base(configured, expected, owner_client, agent, monkeypatch):
    _set_base(monkeypatch, configured)
    with structlog.testing.capture_logs() as logs:
        urls = _start_then_retry(owner_client, agent)
    assert urls == [expected, expected]
    assert [r for r in logs if r.get("event") == "control_agent_callback_base_url_unset"] == []


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_unset_base_falls_back_to_loopback_and_says_so_once(value, owner_client, agent, monkeypatch):
    _set_base(monkeypatch, value)
    with structlog.testing.capture_logs() as logs:
        urls = _start_then_retry(owner_client, agent)
    assert urls == [FALLBACK, FALLBACK]
    warnings = [r for r in logs if r.get("event") == "control_agent_callback_base_url_unset"]
    assert len(warnings) == 1, logs
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["variable"] == "CONTROL_AGENT_CALLBACK_BASE_URL"


def test_no_route_carries_a_callback_host_literal():
    """Closed world: `callback_url` is assigned only from the one helper,
    and no function but the helper's fallback names a host."""
    tree = ast.parse(ROUTES_FILE.read_text())
    assigned = [
        ast.unparse(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "callback_url" for t in node.targets)
    ]
    assert assigned == ["_progress_callback_base()", "_progress_callback_base()"]
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "localhost:8080" in node.value
    ]
    assert literals == ["http://localhost:8080"], "only the documented fallback constant"
