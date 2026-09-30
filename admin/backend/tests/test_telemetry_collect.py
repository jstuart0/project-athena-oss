"""collect_facts over a real (SQLite) database: component resolution mirrors
the router, every seeded component is reported, windows and mappings."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models import IntentMetric, SystemSetting
from app.services.telemetry import collect
from app.services.telemetry.schema import SendState, build_payload
from tests._telemetry_support import (
    ALL_COMPONENTS, DEFAULT_MODEL, add_backend, add_component, add_service, canonical_collect_kwargs, seed_canonical,
)

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
STATE = SendState("3f1c9b2e-7d4a-4c1e-9a6b-2f8e5d7c1a90", "heartbeat", "new", "0.5.0", "stable", "self_hosted_real")


def _collect(db, **overrides):
    kwargs = canonical_collect_kwargs(NOW)
    kwargs.update(overrides)
    return asyncio.run(collect.collect_facts(db, **kwargs))


def _by_component(facts):
    return {c.component: c for c in facts.components}


def test_every_seeded_component_is_reported_even_with_no_rows(db):
    facts = _collect(db)
    components = _by_component(facts)
    assert sorted(components) == sorted(ALL_COMPONENTS)
    assert len(components) >= 11
    for fact in components.values():
        assert fact.assignment == "builtin_default"
        assert fact.raw_model == DEFAULT_MODEL
        assert (fact.backend, fact.locality) == ("ollama", "local")


def test_disabled_row_is_builtin_default_with_default_model_fields(db):
    add_component(db, "intent_classifier", "llama3.1:8b", enabled=False)
    db.commit()
    payload = build_payload(_collect(db), STATE)
    row = next(c for c in payload.llm.components if c.component == "intent_classifier")
    assert row.assignment == "builtin_default"
    assert (row.family, row.size_bucket, row.quantized, row.source) == ("qwen3", "4-9b", True, "ollama-library")
    assert (row.backend, row.locality) == ("ollama", "local")


def _one(db, component, model, **backend):
    add_component(db, component, model)
    if backend:
        add_backend(db, model, backend["type"], backend["url"])
    db.commit()
    return _by_component(_collect(db))[component]


def test_vendor_prefix_is_cloud_vendor(db):
    fact = _one(db, "intent_classifier", "openai/gpt-4o-mini")
    assert (fact.backend, fact.locality) == ("openai", "cloud")
    payload = build_payload(_collect(db), STATE)
    row = next(c for c in payload.llm.components if c.component == "intent_classifier")
    assert (row.family, row.source, row.assignment) == ("gpt-4o", "vendor-api", "configured")


def test_unlisted_backend_type_on_lan_is_local_openai_compatible(db):
    fact = _one(db, "intent_classifier", "served-model", type="vllm", url="http://10.0.0.5:8000")
    assert (fact.backend, fact.locality) == ("openai_compatible", "local")


def test_no_backend_row_is_ollama_at_the_router_url(db):
    fact = _one(db, "intent_classifier", "qwen3:4b")
    assert (fact.backend, fact.locality) == ("ollama", "local")


def test_ollama_cloud_tag_is_cloud(db):
    fact = _one(db, "intent_classifier", "gpt-oss:120b-cloud")
    assert (fact.backend, fact.locality) == ("ollama", "cloud")


def test_openai_row_on_lan_depends_on_component(db):
    add_component(db, "tool_calling_simple", "lan-served")
    add_component(db, "response_synthesis", "lan-served")
    add_backend(db, "lan-served", "openai", "http://10.0.0.5:1234")
    db.commit()
    components = _by_component(_collect(db))
    assert (components["tool_calling_simple"].backend, components["tool_calling_simple"].locality) == ("openai", "local")
    assert (components["response_synthesis"].backend, components["response_synthesis"].locality) == ("openai", "cloud")


def test_system_settings_ollama_url_is_ignored(db):
    db.add(SystemSetting(key="ollama_url", value="https://ollama.com", category="llm"))
    db.commit()
    fact = _by_component(_collect(db))["intent_classifier"]
    assert fact.locality == "local"


def test_custom_registry_name_is_counted_not_named(db):
    add_service(db, "weather", host="athena-rag-weather")
    add_service(db, "acme-private-svc", service_type="rag")
    db.commit()
    payload = build_payload(_collect(db), STATE)
    assert payload.services.rag_custom == "1"
    assert payload.services.rag_enabled == ["weather"]
    assert b"acme-private-svc" not in payload.model_dump_json().encode()


def test_intent_metrics_window_is_strictly_inside_30_days(db):
    for i in range(150):
        db.add(IntentMetric(intent="control", confidence=0.9, created_at=NOW - timedelta(days=29, minutes=i)))
    for i in range(50):
        db.add(IntentMetric(intent="weather", confidence=0.9, created_at=NOW - timedelta(days=31 + i)))
    db.add(IntentMetric(intent="weather", confidence=0.9, created_at=NOW - timedelta(days=30)))
    db.commit()
    facts = _collect(db)
    assert facts.queries_30d == 150
    assert dict(facts.intent_counts_30d) == {"control": 150}


@pytest.mark.parametrize("state,expected", [
    ("ready", "ready"), ("unavailable", "unavailable"), ("embedder_unavailable", "embedder_unavailable"),
    ("shape_mismatch", "shape_mismatch"), ("model_mismatch", "other"),
])
def test_vector_store_state_mapping(db, state, expected):
    payload = build_payload(_collect(db, vector_state=lambda: state), STATE)
    assert payload.memory.vector_store == expected


@pytest.mark.parametrize("env,expected", [
    ({}, False),
    ({"HA_URL": "http://ha.example.lan:8123"}, False),
    ({"HA_TOKEN": "t"}, False),
    ({"HA_URL": "http://ha.example.lan:8123", "HA_TOKEN": "t"}, True),
])
def test_home_assistant_configured(db, env, expected):
    assert _collect(db, env=env).ha_configured is expected


def test_dns_is_resolved_once_per_host_and_times_out_to_unknown(db, monkeypatch):
    calls = []

    async def resolver(host):
        calls.append(host)
        if host == "slow.example.org":
            await asyncio.sleep(5)
        return ["8.8.8.8"]

    monkeypatch.setattr(collect, "DNS_TIMEOUT_SECONDS", 0.05)
    add_component(db, "intent_classifier", "m1")
    add_component(db, "intent_discovery", "m2")
    add_component(db, "response_synthesis", "m3")
    add_backend(db, "m1", "ollama", "http://gpu.example.org:11434")
    add_backend(db, "m2", "ollama", "http://gpu.example.org:11434")
    add_backend(db, "m3", "ollama", "http://slow.example.org:11434")
    db.commit()
    components = _by_component(_collect(db, resolver=resolver))
    assert sorted(calls) == ["gpu.example.org", "slow.example.org"]
    assert components["intent_classifier"].locality == "remote"
    assert components["intent_discovery"].locality == "remote"
    assert components["response_synthesis"].locality == "unknown"


def test_canonical_seed_collects_every_signal(db):
    seed_canonical(db, NOW)
    facts = _collect(db)
    assert facts.platform == "linux" and facts.arch == "x86_64" and facts.deployment == "kubernetes"
    assert (facts.core_enabled, facts.core_healthy) == (2, 1)
    assert sorted(facts.rag_enabled) == ["acme-internal-rag", "sports", "weather"]
    assert sorted(facts.rag_healthy) == ["acme-internal-rag", "weather"]
    assert sorted(facts.infrastructure_healthy) == ["redis"]
    assert facts.cloud_providers_enabled == ("openai",)
    assert sorted(facts.flags_enabled) == ["conversation_context", "intent_classification"]
    assert sorted(facts.modules_enabled) == ["guest_mode", "home_assistant", "jarvis_web"]
    assert facts.jarvis_web_enabled is True
    assert (facts.wyoming_devices, facts.jetson_devices, facts.livekit_enabled) == (2, 1, True)
    assert sorted(facts.stt_engines) == ["faster-whisper", "homebrew-stt"]
    assert sorted(facts.tts_engines) == ["kokoro", "piper"]
    assert facts.guest_mode_enabled is True and facts.legacy_ical_configured is False
    assert sorted(facts.calendar_source_types) == ["airbnb", "lodgify"]
    assert facts.memories == 42
    assert facts.queries_30d == 150
    assert sorted(facts.auth_modes) == ["local", "oidc"]
    assert facts.oidc_configured is True
    assert (facts.control_agent_enabled, facts.service_control_k8s_enabled) == (False, True)
    components = _by_component(facts)
    assert components["fact_check_validation"].assignment == "builtin_default"
    assert (components["response_validator_primary"].backend, components["response_validator_primary"].locality) == \
        ("anthropic", "cloud")
    assert (components["response_validator_secondary"].backend,
            components["response_validator_secondary"].locality) == ("mlx", "local")
