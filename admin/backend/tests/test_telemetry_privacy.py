"""Nothing household-identifying leaves the install.

Sentinels are seeded as real database rows and env values, the real cycle
runs to a recording transport, and the scan reads the request bytes and the
stored last payload. A positive control proves every sentinel was seeded; a
self-check proves the scanner catches each class it looks for.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    CalendarSource, ComponentModelAssignment, Device, Feature, Guest, IntentMetric, LLMBackend, Memory, RagService,
    SystemSetting, User,
)
from app.services.telemetry import sender
from app.services.telemetry.schema import MODEL_FAMILIES, MODEL_SOURCES, SIZE_BUCKETS, Payload
from tests._telemetry_support import (
    ALL_COMPONENTS, FIXTURES, Recorder, add_backend, add_component, add_service, emitted_leaf_paths, load_fixture,
    settings,
)
from tests.conftest import TestingSessionLocal

HOSTILE_MODELS = (
    "registry.sentinel.example.com/private/m:latest",
    "sentinel-user/m:latest",
    "hf.co/sentinel-user/m",
    "hf.co/google/jays-house-tuned",
    "nas:5000/sentinel/m",
    "jays-house-tuned:latest",
    "llama-jays-house",
    "qwen3:q4_ann_smi_th",
    "qwen3-02139",
    "llama3-5551234567",
    "gpt-4o-mini",
)
SENTINEL_HOST_URL = "http://sentinel-host-7f3a.example.net:11434"
SENTINELS = (
    "sentinel", "7f3a", "10.99.88.77", "zebulon", "sentinel@example.com", "sentinel-cal-token",
    "sentinel-private-svc", "sentinel_component", "sentinel_feature", "sentinel memory text",
    "sentinel query text", "sentinel-room", "sentinel-sat", "10.77.66.55", "sentinel-username",
    "ha-sentinel", "tok-sentinel-9b1c", "jays", "house", "02139", "5551234567", "ann_smi",
)

EXPECTED_PATHS = {
    "schema_version", "installation_id", "event",
    "install.version", "install.release_channel", "install.install_class", "install.provenance",
    "install.platform", "install.arch", "install.deployment",
    "services.core_enabled", "services.core_healthy", "services.rag_enabled", "services.rag_healthy",
    "services.rag_custom", "services.infrastructure_healthy",
    "llm.components[].component", "llm.components[].assignment", "llm.components[].family",
    "llm.components[].size_bucket", "llm.components[].quantized", "llm.components[].source",
    "llm.components[].backend", "llm.components[].locality", "llm.custom_components", "llm.cloud_providers_enabled",
    "features.modules_enabled", "features.flags_enabled",
    "home_assistant.configured",
    "voice.wyoming_devices", "voice.jetson_devices", "voice.livekit_enabled", "voice.jarvis_web_enabled",
    "voice.stt_engines", "voice.tts_engines",
    "guest_mode.enabled", "guest_mode.calendar_source_types", "guest_mode.legacy_ical_configured",
    "memory.memories", "memory.vector_store",
    "usage.queries_per_day_30d", "usage.intent_mix_30d",
    "platform_config.auth_modes", "platform_config.oidc_configured", "platform_config.control_agent_enabled",
    "platform_config.service_control_k8s_enabled",
}

REGEX_CLASSES = {
    "url": re.compile(r"://"),
    "ipv4": re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"),
    "at": re.compile(r"@"),
    "domain": re.compile(r"\.(com|net|org|io|dev|ai|local|lan|internal|arpa)\b"),
    "long_token": re.compile(r"[a-z0-9_-]{33,}"),
    "digit_run": re.compile(r"\d{5,}"),
}
UNSCANNED = {"installation_id", "install.version"}


def _string_leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, sub in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if path == "usage.intent_mix_30d" and isinstance(sub, dict):
                for intent in sub:
                    yield path, intent
                continue
            yield from _string_leaves(sub, path)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item, prefix + ("[]" if item and isinstance(item, dict) else ""))
    elif isinstance(value, str):
        yield prefix, value


def _vocabulary():
    """Text the wire vocabulary itself defines: families, the client
    allowlists, enum values and keys."""
    from app.services.telemetry import schema

    words = set(MODEL_FAMILIES) | {"custom"} | set(SIZE_BUCKETS) | set(MODEL_SOURCES)
    for name in dir(schema):
        if name.startswith("KNOWN_"):
            words.update(getattr(schema, name))
    schema = Payload.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            for key, sub in node.items():
                words.add(key)
                if key == "enum":
                    words.update(str(v) for v in sub)
                walk(sub)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return words


def findings(payload: dict, raw: bytes) -> list:
    """Every privacy finding in one payload: (class, detail)."""
    out = []
    text = raw.decode("utf-8").lower()
    for sentinel in SENTINELS:
        if sentinel.lower() in text:
            out.append(("sentinel", sentinel))
    for path, value in _string_leaves(payload):
        if path in UNSCANNED:
            continue
        for name, pattern in REGEX_CLASSES.items():
            if pattern.search(value.lower()):
                out.append((name, f"{path}={value}"))
    for component in payload["llm"]["components"]:
        if component["family"] not in MODEL_FAMILIES | {"custom"}:
            out.append(("family", component["family"]))
        if component["size_bucket"] not in SIZE_BUCKETS or component["source"] not in MODEL_SOURCES:
            out.append(("enum", json.dumps(component)))
    vocabulary = _vocabulary()
    for model in HOSTILE_MODELS:
        lowered = model.lower()
        for n in range(4, len(lowered) + 1):
            for i in range(len(lowered) - n + 1):
                sub = lowered[i:i + n]
                if sub in text and not any(sub in word for word in vocabulary):
                    out.append(("model_text", sub))
    return out


def _seed(db, monkeypatch):
    user = User(username="sentinel-username", email="owner-sentinel-x@example.com", role="owner",
                created_at=datetime.now(timezone.utc) - timedelta(days=10))
    db.add(user)
    db.flush()
    for component, model in zip(ALL_COMPONENTS, HOSTILE_MODELS):
        add_component(db, component, model)
    add_component(db, "sentinel_component", "qwen3:4b")
    add_backend(db, "sentinel-user/m:latest", "ollama", SENTINEL_HOST_URL)
    db.add(SystemSetting(key="ollama_url", value="http://10.99.88.77:11434", category="llm"))
    db.add(Guest(name="Zebulon Sentinel", email="sentinel@example.com", phone="5551234567"))
    db.add(CalendarSource(name="Sentinel listing", source_type="airbnb",
                          ical_url="https://calendar.example.org/ical?token=sentinel-cal-token", enabled=True))
    add_service(db, "sentinel-private-svc", service_type="rag")
    db.add(Feature(name="sentinel_feature", display_name="Sentinel", category="x", enabled=True))
    db.add(Memory(content="sentinel memory text", scope="global", vector_id="00000000-0000-4000-8000-00000000abcd"))
    for i in range(3):
        db.add(IntentMetric(intent="control", confidence=0.9, raw_query="sentinel query text", room="sentinel-room",
                            created_at=datetime.now(timezone.utc) - timedelta(hours=i + 1)))
    db.add(Device(device_type="wyoming", name="sat", hostname="sentinel-sat.local", ip_address="10.77.66.55"))
    db.commit()
    monkeypatch.setenv("HA_URL", "https://ha-sentinel.example.org")
    monkeypatch.setenv("HA_TOKEN", "tok-sentinel-9b1c")


def _positive_control(db):
    import os

    assert {c.model_name for c in db.query(ComponentModelAssignment)} >= set(HOSTILE_MODELS)
    assert db.query(LLMBackend).filter(LLMBackend.endpoint_url == SENTINEL_HOST_URL).count() == 1
    assert db.query(SystemSetting).filter(SystemSetting.value == "http://10.99.88.77:11434").count() == 1
    assert db.query(Guest).filter(Guest.name == "Zebulon Sentinel", Guest.email == "sentinel@example.com").count() == 1
    assert db.query(CalendarSource).filter(CalendarSource.ical_url.contains("sentinel-cal-token")).count() == 1
    assert db.query(RagService).filter(RagService.name == "sentinel-private-svc").count() == 1
    assert db.query(ComponentModelAssignment).filter(
        ComponentModelAssignment.component_name == "sentinel_component").count() == 1
    assert db.query(Feature).filter(Feature.name == "sentinel_feature", Feature.enabled == True).count() == 1  # noqa: E712
    assert db.query(Memory).filter(Memory.content == "sentinel memory text").count() == 1
    assert db.query(IntentMetric).filter(IntentMetric.raw_query == "sentinel query text",
                                         IntentMetric.room == "sentinel-room").count() == 3
    assert db.query(Device).filter(Device.hostname == "sentinel-sat.local",
                                   Device.ip_address == "10.77.66.55").count() == 1
    assert db.query(User).filter(User.username == "sentinel-username").count() == 1
    assert os.environ["HA_URL"] == "https://ha-sentinel.example.org"
    assert os.environ["HA_TOKEN"] == "tok-sentinel-9b1c"


@pytest.fixture
def sent(telemetry_env, db, monkeypatch):
    _seed(db, monkeypatch)
    _positive_control(db)
    recorder = Recorder()
    monkeypatch.setattr(sender, "TRANSPORT", recorder.transport)

    async def resolver(host):
        return ["10.0.0.9"]

    monkeypatch.setattr(sender, "RESOLVER", resolver)
    assert asyncio.run(sender.run_cycle(force=True)) == "sent"
    assert len(recorder.requests) == 1
    return recorder.requests[0], settings(TestingSessionLocal)


def test_request_body_carries_nothing_identifying(sent):
    request, state = sent
    payload = json.loads(request.content)
    assert findings(payload, request.content) == []


def test_stored_last_payload_carries_nothing_identifying(sent):
    request, state = sent
    stored = state["telemetry.last_payload"].encode()
    assert stored == request.content
    assert findings(json.loads(stored), stored) == []


def test_hostile_models_reduce_to_enums(sent):
    request, state = sent
    components = {c["component"]: c for c in json.loads(request.content)["llm"]["components"]}
    assert "sentinel_component" not in components
    assert json.loads(request.content)["llm"]["custom_components"] == "1"
    for component in ALL_COMPONENTS:
        assert components[component]["family"] in MODEL_FAMILIES | {"custom"}


def test_install_key_never_in_the_request_or_payload(sent):
    request, state = sent
    install_key = state["telemetry.install_key"]
    raw_key = base64.urlsafe_b64decode(install_key + "=" * (-len(install_key) % 4))
    wire = b"".join(k + b":" + v for k, v in request.headers.raw) + request.content
    assert install_key.encode() not in wire and raw_key not in wire
    header = request.headers["x-athena-install-key"]
    assert header.encode() not in request.content
    assert header not in state["telemetry.last_payload"]


def test_scanner_self_check():
    base = load_fixture("valid-v1-full.json")
    assert findings(base, json.dumps(base).encode()) == []
    injections = {
        "url": "http://x",
        "ipv4": "10.1.2.3",
        "at": "a@b",
        "domain": "box.lan",
        "long_token": "a" * 33,
        "digit_run": "12345",
    }
    hits = 0
    for name, value in injections.items():
        mutated = json.loads(json.dumps(base))
        mutated["features"]["flags_enabled"] = [value]
        if name in {c for c, _ in findings(mutated, json.dumps(mutated).encode())}:
            hits += 1
    mutated = json.loads(json.dumps(base))
    mutated["features"]["flags_enabled"] = ["zebulon"]
    if "sentinel" in {c for c, _ in findings(mutated, json.dumps(mutated).encode())}:
        hits += 1
    assert hits == len(injections) + 1 == 7


def test_emitted_paths_are_exactly_the_allowlist():
    paths = set(emitted_leaf_paths(load_fixture("valid-v1-full.json")))
    assert len(EXPECTED_PATHS) >= 35
    assert "llm.components[].locality" in EXPECTED_PATHS
    assert paths == EXPECTED_PATHS
    assert (FIXTURES / "valid-v1-full.json").exists()
