"""Payload v1: the wire shape, the allowlists, and the bucketing rules.

Everything that leaves the install is decided here. Every model forbids
extra keys, every value is a closed enum, a bucket, a boolean, or a name
checked against a client allowlist (open-vocabulary fields also carry a
charset pattern, so the collector can accept names a later release adds).
Model identity is four structured fields (family, size bucket, quantized,
source); no model name, tag or repo is ever sent.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Annotated, Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION = 1

OPEN_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,47}$"
FAMILY_RE = r"^[a-z0-9.\-]{1,24}$"
VERSION_RE = r"^\d+\.\d+\.\d+((rc|a|b|dev|post)\d*)?$"
UUID4_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

# Parameter-size candidates over the whole lowercased raw name: "7b", "0.5b",
# "135m" (millions), and "8x7b" (a product). "a3b"/"e4b" active-parameter
# tokens are excluded by the lookbehind.
SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)([bm])(?![a-z0-9])")
MOE_SIZE_RE = re.compile(r"(?<![a-z0-9])(\d+)x(\d+(?:\.\d+)?)b(?![a-z0-9])")
QUANT_TOKEN_RE = re.compile(r"^(q[1-8]|iq[1-4]|fp8|int4|int8|awq|gptq|gguf|exl2|nf4|[2-8]bit)$")
TOKEN_SPLIT_RE = re.compile(r"[:/_\-.]+")

MAX_COMPONENTS = 16
MAX_NAMES = 64

COUNT_BUCKETS = ("0", "1", "2-5", "6-20", "21-50", "51-200", "201-1000", "1001+")
RATE_BUCKETS = ("0", "<1", "1-9", "10-49", "50-199", "200+")
INTENT_PERCENTS = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
SIZE_BUCKETS = ("le3b", "4-9b", "10-20b", "21-40b", "41-80b", "gt80b", "unknown")
MODEL_SOURCES = ("ollama-library", "hf-public-publisher", "vendor-api", "custom-registry", "custom")
BACKENDS = ("ollama", "mlx", "auto", "openai", "anthropic", "google", "openai_compatible")
LOCALITIES = ("local", "cloud", "remote", "unknown")
VECTOR_STORE_STATES = ("ready", "unavailable", "embedder_unavailable", "shape_mismatch", "other")

# The eleven components seeded by alembic 050.
KNOWN_COMPONENTS = frozenset({
    "intent_classifier", "intent_discovery", "response_synthesis", "conversation_summarizer",
    "tool_calling_simple", "tool_calling_complex", "tool_calling_super_complex",
    "smart_home_control", "response_validator_primary", "response_validator_secondary",
    "fact_check_validation",
})
TOOL_CALLING_COMPONENTS = frozenset({"tool_calling_simple", "tool_calling_complex", "tool_calling_super_complex"})

# RAG-group names seeded by app/database.py OSS_SERVICE_REGISTRY.
KNOWN_RAG_SERVICES = frozenset({
    "weather", "airports", "stocks", "flights", "events", "streaming", "news", "sports",
    "websearch", "dining", "recipes", "onecall", "seatgeek", "transportation", "community",
    "amtrak", "tesla", "media", "directions", "sitescraper", "serpapi", "pricecompare", "brightdata",
})
KNOWN_INFRASTRUCTURE = frozenset({"redis", "qdrant", "postgres", "searxng", "control-agent"})

# Feature flags seeded across the alembic migrations.
KNOWN_FEATURES = frozenset({
    "intent_classification", "multi_intent_detection", "conversation_context",
    "rag_weather", "rag_sports", "rag_airports", "rag_directions",
    "redis_caching", "mlx_backend", "response_streaming", "home_assistant", "clarification_questions",
    "llm_based_routing", "enable_llm_intent_classification", "intent_discovery", "music_playback",
    "tool_system_enabled", "mcp_integration", "n8n_integration", "legacy_tools_fallback",
    "admin_jarvis", "real_time_events", "intent_visualization", "self_building_tools", "livekit_webrtc",
    "automation_system_mode", "voice_automations", "hybrid_memory_search", "ai_follow_ups_enabled",
    "weather_provider", "search_pre_classification", "status_bulk_query", "status_skip_synthesis",
    "post_synthesis_fallback", "state_question_routing_kill_switch",
})
KNOWN_MODULES = frozenset({"home_assistant", "guest_mode", "notifications", "monitoring", "jarvis_web"})
KNOWN_STT_ENGINES = frozenset({"faster-whisper", "openai-whisper", "whisper-cpp", "speaches", "web-speech"})
KNOWN_TTS_ENGINES = frozenset({"piper", "kokoro", "elevenlabs", "openai-tts", "speaches", "web-speech"})
KNOWN_INTENTS = frozenset({
    "control", "weather", "airports", "sports", "flights", "events", "streaming", "news", "stocks",
    "recipes", "dining", "directions", "websearch", "text_me_that", "music_play", "music_control",
    "tv_control", "notification_pref", "tesla", "general_info", "unknown",
})
KNOWN_CALENDAR_SOURCE_TYPES = ("airbnb", "vrbo", "lodgify", "generic_ical")
KNOWN_AUTH_MODES = ("oidc", "local")
KNOWN_CLOUD_PROVIDERS = ("openai", "anthropic", "google")

PUBLIC_HF_PUBLISHERS = frozenset({
    "bartowski", "unsloth", "qwen", "meta-llama", "mistralai", "lmstudio-community", "google",
    "microsoft", "deepseek-ai", "thebloke", "mradermacher", "nousresearch", "ibm-granite",
    "huggingfacetb", "allenai", "nvidia",
})

KNOWN_CLOUD_LLM_HOSTS = frozenset({
    "api.openai.com", "openai.azure.com", "api.anthropic.com",
    "generativelanguage.googleapis.com", "aiplatform.googleapis.com",
    "ollama.com",
    "api.groq.com", "api.together.xyz", "api.mistral.ai", "openrouter.ai", "api.deepseek.com",
    "api.fireworks.ai", "api.x.ai", "api.cerebras.ai", "api.perplexity.ai",
    "router.huggingface.co", "integrate.api.nvidia.com",
})
LOCAL_HOST_NAMES = frozenset({"localhost", "host.docker.internal"})
LOCAL_HOST_SUFFIXES = ("svc", "cluster.local", "local", "lan", "internal", "home.arpa", "localdomain")

MODEL_FAMILIES = frozenset({
    # Qwen
    "qwen", "qwen2", "qwen2.5", "qwen3", "qwq",
    # Llama
    "llama", "llama2", "llama3", "llama3.1", "llama3.2", "llama3.3", "llama4", "codellama", "tinyllama",
    # Mistral
    "mistral", "mistral-small", "mistral-nemo", "mixtral", "ministral", "magistral", "devstral", "codestral",
    # Gemma
    "gemma", "gemma2", "gemma3", "gemma3n",
    # Phi
    "phi3", "phi3.5", "phi4",
    # DeepSeek
    "deepseek-r1", "deepseek-v3", "deepseek-coder",
    # OpenAI
    "gpt-oss", "gpt-3.5", "gpt-4", "gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4",
    # Anthropic
    "claude", "claude-opus", "claude-sonnet", "claude-haiku",
    # Google
    "gemini", "gemini-1.5", "gemini-2.0", "gemini-2.5",
    # Others
    "command-r", "granite", "granite3", "granite4", "smollm", "smollm2", "nemotron", "olmo2", "falcon3",
    "llava", "minicpm-v", "hermes3", "dolphin3", "kimi-k2", "glm4", "cogito", "exaone3.5", "aya",
    "zephyr", "solar",
    # Embedding models
    "nomic-embed-text", "mxbai-embed-large", "all-minilm", "bge-m3", "snowflake-arctic-embed",
})

OpenName = Annotated[str, StringConstraints(pattern=OPEN_NAME_RE)]
# D46: the wire family is a closed list. The collector stores only its own
# shipped list, coercing anything else to `custom`, so a family added here is
# reported as `custom` until the collector ships the same list.
Family = Literal[tuple(sorted(MODEL_FAMILIES | {"custom"}))]  # type: ignore[valid-type]
NameList = Annotated[List[OpenName], Field(max_length=MAX_NAMES)]
CountBucket = Literal["0", "1", "2-5", "6-20", "21-50", "51-200", "201-1000", "1001+"]
RateBucket = Literal["0", "<1", "1-9", "10-49", "50-199", "200+"]
IntentPercent = Literal[10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Install(_Strict):
    version: Annotated[str, StringConstraints(pattern=VERSION_RE, max_length=64)]
    release_channel: Literal["stable", "prerelease", "dev"]
    install_class: Literal["production", "self_hosted_real", "dev", "test", "ci"]
    provenance: Literal["new", "upgraded"]
    platform: Literal["linux", "darwin", "windows", "other"]
    arch: Literal["x86_64", "aarch64", "other"]
    deployment: Literal["kubernetes", "container", "bare"]


class Services(_Strict):
    core_enabled: CountBucket
    core_healthy: CountBucket
    rag_enabled: NameList
    rag_healthy: NameList
    rag_custom: CountBucket
    infrastructure_healthy: NameList


class LLMComponent(_Strict):
    component: OpenName
    assignment: Literal["configured", "builtin_default"]
    family: Family
    size_bucket: Literal["le3b", "4-9b", "10-20b", "21-40b", "41-80b", "gt80b", "unknown"]
    quantized: bool
    source: Literal["ollama-library", "hf-public-publisher", "vendor-api", "custom-registry", "custom"]
    backend: Literal["ollama", "mlx", "auto", "openai", "anthropic", "google", "openai_compatible"]
    locality: Literal["local", "cloud", "remote", "unknown"]


class LLM(_Strict):
    components: Annotated[List[LLMComponent], Field(max_length=MAX_COMPONENTS)]
    custom_components: CountBucket
    cloud_providers_enabled: Annotated[List[Literal["openai", "anthropic", "google"]], Field(max_length=MAX_NAMES)]


class Features(_Strict):
    modules_enabled: NameList
    flags_enabled: NameList


class HomeAssistant(_Strict):
    configured: bool


class Voice(_Strict):
    wyoming_devices: CountBucket
    jetson_devices: CountBucket
    livekit_enabled: bool
    jarvis_web_enabled: bool
    stt_engines: NameList
    tts_engines: NameList


class GuestMode(_Strict):
    enabled: bool
    calendar_source_types: Annotated[
        List[Literal["airbnb", "vrbo", "lodgify", "generic_ical"]], Field(max_length=MAX_NAMES)
    ]
    legacy_ical_configured: bool


class MemoryInfo(_Strict):
    memories: CountBucket
    vector_store: Literal["ready", "unavailable", "embedder_unavailable", "shape_mismatch", "other"]


class Usage(_Strict):
    queries_per_day_30d: RateBucket
    intent_mix_30d: Optional[Annotated[Dict[OpenName, IntentPercent], Field(max_length=MAX_NAMES)]]


class PlatformConfig(_Strict):
    auth_modes: Annotated[List[Literal["oidc", "local"]], Field(max_length=MAX_NAMES)]
    oidc_configured: bool
    control_agent_enabled: bool
    service_control_k8s_enabled: bool


class Payload(_Strict):
    schema_version: Literal[1]
    installation_id: Annotated[str, StringConstraints(pattern=UUID4_RE)]
    event: Literal["first_boot", "heartbeat"]
    install: Install
    services: Services
    llm: LLM
    features: Features
    home_assistant: HomeAssistant
    voice: Voice
    guest_mode: GuestMode
    memory: MemoryInfo
    usage: Usage
    platform_config: PlatformConfig


@dataclass(frozen=True)
class ModelFields:
    family: str
    size_bucket: str
    quantized: bool
    source: str


@dataclass(frozen=True)
class ComponentFact:
    """One component as collected: the raw model never leaves this process;
    build_payload reduces it to ModelFields."""
    component: str
    assignment: str
    raw_model: Optional[str]
    backend: str
    locality: str


@dataclass(frozen=True)
class Facts:
    platform: str = "other"
    arch: str = "other"
    deployment: str = "bare"
    core_enabled: int = 0
    core_healthy: int = 0
    rag_enabled: Tuple[str, ...] = ()
    rag_healthy: Tuple[str, ...] = ()
    infrastructure_healthy: Tuple[str, ...] = ()
    components: Tuple[ComponentFact, ...] = ()
    cloud_providers_enabled: Tuple[str, ...] = ()
    modules_enabled: Tuple[str, ...] = ()
    flags_enabled: Tuple[str, ...] = ()
    ha_configured: bool = False
    wyoming_devices: int = 0
    jetson_devices: int = 0
    livekit_enabled: bool = False
    jarvis_web_enabled: bool = False
    stt_engines: Tuple[str, ...] = ()
    tts_engines: Tuple[str, ...] = ()
    guest_mode_enabled: bool = False
    calendar_source_types: Tuple[str, ...] = ()
    legacy_ical_configured: bool = False
    memories: int = 0
    vector_store: str = "unavailable"
    queries_30d: int = 0
    intent_counts_30d: Mapping[str, int] = field(default_factory=dict)
    auth_modes: Tuple[str, ...] = ()
    oidc_configured: bool = False
    control_agent_enabled: bool = False
    service_control_k8s_enabled: bool = False


@dataclass(frozen=True)
class SendState:
    installation_id: str
    event: str
    provenance: str
    version: str
    release_channel: str
    install_class: str


def bucket_count(n: int) -> str:
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    for upper, label in ((5, "2-5"), (20, "6-20"), (50, "21-50"), (200, "51-200"), (1000, "201-1000")):
        if n <= upper:
            return label
    return "1001+"


def bucket_rate(count_30d: int) -> str:
    """Average per day over 30 days, from the raw 30-day count."""
    if count_30d <= 0:
        return "0"
    for upper, label in ((30, "<1"), (300, "1-9"), (1500, "10-49"), (6000, "50-199")):
        if count_30d < upper:
            return label
    return "200+"


def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _snap10(pct: int) -> int:
    return _half_up(pct / 10.0) * 10


def intent_mix(counts: Mapping[str, int]) -> Optional[Dict[str, int]]:
    """Percent of queries per intent, snapped to tens (round half up, never
    Python's banker's round). Omitted below 100 queries. Unknown intents
    and intents that snap to 0 fold into ``other``; the remainder key
    (``other`` if present, else the largest) is recomputed so the mix sums
    to 100 where it can."""
    folded: Dict[str, int] = {}
    for intent, n in counts.items():
        if n <= 0:
            continue
        key = intent if intent in KNOWN_INTENTS else "other"
        folded[key] = folded.get(key, 0) + n
    total = sum(folded.values())
    if total < 100:
        return None

    snapped: Dict[str, int] = {}
    for key, n in list(folded.items()):
        if key == "other":
            continue
        value = _snap10(_half_up(100.0 * n / total))
        if value == 0:
            folded["other"] = folded.get("other", 0) + n
        else:
            snapped[key] = value
    if "other" in folded:
        remainder_key = "other"
    else:
        remainder_key = max(snapped, key=lambda k: (folded[k], k))
    rest = sum(v for k, v in snapped.items() if k != remainder_key)
    remainder = _snap10(max(0, 100 - rest))
    snapped.pop(remainder_key, None)
    if remainder > 0:
        snapped[remainder_key] = min(remainder, 100)
    return {k: snapped[k] for k in sorted(snapped)}


def _names(values, allowed) -> List[str]:
    return sorted({v for v in values if v in allowed})[:MAX_NAMES]


def build_payload(facts: Facts, state: SendState) -> Payload:
    """The privacy gate: every name is filtered against its allowlist and
    every model is reduced to its D42 fields before anything is serialized."""
    from app.services.telemetry.classify import classify_model

    components = []
    custom_components = 0
    for fact in sorted(facts.components, key=lambda c: c.component):
        if fact.component not in KNOWN_COMPONENTS:
            custom_components += 1
            continue
        model = classify_model(fact.raw_model, fact.backend, fact.locality)
        components.append(LLMComponent(
            component=fact.component,
            assignment=fact.assignment,
            family=model.family,
            size_bucket=model.size_bucket,
            quantized=model.quantized,
            source=model.source,
            backend=fact.backend if fact.backend in BACKENDS else "openai_compatible",
            locality=fact.locality if fact.locality in LOCALITIES else "unknown",
        ))

    rag_custom = len({n for n in facts.rag_enabled if n not in KNOWN_RAG_SERVICES})

    def engines(values, known):
        return sorted({v if v in known else "other" for v in values})[:MAX_NAMES]

    vector_store = facts.vector_store if facts.vector_store in VECTOR_STORE_STATES else "other"

    return Payload(
        schema_version=SCHEMA_VERSION,
        installation_id=state.installation_id,
        event=state.event,
        install=Install(
            version=state.version,
            release_channel=state.release_channel,
            install_class=state.install_class,
            provenance=state.provenance,
            platform=facts.platform,
            arch=facts.arch,
            deployment=facts.deployment,
        ),
        services=Services(
            core_enabled=bucket_count(facts.core_enabled),
            core_healthy=bucket_count(facts.core_healthy),
            rag_enabled=_names(facts.rag_enabled, KNOWN_RAG_SERVICES),
            rag_healthy=_names(facts.rag_healthy, KNOWN_RAG_SERVICES),
            rag_custom=bucket_count(rag_custom),
            infrastructure_healthy=_names(facts.infrastructure_healthy, KNOWN_INFRASTRUCTURE),
        ),
        llm=LLM(
            components=components[:MAX_COMPONENTS],
            custom_components=bucket_count(custom_components),
            cloud_providers_enabled=_names(facts.cloud_providers_enabled, KNOWN_CLOUD_PROVIDERS),
        ),
        features=Features(
            modules_enabled=_names(facts.modules_enabled, KNOWN_MODULES),
            flags_enabled=_names(facts.flags_enabled, KNOWN_FEATURES),
        ),
        home_assistant=HomeAssistant(configured=bool(facts.ha_configured)),
        voice=Voice(
            wyoming_devices=bucket_count(facts.wyoming_devices),
            jetson_devices=bucket_count(facts.jetson_devices),
            livekit_enabled=bool(facts.livekit_enabled),
            jarvis_web_enabled=bool(facts.jarvis_web_enabled),
            stt_engines=engines(facts.stt_engines, KNOWN_STT_ENGINES),
            tts_engines=engines(facts.tts_engines, KNOWN_TTS_ENGINES),
        ),
        guest_mode=GuestMode(
            enabled=bool(facts.guest_mode_enabled),
            calendar_source_types=_names(facts.calendar_source_types, KNOWN_CALENDAR_SOURCE_TYPES),
            legacy_ical_configured=bool(facts.legacy_ical_configured),
        ),
        memory=MemoryInfo(memories=bucket_count(facts.memories), vector_store=vector_store),
        usage=Usage(
            queries_per_day_30d=bucket_rate(facts.queries_30d),
            intent_mix_30d=intent_mix(facts.intent_counts_30d),
        ),
        platform_config=PlatformConfig(
            auth_modes=_names(facts.auth_modes, KNOWN_AUTH_MODES),
            oidc_configured=bool(facts.oidc_configured),
            control_agent_enabled=bool(facts.control_agent_enabled),
            service_control_k8s_enabled=bool(facts.service_control_k8s_enabled),
        ),
    )


def serialize(payload: Payload) -> bytes:
    """The exact request body: compact, keys sorted."""
    return json.dumps(payload.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode("utf-8")


def pretty(payload: Payload) -> str:
    """The fixture form (golden file, docs)."""
    return json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def json_schema_text() -> str:
    return json.dumps(Payload.model_json_schema(), indent=2, sort_keys=True) + "\n"
