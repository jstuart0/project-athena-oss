"""Fact collection: every table and query telemetry reads lives here.

Returns raw facts; the privacy gate (allowlists, buckets, model-field
reduction) is schema.build_payload. Component resolution mirrors the LLM
router: a ``provider/`` prefix, then the ``llm_backends`` row for the model,
then Ollama at ``get_config().ollama_url`` (never ``system_settings``).
"""
from __future__ import annotations

import asyncio
import os
import platform as _platform
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    CalendarSource, CloudLLMProvider, ComponentModelAssignment, Device, Feature, GuestModeConfig, IntentMetric,
    LiveKitConfig, LLMBackend, Memory, RagService, User, VoiceInterface,
)
from app.services.service_managers import group_for
from app.services.telemetry import classify
from app.services.telemetry.schema import BACKENDS, KNOWN_COMPONENTS, ComponentFact, Facts

AsyncResolver = Callable[[str], Awaitable[List[str]]]

DNS_TIMEOUT_SECONDS = 2.0
USAGE_WINDOW = timedelta(days=30)
_VENDOR_PROVIDERS = ("openai", "anthropic", "google")


@dataclass(frozen=True)
class _Resolution:
    component: str
    assignment: str
    raw_model: str
    backend: str
    endpoint_url: Optional[str]


async def _system_resolver(host: str) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _resolve_backend(model: str, backends: Dict[str, LLMBackend], ollama_url: str) -> Tuple[str, Optional[str]]:
    """(backend, endpoint_url) exactly as the router resolves a model name."""
    if "/" in model:
        provider = model.split("/", 1)[0].lower()
        if provider in _VENDOR_PROVIDERS:
            return provider, None
    row = backends.get(model)
    if row is not None:
        backend = (row.backend_type or "").lower()
        return (backend if backend in BACKENDS else "openai_compatible"), row.endpoint_url
    return "ollama", ollama_url


def _components(db: Session, default_model: str, ollama_url: str) -> List[_Resolution]:
    rows = {r.component_name: r for r in db.query(ComponentModelAssignment).all()}
    backends = {b.model_name: b for b in db.query(LLMBackend).order_by(LLMBackend.priority).all()}
    out = []
    for name in sorted(set(rows) | KNOWN_COMPONENTS):
        row = rows.get(name)
        if row is not None and row.enabled and row.model_name:
            assignment, model = "configured", row.model_name
        else:
            assignment, model = "builtin_default", default_model
        backend, endpoint = _resolve_backend(model, backends, ollama_url)
        out.append(_Resolution(name, assignment, model, backend, endpoint))
    return out


async def _resolve_hosts(hosts: List[str], resolver: AsyncResolver) -> Dict[str, Optional[List[str]]]:
    async def one(host: str) -> Optional[List[str]]:
        try:
            return list(await asyncio.wait_for(resolver(host), timeout=DNS_TIMEOUT_SECONDS))
        except Exception:
            return None

    results = await asyncio.gather(*(one(h) for h in hosts))
    return dict(zip(hosts, results))


async def _localities(resolutions: List[_Resolution], resolver: AsyncResolver) -> List[str]:
    """Locality per component, resolving each distinct host at most once and
    only when the pure rules can't decide without DNS."""
    needs_dns: List[str] = []

    def recording(host: str) -> List[str]:
        if host not in needs_dns:
            needs_dns.append(host)
        return []

    for r in resolutions:
        classify.locality(r.backend, r.component, r.raw_model, r.endpoint_url, recording)
    answers = await _resolve_hosts(needs_dns, resolver) if needs_dns else {}

    def answered(host: str) -> List[str]:
        result = answers.get(host)
        if result is None:
            raise TimeoutError(host)
        return result

    return [classify.locality(r.backend, r.component, r.raw_model, r.endpoint_url, answered) for r in resolutions]


def _services(db: Session):
    core_enabled = core_healthy = 0
    rag_enabled: List[str] = []
    rag_healthy: List[str] = []
    infra_healthy: List[str] = []
    for row in db.query(RagService).all():
        if not row.enabled:
            continue
        group = group_for(row)
        healthy = (row.health_status or "").lower() == "healthy"
        name = (row.name or "").lower()
        if group == "rag":
            rag_enabled.append(name)
            if healthy:
                rag_healthy.append(name)
        elif group == "infrastructure":
            if healthy:
                infra_healthy.append(name)
        else:
            core_enabled += 1
            core_healthy += int(healthy)
    return core_enabled, core_healthy, tuple(rag_enabled), tuple(rag_healthy), tuple(infra_healthy)


