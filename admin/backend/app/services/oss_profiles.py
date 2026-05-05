"""
OSS profile initialization, planning, diagnostics, and runtime-sync helpers.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..', 'src'))
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog
from shared.config import get_config
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CloudLLMProvider,
    ComponentModelAssignment,
    GatewayConfig,
    LLMBackend,
    ModelConfiguration,
    SystemSetting,
    User,
)
from app.services.backend_adapters import (
    CloudAdapter,
    MLXAdapter,
    ModelDescriptor,
    OllamaAdapter,
    OpenAICompatibleAdapter,
)

logger = structlog.get_logger()

SETTINGS_CATEGORY = "oss_profiles"
ACTIVE_PROFILE_KEY = "oss_active_profile"
PROFILE_OWNERSHIP_KEY = "oss_profile_ownership"
PROFILE_STATUS_KEY = "oss_profile_status"
PROFILE_LAST_RUN_KEY = "oss_profile_last_run"
PROFILE_RUNS_KEY = "oss_profile_runs"
RUNTIME_SYNC_KEY = "oss_runtime_sync"
PROFILE_REGISTRY_STATE_KEY = "oss_profile_registry_state"

DEFAULT_OLLAMA_URL = get_config().ollama_url
RESTART_HOOK_MODE = os.getenv("ATHENA_OSS_RESTART_HOOK_MODE", "manual").lower()

DEFAULT_COMPONENTS = [
    ("intent_classifier", "Intent Classification", "Classifies user queries into intent categories", "orchestrator"),
    ("tool_calling_simple", "Tool Calling (Simple)", "Selects RAG tools for simple queries", "orchestrator"),
    ("tool_calling_complex", "Tool Calling (Complex)", "Selects RAG tools for complex queries", "orchestrator"),
    ("tool_calling_super_complex", "Tool Calling (Super Complex)", "Selects RAG tools for highly complex queries", "orchestrator"),
    ("response_synthesis", "Response Synthesis", "Generates natural language responses from retrieved results", "orchestrator"),
    ("fact_check_validation", "Fact-Check Validation", "Validates responses for accuracy", "validation"),
    ("smart_home_control", "Smart Home Control", "Extracts device commands from natural language", "control"),
    ("response_validator_primary", "Response Validator (Primary)", "Primary model for cross-validation", "validation"),
    ("response_validator_secondary", "Response Validator (Secondary)", "Secondary model for cross-validation", "validation"),
    ("conversation_summarizer", "Conversation Summarizer", "Compresses conversation history", "orchestrator"),
]

LATENCY_CLASS_BY_COMPONENT = {
    "intent_classifier": "low",
    "smart_home_control": "low",
    "conversation_summarizer": "low",
    "tool_calling_simple": "low",
    "tool_calling_complex": "medium",
    "tool_calling_super_complex": "medium",
    "response_synthesis": "medium",
    "fact_check_validation": "medium",
    "response_validator_primary": "medium",
    "response_validator_secondary": "medium",
}

FIT_WEIGHTS = {
    "preferred": 100.0,
    "compatible": 70.0,
    "degraded": 35.0,
    "incompatible": -1000.0,
}


@dataclass
class CompatibilityScore:
    rank: float
    fit_level: str
    reasons: List[str]
    rationale_summary: str
    supports_required_options: bool
    latency_class_match: bool
    model: str
    backend_type: str
    backend_key: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AssignmentDecision:
    component_name: str
    selected_backend: Optional[str]
    selected_model: Optional[str]
    decision_state: str
    selected_score: Optional[CompatibilityScore]
    fallback_chain: List[CompatibilityScore]
    rationale_summary: str
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_name": self.component_name,
            "selected_backend": self.selected_backend,
            "selected_model": self.selected_model,
            "decision_state": self.decision_state,
            "selected_score": self.selected_score.to_dict() if self.selected_score else None,
            "fallback_chain": [score.to_dict() for score in self.fallback_chain],
            "rationale_summary": self.rationale_summary,
            "warnings": self.warnings,
        }


@dataclass
class AssignmentPlan:
    component_assignments: Dict[str, AssignmentDecision]
    aggregated_status: str
    warnings: List[str]
    missing_capabilities: List[str]
    profile_name: str
    profile_version: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_assignments": {name: decision.to_dict() for name, decision in self.component_assignments.items()},
            "aggregated_status": self.aggregated_status,
            "warnings": self.warnings,
            "missing_capabilities": self.missing_capabilities,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_model_family(model_name: str) -> str:
    lowered = (model_name or "").lower()
    if "qwen3" in lowered:
        return "qwen3"
    if "qwen2.5" in lowered:
        return "qwen2.5"
    if "llama" in lowered:
        return "llama"
    if "phi" in lowered:
        return "phi"
    return lowered.split(":", 1)[0] or lowered


def preferred_backend_order(component_name: str) -> List[str]:
    latency_class = LATENCY_CLASS_BY_COMPONENT.get(component_name, "medium")
    if latency_class == "low":
        return ["mlx", "ollama", "openai", "cloud"]
    return ["mlx", "ollama", "openai", "cloud"]


def backend_preference_score(component_name: str, backend_type: str) -> float:
    order = preferred_backend_order(component_name)
    normalized = "openai" if backend_type not in {"ollama", "mlx", "cloud"} else backend_type
    if normalized not in order:
        return 0.0
    return float((len(order) - order.index(normalized)) * 10)


def component_meta() -> Dict[str, Dict[str, str]]:
    return {
        name: {
            "display_name": display_name,
            "description": description,
            "category": category,
        }
        for name, display_name, description, category in DEFAULT_COMPONENTS
    }


def profile_registry() -> Dict[str, Dict[str, Any]]:
    ollama_default = os.getenv("ATHENA_DEFAULT_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
    mlx_small = os.getenv("ATHENA_MLX_SMALL_MODEL", "qwen3-4b-mlx")
    mlx_large = os.getenv("ATHENA_MLX_LARGE_MODEL", "qwen3-8b-mlx")
    profiles = {
        "ollama_qwen_small": {
            "name": "ollama_qwen_small",
            "display_name": "Ollama Qwen Small",
            "description": "Balanced default for local Ollama deployments using tuned Qwen models.",
            "version": "1.1.0",
            "last_validated": "2026-04-02",
            "preferred_models": [ollama_default, "qwen3:8b"],
            "preferred_families": ["qwen3"],
            "gateway": {
                "intent_model": ollama_default,
                "intent_temperature": 0.1,
                "intent_max_tokens": 24,
                "intent_timeout_seconds": 15,
            },
            "model_configurations": {
                "_default": {
                    "backend_type": "ollama",
                    "temperature": 0.4,
                    "max_tokens": 256,
                    "timeout_seconds": 45,
                    "keep_alive_seconds": -1,
                    "ollama_options": {"num_ctx": 2048, "num_batch": 256},
                },
                ollama_default: {
                    "backend_type": "ollama",
                    "temperature": 0.4,
                    "max_tokens": 256,
                    "timeout_seconds": 45,
                    "keep_alive_seconds": -1,
                    "ollama_options": {
                        "num_ctx": 2048,
                        "num_batch": 256,
                        "num_predict": 256,
                        "mirostat": 2,
                        "mirostat_eta": 0.1,
                        "mirostat_tau": 5.0,
                    },
                },
                "qwen3:8b": {
                    "backend_type": "ollama",
                    "temperature": 0.4,
                    "max_tokens": 384,
                    "timeout_seconds": 60,
                    "keep_alive_seconds": -1,
                    "ollama_options": {"num_ctx": 2048, "num_batch": 256, "num_predict": 256},
                },
            },
            "component_hints": {
                "tool_calling_super_complex": {"preferred_models": ["qwen3:8b"]},
                "fact_check_validation": {"preferred_models": ["qwen3:8b"]},
            },
        },
        "mlx_qwen_hybrid": {
            "name": "mlx_qwen_hybrid",
            "display_name": "MLX Qwen Hybrid",
            "description": "Prefer MLX for low-latency local inference and fall back to Ollama for gaps.",
            "version": "1.0.0",
            "last_validated": "2026-04-02",
            "preferred_models": [mlx_small, mlx_large, ollama_default, "qwen3:8b"],
            "preferred_families": ["qwen3"],
            "gateway": {
                "intent_model": mlx_small,
                "intent_temperature": 0.1,
                "intent_max_tokens": 24,
                "intent_timeout_seconds": 15,
            },
            "model_configurations": {
                mlx_small: {
                    "backend_type": "mlx",
                    "temperature": 0.3,
                    "max_tokens": 192,
                    "timeout_seconds": 40,
                    "keep_alive_seconds": -1,
                    "mlx_options": {"chat_template_kwargs": {"enable_thinking": False}},
                },
                mlx_large: {
                    "backend_type": "mlx",
                    "temperature": 0.3,
                    "max_tokens": 256,
                    "timeout_seconds": 45,
                    "keep_alive_seconds": -1,
                    "mlx_options": {"chat_template_kwargs": {"enable_thinking": False}},
                },
                ollama_default: {
                    "backend_type": "ollama",
                    "temperature": 0.4,
                    "max_tokens": 256,
                    "timeout_seconds": 45,
                    "keep_alive_seconds": -1,
                    "ollama_options": {"num_ctx": 2048, "num_batch": 256, "num_predict": 256},
                },
            },
            "component_hints": {
                "intent_classifier": {"preferred_backend_types": ["mlx", "ollama"]},
                "conversation_summarizer": {"preferred_backend_types": ["mlx", "ollama"]},
                "response_synthesis": {"preferred_backend_types": ["mlx", "ollama"]},
                "tool_calling_super_complex": {"preferred_models": [mlx_large, "qwen3:8b"]},
            },
        },
        "safe_fallback": {
            "name": "safe_fallback",
            "display_name": "Safe Fallback",
            "description": "Fallback profile when preferred tuned models are not available.",
            "version": "1.0.0",
            "last_validated": "2026-04-02",
            "preferred_models": [ollama_default],
            "preferred_families": ["qwen3", "llama", "phi"],
            "gateway": {
                "intent_model": ollama_default,
                "intent_temperature": 0.1,
                "intent_max_tokens": 24,
                "intent_timeout_seconds": 15,
            },
            "model_configurations": {
                "_default": {
                    "backend_type": "ollama",
                    "temperature": 0.5,
                    "max_tokens": 256,
                    "timeout_seconds": 45,
                    "keep_alive_seconds": -1,
                    "ollama_options": {"num_ctx": 2048},
                },
            },
            "component_hints": {},
        },
    }
    for profile in profiles.values():
        content_hash = hashlib.sha256(json.dumps(
            {k: v for k, v in profile.items() if k != "content_hash"},
            sort_keys=True,
        ).encode()).hexdigest()[:12]
        profile["content_hash"] = content_hash
    return profiles


def get_system_setting(db: Session, key: str) -> Optional[SystemSetting]:
    return db.query(SystemSetting).filter(SystemSetting.key == key).first()


def get_setting_text(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    setting = get_system_setting(db, key)
    return setting.value if setting else default


def set_setting_text(db: Session, key: str, value: str, description: str) -> None:
    setting = get_system_setting(db, key)
    if not setting:
        setting = SystemSetting(key=key, value=value, description=description, category=SETTINGS_CATEGORY)
        db.add(setting)
    else:
        setting.value = value
        setting.description = description
        setting.category = SETTINGS_CATEGORY


def get_setting_json(db: Session, key: str, default: Optional[Any] = None) -> Any:
    raw = get_setting_text(db, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("invalid_json_system_setting", key=key)
        return default


def set_setting_json(db: Session, key: str, value: Any, description: str) -> None:
    set_setting_text(db, key, json.dumps(value, sort_keys=True), description)


def ownership_key(record_type: str, identifier: str, field_name: str) -> str:
    return f"{record_type}:{identifier}.{field_name}"


def get_ownership_map(db: Session) -> Dict[str, Dict[str, Any]]:
    return get_setting_json(db, PROFILE_OWNERSHIP_KEY, default={}) or {}


def set_ownership_map(db: Session, ownership: Dict[str, Dict[str, Any]]) -> None:
    set_setting_json(db, PROFILE_OWNERSHIP_KEY, ownership, "Tracks which profile currently manages which fields.")


def redact_endpoint(endpoint_url: Optional[str]) -> Optional[str]:
    if not endpoint_url:
        return endpoint_url
    if "@" not in endpoint_url:
        return endpoint_url
    prefix, suffix = endpoint_url.split("@", 1)
    if "://" in prefix:
        scheme, _ = prefix.split("://", 1)
        return f"{scheme}://REDACTED@{suffix}"
    return f"REDACTED@{suffix}"


def get_ollama_url(db: Session) -> str:
    return get_setting_text(db, "ollama_url", DEFAULT_OLLAMA_URL) or DEFAULT_OLLAMA_URL


def ensure_gateway_config(db: Session) -> GatewayConfig:
    config = db.query(GatewayConfig).filter(GatewayConfig.id == 1).first()
    if not config:
        config = GatewayConfig(id=1)
        db.add(config)
        db.flush()
    return config


def get_model_config_map(db: Session) -> Dict[str, ModelConfiguration]:
    return {row.model_name: row for row in db.query(ModelConfiguration).all()}


def get_backend_map(db: Session) -> Dict[str, LLMBackend]:
    return {row.model_name: row for row in db.query(LLMBackend).all()}


def config_provenance(path: str, ownership: Dict[str, Dict[str, Any]], current_value: Any, inherited: bool = False) -> str:
    entry = ownership.get(path)
    if entry:
        if entry.get("detached"):
            return "detached"
        if entry.get("managed"):
            return "profile"
    if inherited:
        return "fallback"
    if current_value not in (None, "", {}):
        return "explicit"
    return "unset"


def get_cached_or_configured_capabilities(db: Session) -> List[Dict[str, Any]]:
    capabilities = get_setting_json(db, "_oss_cached_capabilities")
    if capabilities is not None:
        return capabilities

    capabilities = []
    for backend in db.query(LLMBackend).filter(LLMBackend.enabled == True).all():
        capabilities.append({
            "backend_key": f"{backend.backend_type}:{backend.endpoint_url}",
            "backend_type": backend.backend_type,
            "endpoint_url": backend.endpoint_url,
            "display_name": backend.backend_type.title(),
            "healthy": True,
            "status": "configured",
            "supports_install_action": backend.backend_type == "ollama",
            "supports_hot_reload": backend.backend_type == "mlx",
            "models": [{
                "name": backend.model_name,
                "backend_type": backend.backend_type,
                "endpoint_url": backend.endpoint_url,
                "family": normalize_model_family(backend.model_name),
                "status": "configured",
                "installability_state": "configured",
                "supports_runtime_options": backend.backend_type in {"ollama", "mlx"},
                "supports_hot_reload": backend.backend_type == "mlx",
                "backend_key": f"{backend.backend_type}:{backend.endpoint_url}",
            }],
            "errors": [],
        })
    return capabilities


def get_active_profile_plan(db: Session, profile_name: Optional[str] = None) -> Tuple[str, Dict[str, Any], AssignmentPlan]:
    profiles = profile_registry()
    active_profile = profile_name or get_setting_text(db, ACTIVE_PROFILE_KEY)
    if not active_profile:
        raise ValueError("No active OSS profile is set.")
    if active_profile not in profiles:
        raise ValueError(f"Unknown active profile '{active_profile}'")
    capabilities = get_cached_or_configured_capabilities(db)
    return active_profile, profiles[active_profile], build_assignment_plan(active_profile, capabilities)


def desired_gateway_values(db: Session, profile_name: str, profile: Dict[str, Any], plan: AssignmentPlan) -> Dict[str, Any]:
    primary_model = next(
        (decision.selected_model for decision in plan.component_assignments.values() if decision.component_name == "intent_classifier"),
        profile.get("preferred_models", [None])[0],
    )
    primary_backend = next(
        (decision.selected_backend for decision in plan.component_assignments.values() if decision.component_name == "intent_classifier"),
        "ollama",
    )
    gateway_spec = dict(profile.get("gateway", {}))
    gateway_spec["intent_model"] = primary_model
    if primary_backend == "ollama":
        gateway_spec["ollama_fallback_url"] = get_ollama_url(db)
    return gateway_spec


def desired_component_values(component_name: str, plan: AssignmentPlan) -> Dict[str, Any]:
    decision = plan.component_assignments.get(component_name)
    if not decision or not decision.selected_model or not decision.selected_backend:
        raise ValueError(f"No profile-managed assignment is available for component '{component_name}'")
    return {
        "model_name": decision.selected_model,
        "backend_type": decision.selected_backend,
        "enabled": True,
        # Component-level tuning should normally inherit from model configuration.
        "max_tokens": None,
        "temperature": None,
        "timeout_seconds": None,
    }


def desired_model_config_values(model_name: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    config_spec = profile.get("model_configurations", {}).get(model_name) or profile.get("model_configurations", {}).get("_default")
    if not config_spec:
        raise ValueError(f"No profile-managed model configuration is available for '{model_name}'")
    return dict(config_spec)


def desired_record_values(db: Session, record_type: str, identifier: str, active_profile: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    profile_name, profile, plan = get_active_profile_plan(db, active_profile)
    if record_type == "GatewayConfig":
        return profile_name, desired_gateway_values(db, profile_name, profile, plan)
    if record_type == "ComponentModelAssignment":
        return profile_name, desired_component_values(identifier, plan)
    if record_type == "ModelConfiguration":
        return profile_name, desired_model_config_values(identifier, profile)
    raise ValueError(f"Unsupported record type '{record_type}'")


def get_or_create_target_record(db: Session, record_type: str, identifier: str) -> Any:
    if record_type == "GatewayConfig":
        if identifier != "1":
            raise ValueError("GatewayConfig reset only supports identifier '1'")
        return ensure_gateway_config(db)
    if record_type == "ComponentModelAssignment":
        assignment = db.query(ComponentModelAssignment).filter(ComponentModelAssignment.component_name == identifier).first()
        if assignment:
            return assignment
        meta = component_meta().get(identifier)
        if not meta:
            raise ValueError(f"Unknown component '{identifier}'")
        assignment = ComponentModelAssignment(
            component_name=identifier,
            display_name=meta["display_name"],
            description=meta["description"],
            category=meta["category"],
            enabled=True,
        )
        db.add(assignment)
        db.flush()
        return assignment
    if record_type == "ModelConfiguration":
        config = db.query(ModelConfiguration).filter(ModelConfiguration.model_name == identifier).first()
        if config:
            return config
        config = ModelConfiguration(model_name=identifier)
        db.add(config)
        db.flush()
        return config
    raise ValueError(f"Unsupported record type '{record_type}'")


def get_existing_target_record(db: Session, record_type: str, identifier: str) -> Any:
    if record_type == "GatewayConfig":
        if identifier != "1":
            raise ValueError("GatewayConfig detach only supports identifier '1'")
        return ensure_gateway_config(db)
    if record_type == "ComponentModelAssignment":
        return db.query(ComponentModelAssignment).filter(ComponentModelAssignment.component_name == identifier).first()
    if record_type == "ModelConfiguration":
        return db.query(ModelConfiguration).filter(ModelConfiguration.model_name == identifier).first()
    raise ValueError(f"Unsupported record type '{record_type}'")


def managed_record_fields(ownership: Dict[str, Dict[str, Any]], record_type: str, identifier: str) -> List[str]:
    prefix = f"{record_type}:{identifier}."
    return sorted(path for path in ownership if path.startswith(prefix))


def mark_detached(ownership: Dict[str, Dict[str, Any]], path: str, active_profile: str) -> None:
    ownership[path] = {
        "profile": active_profile,
        "managed": False,
        "detached": True,
        "updated_at": utc_now(),
    }


def reset_profile_field(
    db: Session,
    record_type: str,
    identifier: str,
    field_name: str,
    current_user: Optional[User] = None,
) -> Dict[str, Any]:
    active_profile, desired_values = desired_record_values(db, record_type, identifier)
    if field_name not in desired_values:
        raise ValueError(f"Field '{field_name}' is not profile-managed for {record_type}:{identifier}")

    target = get_or_create_target_record(db, record_type, identifier)
    ownership = get_ownership_map(db)
    path = ownership_key(record_type, identifier, field_name)
    previous_value = getattr(target, field_name, None)
    setattr(target, field_name, desired_values[field_name])
    ownership[path] = {
        "profile": active_profile,
        "managed": True,
        "detached": False,
        "updated_at": utc_now(),
    }
    set_ownership_map(db, ownership)

    reload_plan = compute_reload_plan([path])
    sync_state = {
        "state": "pending_reload" if reload_plan["required_action"] == "restart_required" else "synchronized",
        "required_action": reload_plan["required_action"],
        "affected_services": reload_plan["affected_services"],
        "last_failure": None,
        "updated_at": utc_now(),
    }
    set_runtime_sync_state(db, sync_state)

    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_profile_field",
            resource_id=None,
            old_value={"record_type": record_type, "identifier": identifier, "field_name": field_name, "value": previous_value},
            new_value={"record_type": record_type, "identifier": identifier, "field_name": field_name, "value": desired_values[field_name], "operation": "reset"},
        )

    db.commit()
    return {
        "status": "ok",
        "record_type": record_type,
        "identifier": identifier,
        "field_name": field_name,
        "operation": "reset",
        "active_profile": active_profile,
        "reload_plan": reload_plan,
        "runtime_sync": sync_state,
    }


def detach_profile_field(
    db: Session,
    record_type: str,
    identifier: str,
    field_name: str,
    current_user: Optional[User] = None,
) -> Dict[str, Any]:
    active_profile, _profile, _plan = get_active_profile_plan(db)
    target = get_existing_target_record(db, record_type, identifier)
    if target is None:
        raise ValueError(f"Record '{record_type}:{identifier}' does not exist")

    ownership = get_ownership_map(db)
    path = ownership_key(record_type, identifier, field_name)
    current_value = getattr(target, field_name, None)
    mark_detached(ownership, path, active_profile)
    set_ownership_map(db, ownership)

    reload_plan = compute_reload_plan([path])
    sync_state = {
        "state": "pending_reload" if reload_plan["required_action"] == "restart_required" else "synchronized",
        "required_action": reload_plan["required_action"],
        "affected_services": reload_plan["affected_services"],
        "last_failure": None,
        "updated_at": utc_now(),
    }
    set_runtime_sync_state(db, sync_state)

    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_profile_field",
            resource_id=None,
            old_value={"record_type": record_type, "identifier": identifier, "field_name": field_name, "value": current_value},
            new_value={"record_type": record_type, "identifier": identifier, "field_name": field_name, "value": current_value, "operation": "detach"},
        )

    db.commit()
    return {
        "status": "ok",
        "record_type": record_type,
        "identifier": identifier,
        "field_name": field_name,
        "operation": "detach",
        "active_profile": active_profile,
        "reload_plan": reload_plan,
        "runtime_sync": sync_state,
    }


def reset_profile_record(
    db: Session,
    record_type: str,
    identifier: str,
    current_user: Optional[User] = None,
) -> Dict[str, Any]:
    active_profile, desired_values = desired_record_values(db, record_type, identifier)
    target = get_or_create_target_record(db, record_type, identifier)
    ownership = get_ownership_map(db)
    touched: List[str] = []
    previous_state = {}

    for field_name, desired_value in desired_values.items():
        path = ownership_key(record_type, identifier, field_name)
        previous_state[field_name] = getattr(target, field_name, None)
        setattr(target, field_name, desired_value)
        ownership[path] = {
            "profile": active_profile,
            "managed": True,
            "detached": False,
            "updated_at": utc_now(),
        }
        touched.append(path)

    for path in managed_record_fields(ownership, record_type, identifier):
        field_name = path.split(".", 1)[1]
        if field_name in desired_values:
            continue
        if hasattr(target, field_name):
            previous_state.setdefault(field_name, getattr(target, field_name, None))
            setattr(target, field_name, None)
            ownership[path] = {
                "profile": active_profile,
                "managed": True,
                "detached": False,
                "updated_at": utc_now(),
            }
            touched.append(path)

    set_ownership_map(db, ownership)
    reload_plan = compute_reload_plan(touched)
    sync_state = {
        "state": "pending_reload" if reload_plan["required_action"] == "restart_required" else "synchronized",
        "required_action": reload_plan["required_action"],
        "affected_services": reload_plan["affected_services"],
        "last_failure": None,
        "updated_at": utc_now(),
    }
    set_runtime_sync_state(db, sync_state)

    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_profile_record",
            resource_id=None,
            old_value={"record_type": record_type, "identifier": identifier, "state": previous_state},
            new_value={"record_type": record_type, "identifier": identifier, "state": desired_values, "operation": "reset"},
        )

    db.commit()
    return {
        "status": "ok",
        "record_type": record_type,
        "identifier": identifier,
        "operation": "reset",
        "active_profile": active_profile,
        "reload_plan": reload_plan,
        "runtime_sync": sync_state,
        "touched_fields": len(touched),
    }


def detach_profile_record(
    db: Session,
    record_type: str,
    identifier: str,
    current_user: Optional[User] = None,
) -> Dict[str, Any]:
    active_profile, _profile, _plan = get_active_profile_plan(db)
    target = get_existing_target_record(db, record_type, identifier)
    if target is None:
        raise ValueError(f"Record '{record_type}:{identifier}' does not exist")

    ownership = get_ownership_map(db)
    touched: List[str] = []
    previous_state = {}
    for path in managed_record_fields(ownership, record_type, identifier):
        field_name = path.split(".", 1)[1]
        if hasattr(target, field_name):
            previous_state[field_name] = getattr(target, field_name, None)
        mark_detached(ownership, path, active_profile)
        touched.append(path)

    if not touched:
        raise ValueError(f"No profile-managed fields exist for {record_type}:{identifier}")

    set_ownership_map(db, ownership)
    reload_plan = compute_reload_plan(touched)
    sync_state = {
        "state": "pending_reload" if reload_plan["required_action"] == "restart_required" else "synchronized",
        "required_action": reload_plan["required_action"],
        "affected_services": reload_plan["affected_services"],
        "last_failure": None,
        "updated_at": utc_now(),
    }
    set_runtime_sync_state(db, sync_state)

    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_profile_record",
            resource_id=None,
            old_value={"record_type": record_type, "identifier": identifier, "state": previous_state},
            new_value={"record_type": record_type, "identifier": identifier, "state": previous_state, "operation": "detach"},
        )

    db.commit()
    return {
        "status": "ok",
        "record_type": record_type,
        "identifier": identifier,
        "operation": "detach",
        "active_profile": active_profile,
        "reload_plan": reload_plan,
        "runtime_sync": sync_state,
        "touched_fields": len(touched),
    }


async def collect_backend_capabilities(db: Session) -> List[Dict[str, Any]]:
    capabilities = []
    adapters = [
        OllamaAdapter(db, get_ollama_url(db)),
        MLXAdapter(db),
        OpenAICompatibleAdapter(db),
        CloudAdapter(db),
    ]
    for adapter in adapters:
        capabilities.extend([cap.to_dict() for cap in await adapter.collect()])
    return capabilities


def score_component_fit(component_name: str, model: Dict[str, Any], profile: Dict[str, Any]) -> CompatibilityScore:
    model_name = model["name"]
    family = model.get("family") or normalize_model_family(model_name)
    preferred_families = set(profile.get("preferred_families", []))
    preferred_models = set(profile.get("preferred_models", []))
    component_hints = (profile.get("component_hints", {}) or {}).get(component_name, {})
    preferred_backend_types = component_hints.get("preferred_backend_types", [])
    preferred_component_models = set(component_hints.get("preferred_models", []))
    reasons: List[str] = []

    fit_level = "compatible"
    if model.get("status") == "unavailable":
        fit_level = "incompatible"
        reasons.append("Model is unavailable on this backend.")
    elif preferred_component_models and model_name in preferred_component_models:
        fit_level = "preferred"
        reasons.append("Matches component-specific preferred model.")
    elif model_name in preferred_models:
        fit_level = "preferred"
        reasons.append("Matches profile preferred model.")
    elif family in preferred_families:
        fit_level = "compatible"
        reasons.append(f"Matches preferred model family {family}.")
    elif model.get("status") == "configured":
        fit_level = "degraded"
        reasons.append("Model is configured but not confirmed as served/installed.")
    else:
        fit_level = "degraded"
        reasons.append("Model does not match preferred family.")

    rank = FIT_WEIGHTS[fit_level]
    latency_match = model["backend_type"] in preferred_backend_order(component_name)
    if latency_match:
        rank += backend_preference_score(component_name, model["backend_type"])
    if preferred_backend_types and model["backend_type"] in preferred_backend_types:
        rank += 15
        reasons.append("Matches component-specific preferred backend.")
    if model.get("supports_runtime_options"):
        rank += 5
    if model.get("status") in {"installed", "served"}:
        rank += 10

    rationale_summary = "; ".join(reasons) or "No specific rationale available."
    return CompatibilityScore(
        rank=rank,
        fit_level=fit_level,
        reasons=reasons,
        rationale_summary=rationale_summary,
        supports_required_options=bool(model.get("supports_runtime_options", False)),
        latency_class_match=latency_match,
        model=model_name,
        backend_type=model["backend_type"],
        backend_key=model["backend_key"],
    )


def build_assignment_plan(profile_name: str, capabilities: List[Dict[str, Any]], include_disabled: bool = False) -> AssignmentPlan:
    profiles = profile_registry()
    profile = profiles[profile_name]
    component_assignments: Dict[str, AssignmentDecision] = {}
    warnings: List[str] = []
    missing_capabilities: List[str] = []

    candidates: List[Dict[str, Any]] = []
    for capability in capabilities:
        for model in capability.get("models", []):
            candidate = dict(model)
            candidate["backend_key"] = capability["backend_key"]
            candidate["backend_display_name"] = capability["display_name"]
            candidate["backend_healthy"] = capability["healthy"]
            candidates.append(candidate)

    for component_name, _display, _description, _category in DEFAULT_COMPONENTS:
        scores = [score_component_fit(component_name, candidate, profile) for candidate in candidates]
        scores.sort(key=lambda item: item.rank, reverse=True)
        valid_scores = [score for score in scores if score.fit_level != "incompatible"]
        if not valid_scores:
            decision = AssignmentDecision(
                component_name=component_name,
                selected_backend=None,
                selected_model=None,
                decision_state="misconfigured",
                selected_score=None,
                fallback_chain=[],
                rationale_summary="No compatible backend/model path found.",
                warnings=["No compatible assignment available."],
            )
            missing_capabilities.append(component_name)
        else:
            selected = valid_scores[0]
            state = "healthy" if selected.fit_level == "preferred" else "fallback_active"
            decision = AssignmentDecision(
                component_name=component_name,
                selected_backend=selected.backend_type,
                selected_model=selected.model,
                decision_state=state,
                selected_score=selected,
                fallback_chain=valid_scores[1:3],
                rationale_summary=selected.rationale_summary,
                warnings=[] if state == "healthy" else ["Using a fallback-compatible assignment."],
            )
            if state == "fallback_active":
                warnings.append(f"{component_name} is using a fallback-compatible assignment.")
        component_assignments[component_name] = decision

    states = {decision.decision_state for decision in component_assignments.values()}
    if not component_assignments:
        aggregated = "uninitialized"
    elif states == {"healthy"}:
        aggregated = "healthy"
    elif states == {"misconfigured"}:
        aggregated = "not_serving"
    elif "misconfigured" in states:
        aggregated = "partially_serving"
    else:
        aggregated = "fallback_active"

    return AssignmentPlan(
        component_assignments=component_assignments,
        aggregated_status=aggregated,
        warnings=warnings,
        missing_capabilities=missing_capabilities,
        profile_name=profile_name,
        profile_version=profile["version"],
    )


def resolve_profile_name(capabilities: List[Dict[str, Any]]) -> str:
    profiles = profile_registry()
    scored: List[Tuple[float, str]] = []
    for profile_name in profiles:
        plan = build_assignment_plan(profile_name, capabilities)
        score = 0.0
        if plan.aggregated_status == "healthy":
            score += 1000
        elif plan.aggregated_status == "fallback_active":
            score += 700
        elif plan.aggregated_status == "partially_serving":
            score += 300
        score -= len(plan.missing_capabilities) * 100
        score -= len(plan.warnings) * 10
        for decision in plan.component_assignments.values():
            if decision.selected_score:
                score += decision.selected_score.rank
        scored.append((score, profile_name))
    scored.sort(reverse=True)
    return scored[0][1] if scored else "safe_fallback"


def runtime_sync_state(db: Session) -> Dict[str, Any]:
    return get_setting_json(db, RUNTIME_SYNC_KEY, default={
        "state": "synchronized",
        "required_action": None,
        "affected_services": [],
        "last_failure": None,
        "updated_at": utc_now(),
    }) or {}


def set_runtime_sync_state(db: Session, state: Dict[str, Any]) -> None:
    set_setting_json(db, RUNTIME_SYNC_KEY, state, "Tracks whether persisted tuning changes still need runtime reload/restart.")


def record_profile_run(db: Session, payload: Dict[str, Any]) -> None:
    runs = get_setting_json(db, PROFILE_RUNS_KEY, default=[]) or []
    runs.insert(0, payload)
    runs = runs[:50]
    set_setting_json(db, PROFILE_RUNS_KEY, runs, "Recent OSS profile application history.")


def ensure_profile_registry_state(db: Session) -> None:
    profiles = profile_registry()
    state = {
        name: {
            "version": profile["version"],
            "content_hash": profile["content_hash"],
            "last_seen_at": utc_now(),
            "source_type": "shipped",
        }
        for name, profile in profiles.items()
    }
    set_setting_json(db, PROFILE_REGISTRY_STATE_KEY, state, "Tracks shipped profile versions and hashes seen by this install.")


def should_apply_field(mode: str, current_value: Any, path: str, ownership: Dict[str, Dict[str, Any]], active_profile: str) -> bool:
    if mode == "overwrite_all":
        return True
    if mode == "fill_missing_only":
        return current_value in (None, "", {})
    managed = ownership.get(path)
    if mode == "reconcile_profile":
        return current_value in (None, "", {}) or (
            managed
            and managed.get("profile") == active_profile
            and managed.get("managed")
            and not managed.get("detached")
        )
    return False


def apply_owned_field(
    target: Any,
    field_name: str,
    desired_value: Any,
    mode: str,
    ownership: Dict[str, Dict[str, Any]],
    record_type: str,
    identifier: str,
    active_profile: str,
    touched: List[str],
) -> None:
    path = ownership_key(record_type, identifier, field_name)
    current_value = getattr(target, field_name, None)
    if should_apply_field(mode, current_value, path, ownership, active_profile):
        setattr(target, field_name, desired_value)
        ownership[path] = {"profile": active_profile, "managed": True, "updated_at": utc_now()}
        touched.append(path)


def log_config_mutation(
    db: Session,
    current_user: User,
    action: str,
    resource_type: str,
    resource_id: Optional[int],
    old_value: Optional[Dict[str, Any]],
    new_value: Optional[Dict[str, Any]],
    success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    entry = AuditLog(
        user_id=current_user.id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
        success=success,
        error_message=error_message,
    )
    db.add(entry)


async def compute_profile_status(db: Session) -> Dict[str, Any]:
    ensure_profile_registry_state(db)
    capabilities = await collect_backend_capabilities(db)
    suggested_profile = resolve_profile_name(capabilities)
    active_profile = get_setting_text(db, ACTIVE_PROFILE_KEY)
    active_plan = build_assignment_plan(active_profile, capabilities) if active_profile in profile_registry() else None
    suggested_plan = build_assignment_plan(suggested_profile, capabilities)
    ownership = get_ownership_map(db)
    sync_state = runtime_sync_state(db)

    issues: List[Dict[str, Any]] = []
    per_component = []
    plan_for_status = active_plan or suggested_plan
    for capability in capabilities:
        if not capability["healthy"]:
            issues.append({
                "severity": "high",
                "code": f"{capability['backend_type']}_unreachable",
                "summary": f"{capability['display_name']} backend is not reachable.",
                "detail": capability.get("errors", [None])[0],
                "remediation": "Validate the backend endpoint or configure a fallback profile.",
            })
    for component_name, decision in plan_for_status.component_assignments.items():
        per_component.append(decision.to_dict())
        if decision.decision_state == "misconfigured":
            issues.append({
                "severity": "high",
                "code": "component_unassigned",
                "summary": f"{component_name} has no compatible assignment.",
                "detail": decision.rationale_summary,
                "remediation": "Install a compatible model or apply a different profile.",
            })
        elif decision.decision_state == "fallback_active":
            issues.append({
                "severity": "medium",
                "code": "component_fallback",
                "summary": f"{component_name} is using a fallback-compatible assignment.",
                "detail": decision.rationale_summary,
                "remediation": "Install the preferred model/backend if you want the tuned profile outcome.",
            })

    availability_state = "uninitialized" if not db.query(ComponentModelAssignment).count() else (
        "serving" if plan_for_status.aggregated_status == "healthy"
        else "partially_serving" if plan_for_status.aggregated_status in {"fallback_active", "partially_serving"}
        else "not_serving"
    )
    config_quality_state = "uninitialized" if not active_profile else (
        "healthy" if active_profile == suggested_profile and plan_for_status.aggregated_status == "healthy"
        else "fallback_active" if plan_for_status.aggregated_status == "fallback_active"
        else "misconfigured" if plan_for_status.aggregated_status in {"not_serving", "partially_serving"}
        else "untuned"
    )
    performance_risk_state = "high" if any(issue["severity"] == "high" for issue in issues) else ("medium" if issues else "low")

    profiles = []
    registry_state = get_setting_json(db, PROFILE_REGISTRY_STATE_KEY, default={}) or {}
    for profile_name, profile in profile_registry().items():
        plan = build_assignment_plan(profile_name, capabilities)
        state = registry_state.get(profile_name, {})
        installable = []
        installed = []
        unavailable = []
        for preferred in profile.get("preferred_models", []):
            matched = False
            for capability in capabilities:
                for model in capability["models"]:
                    if model["name"] == preferred:
                        matched = True
                        if model["installability_state"] == "installed" or model["status"] in {"installed", "served"}:
                            installed.append(preferred)
                        elif model["installability_state"] in {"installable", "configured"}:
                            installable.append(preferred)
                        else:
                            unavailable.append(preferred)
                        break
                if matched:
                    break
            if not matched:
                unavailable.append(preferred)
        profiles.append({
            "name": profile_name,
            "display_name": profile["display_name"],
            "description": profile["description"],
            "version": profile["version"],
            "content_hash": profile["content_hash"],
            "last_validated": profile["last_validated"],
            "recommended": profile_name == suggested_profile,
            "installed_matches": installed,
            "installable_matches": installable,
            "unavailable_matches": unavailable,
            "plan": plan.to_dict(),
            "registry_state": state,
        })

    return {
        "timestamp": utc_now(),
        "active_profile": active_profile,
        "suggested_profile": suggested_profile,
        "availability_state": availability_state,
        "config_quality_state": config_quality_state,
        "performance_risk_state": performance_risk_state,
        "backend_status": capabilities,
        "issues": issues,
        "ownership_count": len(ownership),
        "profiles": profiles,
        "assignment_plan": plan_for_status.to_dict(),
        "per_component_status": per_component,
        "runtime_sync": sync_state,
        "restart_semantics": {
            "component_models": "cache_invalidation",
            "model_configurations": "cache_invalidation",
            "gateway_config": "manual_or_hooked_restart",
        },
    }


async def compute_effective_config(db: Session) -> Dict[str, Any]:
    status = await compute_profile_status(db)
    ownership = get_ownership_map(db)
    model_configs = get_model_config_map(db)
    backends = get_backend_map(db)
    gateway = ensure_gateway_config(db)
    components = []
    assignment_map = status["assignment_plan"]["component_assignments"]
    for assignment in db.query(ComponentModelAssignment).order_by(ComponentModelAssignment.category, ComponentModelAssignment.component_name).all():
        model_config = model_configs.get(assignment.model_name) or model_configs.get("_default")
        backend = backends.get(assignment.model_name)
        component_id = assignment.component_name
        planned = assignment_map.get(component_id)
        components.append({
            "component_name": component_id,
            "display_name": assignment.display_name,
            "category": assignment.category,
            "record_type": "ComponentModelAssignment",
            "identifier": component_id,
            "planner": planned,
            "model_name": {
                "value": assignment.model_name,
                "source": config_provenance(ownership_key("ComponentModelAssignment", component_id, "model_name"), ownership, assignment.model_name),
            },
            "backend_type": {
                "value": assignment.backend_type,
                "source": config_provenance(ownership_key("ComponentModelAssignment", component_id, "backend_type"), ownership, assignment.backend_type),
            },
            "max_tokens": {
                "value": assignment.max_tokens if assignment.max_tokens is not None else (model_config.max_tokens if model_config else None),
                "source": config_provenance(ownership_key("ComponentModelAssignment", component_id, "max_tokens"), ownership, assignment.max_tokens, inherited=assignment.max_tokens is None),
            },
            "temperature": {
                "value": assignment.temperature if assignment.temperature is not None else (float(model_config.temperature) if model_config and model_config.temperature is not None else None),
                "source": config_provenance(ownership_key("ComponentModelAssignment", component_id, "temperature"), ownership, assignment.temperature, inherited=assignment.temperature is None),
            },
            "timeout_seconds": {
                "value": assignment.timeout_seconds if assignment.timeout_seconds is not None else (model_config.timeout_seconds if model_config else None),
                "source": config_provenance(ownership_key("ComponentModelAssignment", component_id, "timeout_seconds"), ownership, assignment.timeout_seconds, inherited=assignment.timeout_seconds is None),
            },
            "model_configuration": {
                "backend_endpoint": redact_endpoint(backend.endpoint_url if backend else None),
                "keep_alive_seconds": model_config.keep_alive_seconds if model_config else None,
                "ollama_options": (model_config.ollama_options or {}) if model_config else {},
                "mlx_options": (model_config.mlx_options or {}) if model_config else {},
                "source": config_provenance(ownership_key("ModelConfiguration", assignment.model_name, "ollama_options"), ownership, (model_config.ollama_options if model_config else None), inherited=model_config is None),
            },
        })

    return {
        "generated_at": utc_now(),
        "active_profile": status.get("active_profile"),
        "suggested_profile": status.get("suggested_profile"),
        "assignment_plan": status.get("assignment_plan"),
        "runtime_sync": status.get("runtime_sync"),
        "gateway": {
            "record_type": "GatewayConfig",
            "identifier": "1",
            "intent_model": {
                "value": gateway.intent_model,
                "source": config_provenance(ownership_key("GatewayConfig", "1", "intent_model"), ownership, gateway.intent_model),
            },
            "intent_temperature": {
                "value": gateway.intent_temperature,
                "source": config_provenance(ownership_key("GatewayConfig", "1", "intent_temperature"), ownership, gateway.intent_temperature),
            },
            "intent_max_tokens": {
                "value": gateway.intent_max_tokens,
                "source": config_provenance(ownership_key("GatewayConfig", "1", "intent_max_tokens"), ownership, gateway.intent_max_tokens),
            },
            "intent_timeout_seconds": {
                "value": gateway.intent_timeout_seconds,
                "source": config_provenance(ownership_key("GatewayConfig", "1", "intent_timeout_seconds"), ownership, gateway.intent_timeout_seconds),
            },
            "ollama_fallback_url": redact_endpoint(gateway.ollama_fallback_url),
        },
        "components": components,
        "models": [
            {
                "model_name": model_name,
                "record_type": "ModelConfiguration",
                "identifier": model_name,
                "backend_type": config.backend_type,
                "enabled": config.enabled,
                "max_tokens": config.max_tokens,
                "timeout_seconds": config.timeout_seconds,
                "keep_alive_seconds": config.keep_alive_seconds,
                "ollama_options": config.ollama_options or {},
                "mlx_options": config.mlx_options or {},
                "source": config_provenance(ownership_key("ModelConfiguration", model_name, "ollama_options"), ownership, config.ollama_options),
            }
            for model_name, config in sorted(model_configs.items())
        ],
    }


def compute_reload_plan(touched_paths: List[str]) -> Dict[str, Any]:
    affected_services = []
    required_action = "none"
    if any(path.startswith("ComponentModelAssignment:") or path.startswith("ModelConfiguration:") for path in touched_paths):
        required_action = "invalidate_cache"
        affected_services.append("orchestrator")
    if any(path.startswith("GatewayConfig:") for path in touched_paths):
        required_action = "restart_required"
        affected_services.extend(["gateway"])
    return {
        "required_action": required_action,
        "affected_services": sorted(set(affected_services)),
        "hook_mode": RESTART_HOOK_MODE,
        "automation_supported": required_action == "invalidate_cache" or RESTART_HOOK_MODE != "manual",
    }


async def preview_profile(db: Session, profile_name: str, mode: str) -> Dict[str, Any]:
    capabilities = await collect_backend_capabilities(db)
    plan = build_assignment_plan(profile_name, capabilities)
    ownership = get_ownership_map(db)
    changes = []
    current_assignments = {row.component_name: row for row in db.query(ComponentModelAssignment).all()}
    for component_name, decision in plan.component_assignments.items():
        current = current_assignments.get(component_name)
        if not current or current.model_name != decision.selected_model or current.backend_type != decision.selected_backend:
            changes.append({
                "component_name": component_name,
                "current_model": current.model_name if current else None,
                "current_backend": current.backend_type if current else None,
                "planned_model": decision.selected_model,
                "planned_backend": decision.selected_backend,
                "decision_state": decision.decision_state,
                "rationale_summary": decision.rationale_summary,
            })
    reload_plan = compute_reload_plan([f"ComponentModelAssignment:{c['component_name']}.model_name" for c in changes])
    return {
        "profile_name": profile_name,
        "mode": mode,
        "profile_version": profile_registry()[profile_name]["version"],
        "profile_content_hash": profile_registry()[profile_name]["content_hash"],
        "assignment_plan": plan.to_dict(),
        "changes": changes,
        "fallback_substitutions": [warning for warning in plan.warnings],
        "reload_plan": reload_plan,
        "ownership_count": len(ownership),
    }


def apply_profile(db: Session, profile_name: str, mode: str, current_user: Optional[User] = None) -> Dict[str, Any]:
    profiles = profile_registry()
    if profile_name not in profiles:
        raise ValueError(f"Unknown profile '{profile_name}'")
    if mode not in {"fill_missing_only", "reconcile_profile", "overwrite_all"}:
        raise ValueError(f"Unsupported mode '{mode}'")

    previous_active_profile = get_setting_text(db, ACTIVE_PROFILE_KEY)
    capabilities = get_cached_or_configured_capabilities(db)
    plan = build_assignment_plan(profile_name, capabilities)
    profile = profiles[profile_name]
    ownership = get_ownership_map(db)
    touched: List[str] = []

    gateway = ensure_gateway_config(db)
    primary_model = next((decision.selected_model for decision in plan.component_assignments.values() if decision.component_name == "intent_classifier"), profile.get("preferred_models", [None])[0])
    primary_backend = next((decision.selected_backend for decision in plan.component_assignments.values() if decision.component_name == "intent_classifier"), "ollama")
    gateway_spec = dict(profile.get("gateway", {}))
    gateway_spec["intent_model"] = primary_model
    for field_name, desired in gateway_spec.items():
        apply_owned_field(gateway, field_name, desired, mode, ownership, "GatewayConfig", "1", profile_name, touched)
    if primary_backend == "ollama":
        apply_owned_field(gateway, "ollama_fallback_url", get_ollama_url(db), mode, ownership, "GatewayConfig", "1", profile_name, touched)

    backend_map = get_backend_map(db)
    model_config_map = get_model_config_map(db)
    current_assignments = {row.component_name: row for row in db.query(ComponentModelAssignment).all()}
    meta = component_meta()

    for component_name, decision in plan.component_assignments.items():
        if not decision.selected_model or not decision.selected_backend:
            continue
        assignment = current_assignments.get(component_name)
        if not assignment:
            assignment = ComponentModelAssignment(
                component_name=component_name,
                display_name=meta[component_name]["display_name"],
                description=meta[component_name]["description"],
                category=meta[component_name]["category"],
                enabled=True,
                model_name=decision.selected_model,
                backend_type=decision.selected_backend,
            )
            db.add(assignment)
            db.flush()

        apply_owned_field(assignment, "model_name", decision.selected_model, mode, ownership, "ComponentModelAssignment", component_name, profile_name, touched)
        apply_owned_field(assignment, "backend_type", decision.selected_backend, mode, ownership, "ComponentModelAssignment", component_name, profile_name, touched)
        apply_owned_field(assignment, "enabled", True, mode, ownership, "ComponentModelAssignment", component_name, profile_name, touched)

        backend = backend_map.get(decision.selected_model)
        if not backend:
            endpoint_url = get_ollama_url(db) if decision.selected_backend == "ollama" else None
            backend = LLMBackend(
                model_name=decision.selected_model,
                backend_type=decision.selected_backend,
                endpoint_url=endpoint_url or "",
                enabled=True,
                priority=50,
                max_tokens=4096,
                temperature_default=0.4,
                timeout_seconds=60,
                keep_alive_seconds=-1,
                description=f"Seeded from OSS profile {profile_name}",
            )
            db.add(backend)
            db.flush()

        config_spec = profile.get("model_configurations", {}).get(decision.selected_model) or profile.get("model_configurations", {}).get("_default")
        if config_spec:
            config = model_config_map.get(decision.selected_model)
            if not config:
                config = ModelConfiguration(model_name=decision.selected_model)
                db.add(config)
                db.flush()
            for field_name, desired in config_spec.items():
                apply_owned_field(config, field_name, desired, mode, ownership, "ModelConfiguration", decision.selected_model, profile_name, touched)

    set_ownership_map(db, ownership)
    set_setting_text(db, ACTIVE_PROFILE_KEY, profile_name, "Currently active OSS initialization profile.")
    last_run = {
        "last_action": "apply_profile",
        "mode": mode,
        "profile_name": profile_name,
        "profile_version": profile["version"],
        "profile_content_hash": profile["content_hash"],
        "touched_fields": len(touched),
        "applied_at": utc_now(),
        "assignment_plan": plan.to_dict(),
    }
    set_setting_json(db, PROFILE_STATUS_KEY, last_run, "Tracks the last OSS profile application run.")
    set_setting_text(db, PROFILE_LAST_RUN_KEY, utc_now(), "Timestamp of the last OSS profile action.")
    record_profile_run(db, last_run)

    reload_plan = compute_reload_plan(touched)
    if reload_plan["required_action"] == "restart_required":
        set_runtime_sync_state(db, {
            "state": "pending_reload",
            "required_action": reload_plan["required_action"],
            "affected_services": reload_plan["affected_services"],
            "last_failure": None,
            "updated_at": utc_now(),
        })
    else:
        set_runtime_sync_state(db, {
            "state": "synchronized",
            "required_action": reload_plan["required_action"],
            "affected_services": reload_plan["affected_services"],
            "last_failure": None,
            "updated_at": utc_now(),
        })

    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_profile",
            resource_id=None,
            old_value={"active_profile": previous_active_profile},
            new_value={"profile_name": profile_name, "mode": mode, "reload_plan": reload_plan, "plan_status": plan.aggregated_status},
        )

    db.commit()
    return {
        "status": "ok",
        "profile_name": profile_name,
        "mode": mode,
        "profile_version": profile["version"],
        "primary_model": primary_model,
        "touched_fields": len(touched),
        "reload_plan": reload_plan,
        "assignment_plan": plan.to_dict(),
    }


async def retry_runtime_sync(db: Session, current_user: Optional[User] = None) -> Dict[str, Any]:
    state = runtime_sync_state(db)
    required_action = state.get("required_action")
    result = {"previous_state": state, "attempted_at": utc_now()}
    if required_action == "invalidate_cache":
        state = {
            "state": "synchronized",
            "required_action": None,
            "affected_services": [],
            "last_failure": None,
            "updated_at": utc_now(),
        }
    elif required_action == "restart_required" and RESTART_HOOK_MODE == "manual":
        state["last_failure"] = "Manual restart hook mode is active; operator restart still required."
        state["updated_at"] = utc_now()
    else:
        state["last_failure"] = "No automated restart hook available."
        state["updated_at"] = utc_now()
    set_runtime_sync_state(db, state)
    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_runtime_sync",
            resource_id=None,
            old_value=result["previous_state"],
            new_value=state,
        )
    db.commit()
    result["runtime_sync"] = state
    return result


async def install_ollama_model(db: Session, model_name: str, current_user: Optional[User] = None) -> Dict[str, Any]:
    ollama_url = get_ollama_url(db)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(f"{ollama_url}/api/pull", json={"name": model_name, "stream": False})
        response.raise_for_status()
        payload = response.json()
    if current_user:
        log_config_mutation(
            db,
            current_user=current_user,
            action="update",
            resource_type="oss_model_install",
            resource_id=None,
            old_value=None,
            new_value={"model_name": model_name, "ollama_url": redact_endpoint(ollama_url)},
        )
        db.commit()
    return {"status": "ok", "model_name": model_name, "result": payload}
