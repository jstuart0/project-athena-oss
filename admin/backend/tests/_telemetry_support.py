"""Shared helpers for the telemetry tests (not a test module itself)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
FIXTURES = BACKEND / "tests" / "fixtures" / "telemetry"
SCHEMA_FILE = REPO / "docs" / "telemetry" / "payload-v1.schema.json"

# Paths whose value is a map (open-vocabulary keys): the map itself is the
# leaf, its keys are data.
MAP_LEAVES = frozenset({"usage.intent_mix_30d"})


def loc_to_path(loc: Tuple[Any, ...]) -> str:
    """A pydantic error location as the shared wire path: dots between keys,
    ``[i]`` for list indexes; the ``[key]`` marker of a map-key error is
    dropped (the path names the offending key)."""
    out = ""
    for part in loc:
        if part == "[key]":
            continue
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def error_paths(exc) -> set:
    return {loc_to_path(tuple(e["loc"])) for e in exc.errors()}


def resolve_ref(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        name = node["$ref"].split("/")[-1]
        node = schema["$defs"][name]
    return node


def _non_null(schema: dict, node: dict) -> dict:
    node = resolve_ref(schema, node)
    if "anyOf" in node:
        options = [resolve_ref(schema, o) for o in node["anyOf"] if o.get("type") != "null"]
        if len(options) == 1:
            return options[0]
    return node


def schema_leaf_paths(schema: dict) -> List[str]:
    """Leaf paths of the JSON Schema: objects with properties recurse, arrays
    of objects recurse as ``[]``, everything else (scalars, scalar lists,
    maps) is a leaf."""
    out: List[str] = []

    def walk(node: dict, prefix: str) -> None:
        node = _non_null(schema, node)
        if "properties" in node:
            for key, sub in node["properties"].items():
                walk(sub, f"{prefix}.{key}" if prefix else key)
            return
        if node.get("type") == "array":
            items = _non_null(schema, node.get("items", {}))
            if "properties" in items:
                walk(items, prefix + "[]")
                return
        out.append(prefix)

    walk(schema, "")
    return sorted(out)


def emitted_leaf_paths(payload: dict) -> List[str]:
    out = set()

    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict) and prefix not in MAP_LEAVES:
            for key, sub in value.items():
                walk(sub, f"{prefix}.{key}" if prefix else key)
            return
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for item in value:
                walk(item, prefix + "[]")
            return
        out.add(prefix)

    walk(payload, "")
    return sorted(out)


def iter_object_nodes(instance: Any, prefix: str = "") -> Iterator[Tuple[str, dict]]:
    """Every object node of a payload instance (maps excluded), as
    (path, dict). List items get ``[i]`` indexes."""
    if isinstance(instance, dict) and prefix not in MAP_LEAVES:
        yield prefix, instance
        for key, sub in instance.items():
            yield from iter_object_nodes(sub, f"{prefix}.{key}" if prefix else key)
    elif isinstance(instance, list):
        for i, sub in enumerate(instance):
            yield from iter_object_nodes(sub, f"{prefix}[{i}]")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Canonical seeded install (golden fixture, collect and privacy tests)
# ---------------------------------------------------------------------------

ALL_COMPONENTS = (
    "conversation_summarizer", "fact_check_validation", "intent_classifier", "intent_discovery",
    "response_synthesis", "response_validator_primary", "response_validator_secondary", "smart_home_control",
    "tool_calling_complex", "tool_calling_simple", "tool_calling_super_complex",
)
DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
GOLDEN_ID = "3f1c9b2e-7d4a-4c1e-9a6b-2f8e5d7c1a90"


def canonical_config():
    from types import SimpleNamespace

    return SimpleNamespace(
        ollama_url="http://ollama.athena.svc.cluster.local:11434",
        oidc_issuer="https://idp.example.org/application/o/athena/",
        control_agent_enabled=False,
        service_control_k8s_enabled=True,
    )


def canonical_collect_kwargs(now):
    return dict(
        now=now,
        env={"KUBERNETES_SERVICE_HOST": "10.96.0.1", "HA_URL": "http://ha.example.lan:8123", "HA_TOKEN": "t"},
        system="Linux",
        machine="x86_64",
        exists=lambda path: False,
        config=canonical_config(),
        module_enabled=lambda module_id: module_id in {"home_assistant", "guest_mode", "jarvis_web"},
        vector_state=lambda: "ready",
        default_model=DEFAULT_MODEL,
        resolver=None,
    )


def add_component(db, name, model, *, enabled=True, backend_type="ollama"):
    from app.models import ComponentModelAssignment

    db.add(ComponentModelAssignment(component_name=name, display_name=name, category="orchestrator",
                                    model_name=model, backend_type=backend_type, enabled=enabled))


def add_backend(db, model, backend_type, endpoint_url, *, enabled=True):
    from app.models import LLMBackend

    db.add(LLMBackend(model_name=model, backend_type=backend_type, endpoint_url=endpoint_url, enabled=enabled))


def add_service(db, name, *, service_type=None, host=None, enabled=True, health="healthy"):
    from app.models import RagService

    db.add(RagService(name=name, display_name=name, service_type=service_type, host=host or name,
                      port=8000, enabled=enabled, health_status=health))


def seed_canonical(db, now):
    """A realistic install: every table collect_facts reads has rows, and the
    intent mix is over the 100-query floor."""
    from datetime import timedelta

    from app.models import (
        CalendarSource, CloudLLMProvider, Device, Feature, GuestModeConfig, IntentMetric, LiveKitConfig,
        Memory, User, VoiceInterface,
    )

    owner = User(username="owner", email="owner@example.com", role="owner", auth_provider="oidc", active=True,
                 created_at=now - timedelta(days=400))
    db.add(owner)
    db.add(User(username="local-admin", email="local@example.com", role="operator", auth_provider="local",
                active=True, created_at=now - timedelta(days=200)))
    db.add(User(username="gone", email="gone@example.com", role="viewer", auth_provider="saml", active=False,
                created_at=now - timedelta(days=100)))
    db.flush()

    models = {
        "intent_classifier": DEFAULT_MODEL,
        "intent_discovery": "llama3.1:8b",
        "response_synthesis": "openai/gpt-4o-mini",
        "conversation_summarizer": "vllm-qwen-served",
        "tool_calling_simple": "qwen3:8b",
        "tool_calling_complex": "qwen3:8b",
        "tool_calling_super_complex": "hf.co/bartowski/qwen2.5-7b-instruct-gguf:q4_k_m",
        "smart_home_control": "gpt-oss:120b-cloud",
        "response_validator_primary": "claude-sonnet-4-20250514",
        "response_validator_secondary": "mixtral:8x7b",
    }
    for name, model in models.items():
        backend_type = "anthropic" if model.startswith("claude") else "ollama"
        add_component(db, name, model, backend_type=backend_type)
    add_component(db, "fact_check_validation", "llama3.1:8b", enabled=False)
    add_backend(db, "qwen3:8b", "openai", "http://10.0.0.5:1234")
    add_backend(db, "vllm-qwen-served", "vllm", "http://10.0.0.5:8000")
    add_backend(db, "claude-sonnet-4-20250514", "anthropic", "https://api.anthropic.com")
    add_backend(db, "mixtral:8x7b", "mlx", "http://localhost:8080")

    add_service(db, "weather", host="athena-rag-weather")
    add_service(db, "sports", host="athena-rag-sports", health="unhealthy")
    add_service(db, "news", host="athena-rag-news", enabled=False)
    add_service(db, "acme-internal-rag", service_type="rag")
    add_service(db, "athena-orchestrator")
    add_service(db, "athena-gateway", health="unhealthy")
    add_service(db, "redis", service_type="infrastructure")
    add_service(db, "qdrant", service_type="infrastructure", health="unhealthy")

    db.add(Feature(name="intent_classification", display_name="x", category="processing", enabled=True))
    db.add(Feature(name="conversation_context", display_name="x", category="processing", enabled=True))
    db.add(Feature(name="mlx_backend", display_name="x", category="optimization", enabled=False))

    db.add(CloudLLMProvider(provider="openai", display_name="OpenAI", enabled=True))
    db.add(CloudLLMProvider(provider="anthropic", display_name="Anthropic", enabled=False))

    db.add(Device(device_type="wyoming", name="sat-1"))
    db.add(Device(device_type="wyoming", name="sat-2"))
    db.add(Device(device_type="jetson", name="edge-1"))

    db.add(LiveKitConfig(livekit_url="wss://livekit.example.org", enabled=True))
    db.add(VoiceInterface(interface_name="web_jarvis", enabled=True, stt_engine="faster-whisper", tts_engine="piper"))
    db.add(VoiceInterface(interface_name="home_assistant", enabled=True, stt_engine="homebrew-stt", tts_engine="kokoro"))
    db.add(VoiceInterface(interface_name="admin_jarvis", enabled=False, stt_engine="whisper-cpp", tts_engine="piper"))

    db.add(GuestModeConfig(enabled=True, calendar_url="", created_by_id=owner.id))
    db.add(CalendarSource(name="Listing", source_type="airbnb", ical_url="https://example.org/a.ics", enabled=True))
    db.add(CalendarSource(name="Direct", source_type="lodgify", ical_url="https://example.org/b.ics", enabled=True))
    db.add(CalendarSource(name="Old", source_type="vrbo", ical_url="https://example.org/c.ics", enabled=False))

    for i in range(42):
        db.add(Memory(content=f"memory {i}", scope="global", vector_id=f"00000000-0000-4000-8000-{i:012d}"))
    for i in range(3):
        db.add(Memory(content="gone", scope="global", vector_id=f"00000000-0000-4000-9000-{i:012d}", is_deleted=True))

    mix = {"control": 60, "weather": 45, "general_info": 30, "a_novel_intent": 15}
    for intent, n in mix.items():
        for j in range(n):
            db.add(IntentMetric(intent=intent, confidence=0.9, created_at=now - timedelta(days=1, minutes=j)))
    for j in range(20):
        db.add(IntentMetric(intent="control", confidence=0.9, created_at=now - timedelta(days=45)))
    db.commit()


# ---------------------------------------------------------------------------
# Sender harness
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=None):
        from datetime import datetime, timezone

        self.now = start or datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        from datetime import timedelta

        self.now = self.now + timedelta(**delta)


class Recorder:
    """An httpx MockTransport that records every request. ``status`` and
    ``body`` set the next responses; ``handler`` overrides both."""

    def __init__(self, status=200, body=b'{"status":"ok"}'):
        import httpx

        self.requests = []
        self.status = status
        self.body = body
        self.handler = None
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        import httpx

        request.read()
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(self.status, content=self.body)

    def payloads(self):
        return [json.loads(r.content) for r in self.requests]


def settings(db_or_factory, prefix="telemetry."):
    from app.models import SystemSetting

    session = db_or_factory() if callable(db_or_factory) else db_or_factory
    try:
        session.expire_all()
        return {r.key: r.value for r in session.query(SystemSetting).filter(SystemSetting.key.like(prefix + "%"))}
    finally:
        if callable(db_or_factory):
            session.close()