def _usage(db: Session, now: datetime) -> Tuple[int, Dict[str, int]]:
    since = now - USAGE_WINDOW
    rows = (db.query(IntentMetric.intent, func.count(IntentMetric.id))
            .filter(IntentMetric.created_at > since)
            .group_by(IntentMetric.intent).all())
    counts = {(intent or "unknown"): int(n) for intent, n in rows}
    return sum(counts.values()), counts


async def collect_facts(
    db: Session,
    *,
    now: datetime,
    resolver: Optional[AsyncResolver] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    exists: Optional[Callable[[str], bool]] = None,
    config=None,
    module_enabled: Optional[Callable[[str], bool]] = None,
    vector_state: Optional[Callable[[], str]] = None,
    default_model: Optional[str] = None,
) -> Facts:
    """Read every fact the payload needs. Every environment-dependent input
    is injectable; the defaults are this process's real ones."""
    if env is None:
        env = os.environ
    if config is None:
        from shared.config import get_config
        config = get_config()
    if module_enabled is None:
        from shared.module_registry import module_registry
        module_enabled = module_registry.is_enabled
    if vector_state is None:
        from app.services import memory_vectors
        vector_state = lambda: memory_vectors.get_state().status  # noqa: E731
    if default_model is None:
        from app.database import OSS_DEFAULT_MODEL
        default_model = OSS_DEFAULT_MODEL

    plat, arch = classify.platform_arch(system or _platform.system(), machine or _platform.machine())
    deployment = classify.deployment_shape(env, exists or os.path.exists)

    resolutions = _components(db, default_model, config.ollama_url)
    localities = await _localities(resolutions, resolver or _system_resolver)
    components = tuple(
        ComponentFact(r.component, r.assignment, r.raw_model, r.backend, loc)
        for r, loc in zip(resolutions, localities)
    )

    core_enabled, core_healthy, rag_enabled, rag_healthy, infra_healthy = _services(db)
    queries_30d, intent_counts = _usage(db, now)

    guest = db.query(GuestModeConfig).order_by(GuestModeConfig.id).first()
    voice = db.query(VoiceInterface).filter(VoiceInterface.enabled == True).all()  # noqa: E712
    modules = [m for m in ("home_assistant", "guest_mode", "notifications", "monitoring", "jarvis_web")
               if module_enabled(m)]
    state = await asyncio.to_thread(vector_state)

    return Facts(
        platform=plat,
        arch=arch,
        deployment=deployment,
        core_enabled=core_enabled,
        core_healthy=core_healthy,
        rag_enabled=rag_enabled,
        rag_healthy=rag_healthy,
        infrastructure_healthy=infra_healthy,
        components=components,
        cloud_providers_enabled=tuple(sorted(
            p for (p,) in db.query(CloudLLMProvider.provider).filter(CloudLLMProvider.enabled == True).all()  # noqa: E712
        )),
        modules_enabled=tuple(modules),
        flags_enabled=tuple(sorted(n for (n,) in db.query(Feature.name).filter(Feature.enabled == True).all())),  # noqa: E712
        ha_configured=bool(env.get("HA_URL") and env.get("HA_TOKEN")),
        wyoming_devices=db.query(Device).filter(Device.device_type == "wyoming").count(),
        jetson_devices=db.query(Device).filter(Device.device_type == "jetson").count(),
        livekit_enabled=db.query(LiveKitConfig).filter(LiveKitConfig.enabled == True).count() > 0,  # noqa: E712
        jarvis_web_enabled="jarvis_web" in modules,
        stt_engines=tuple(sorted({v.stt_engine for v in voice if v.stt_engine})),
        tts_engines=tuple(sorted({v.tts_engine for v in voice if v.tts_engine})),
        guest_mode_enabled=bool(guest and guest.enabled),
        calendar_source_types=tuple(sorted({
            t for (t,) in db.query(CalendarSource.source_type).filter(CalendarSource.enabled == True).all()  # noqa: E712
        })),
        legacy_ical_configured=bool(guest and (guest.calendar_url or "").strip()),
        memories=db.query(Memory).filter(Memory.is_deleted == False).count(),  # noqa: E712
        vector_store=state,
        queries_30d=queries_30d,
        intent_counts_30d=intent_counts,
        auth_modes=tuple(sorted({
            (p or "").lower() for (p,) in db.query(User.auth_provider).filter(User.active == True).all()  # noqa: E712
        })),
        oidc_configured=bool((config.oidc_issuer or "").strip()),
        control_agent_enabled=bool(config.control_agent_enabled),
        service_control_k8s_enabled=bool(config.service_control_k8s_enabled),
    )
