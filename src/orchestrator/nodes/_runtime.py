"""Runtime context for pipeline nodes.

Singletons are initialized by main.py's lifespan and read by node modules at
call time (NOT import time). Caches that survive across requests live here.

Invariant: every getter returns the current value at call time. Setters are
called from lifespan() exactly once per process; tests call setters directly
to install fakes.

This module contains NO business logic — it is purely a setter/getter facade
over typed module-level slots. Adding business logic here is a code smell that
recreates the god-object boundary.
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Forward-only imports to avoid circularity with orchestrator.main
    from shared.ha_client import HomeAssistantClient
    from shared.llm_router import LLMRouter

logger = logging.getLogger(__name__)


@dataclass
class RuntimeContext:
    """Typed container for the 16 service singletons + 5 caches.

    Slots are Optional because lifespan initializes them; tests install fakes.
    Reading a None slot in production is a programmer error and should be
    caught by the post-lifespan readiness assertion (see lifespan epilogue).
    """
    # 16 service singletons
    ha_client: Optional["HomeAssistantClient"] = None
    llm_router: Optional["LLMRouter"] = None
    cache_client: Optional[Any] = None
    session_manager: Optional[Any] = None
    rag_client: Optional[Any] = None
    parallel_search_engine: Optional[Any] = None
    result_fusion: Optional[Any] = None
    mode_client: Optional[Any] = None
    entity_manager: Optional[Any] = None
    smart_controller: Optional[Any] = None
    sequence_executor: Optional[Any] = None
    automation_agent: Optional[Any] = None
    music_handler: Optional[Any] = None
    tv_handler: Optional[Any] = None
    follow_me_service: Optional[Any] = None
    intent_classifier: Optional[Any] = None
    # 5 caches (Dict[str, Any] — populated at runtime; tests may pre-populate)
    component_model_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    orch_feature_flag_cache: Dict[str, Any] = field(default_factory=dict)
    tool_schema_cache: Dict[str, Any] = field(default_factory=dict)
    tool_config_cache: Dict[str, Any] = field(default_factory=dict)
    origin_placeholder_cache: Dict[str, Any] = field(default_factory=dict)
    # Conversation fallback (in-memory when Redis unavailable)
    memory_context: Dict[str, Any] = field(default_factory=dict)


# _ctx is private; readers use is_ready() / missing_required() instead.
_ctx: RuntimeContext = RuntimeContext()
_strict_warn_in_test_mode: bool = False

# Required singletons: only the 6 unconditionally-initialized singletons.
# HA-conditioned singletons (ha_client, entity_manager, smart_controller,
# sequence_executor, automation_agent, music_handler, tv_handler,
# follow_me_service) are omitted — they are only initialized when an HA token
# is present, so no-HA OSS deployments would block K8s readiness forever if
# they were included here.  parallel_search_engine and result_fusion are also
# excluded — they are conditionally initialized (lifespan may leave them None).
_REQUIRED_SINGLETONS: tuple = (
    "llm_router",
    "cache_client",
    "session_manager",
    "rag_client",
    "mode_client",
    "intent_classifier",
)


# ---------------------------------------------------------------------------
# Service singleton setters / getters (16 pairs)
# ---------------------------------------------------------------------------

def set_ha_client(c: Any) -> None:
    _ctx.ha_client = c

def get_ha_client() -> Optional[Any]:
    if _ctx.ha_client is None and _strict_warn_in_test_mode:
        warnings.warn("ha_client read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.ha_client


def set_llm_router(c: Any) -> None:
    _ctx.llm_router = c

def get_llm_router() -> Optional[Any]:
    if _ctx.llm_router is None and _strict_warn_in_test_mode:
        warnings.warn("llm_router read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.llm_router


def set_cache_client(c: Any) -> None:
    _ctx.cache_client = c

def get_cache_client() -> Optional[Any]:
    if _ctx.cache_client is None and _strict_warn_in_test_mode:
        warnings.warn("cache_client read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.cache_client


def set_session_manager(c: Any) -> None:
    _ctx.session_manager = c

def get_session_manager() -> Optional[Any]:
    if _ctx.session_manager is None and _strict_warn_in_test_mode:
        warnings.warn("session_manager read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.session_manager


def set_rag_client(c: Any) -> None:
    _ctx.rag_client = c

def get_rag_client() -> Optional[Any]:
    if _ctx.rag_client is None and _strict_warn_in_test_mode:
        warnings.warn("rag_client read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.rag_client


def set_parallel_search_engine(c: Any) -> None:
    _ctx.parallel_search_engine = c

def get_parallel_search_engine() -> Optional[Any]:
    # parallel_search_engine is nullable by design; no strict warning
    return _ctx.parallel_search_engine


def set_result_fusion(c: Any) -> None:
    _ctx.result_fusion = c

def get_result_fusion() -> Optional[Any]:
    # result_fusion is nullable by design; no strict warning
    return _ctx.result_fusion


def set_mode_client(c: Any) -> None:
    _ctx.mode_client = c

def get_mode_client() -> Optional[Any]:
    if _ctx.mode_client is None and _strict_warn_in_test_mode:
        warnings.warn("mode_client read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.mode_client


def set_entity_manager(c: Any) -> None:
    _ctx.entity_manager = c

def get_entity_manager() -> Optional[Any]:
    if _ctx.entity_manager is None and _strict_warn_in_test_mode:
        warnings.warn("entity_manager read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.entity_manager


def set_smart_controller(c: Any) -> None:
    _ctx.smart_controller = c

def get_smart_controller() -> Optional[Any]:
    if _ctx.smart_controller is None and _strict_warn_in_test_mode:
        warnings.warn("smart_controller read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.smart_controller


def set_sequence_executor(c: Any) -> None:
    _ctx.sequence_executor = c

def get_sequence_executor() -> Optional[Any]:
    if _ctx.sequence_executor is None and _strict_warn_in_test_mode:
        warnings.warn("sequence_executor read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.sequence_executor


def set_automation_agent(c: Any) -> None:
    _ctx.automation_agent = c

def get_automation_agent() -> Optional[Any]:
    if _ctx.automation_agent is None and _strict_warn_in_test_mode:
        warnings.warn("automation_agent read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.automation_agent


def set_music_handler(c: Any) -> None:
    _ctx.music_handler = c

def get_music_handler() -> Optional[Any]:
    if _ctx.music_handler is None and _strict_warn_in_test_mode:
        warnings.warn("music_handler read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.music_handler


def set_tv_handler(c: Any) -> None:
    _ctx.tv_handler = c

def get_tv_handler() -> Optional[Any]:
    if _ctx.tv_handler is None and _strict_warn_in_test_mode:
        warnings.warn("tv_handler read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.tv_handler


def set_follow_me_service(c: Any) -> None:
    _ctx.follow_me_service = c

def get_follow_me_service() -> Optional[Any]:
    if _ctx.follow_me_service is None and _strict_warn_in_test_mode:
        warnings.warn("follow_me_service read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.follow_me_service


def set_intent_classifier(c: Any) -> None:
    _ctx.intent_classifier = c

def get_intent_classifier() -> Optional[Any]:
    if _ctx.intent_classifier is None and _strict_warn_in_test_mode:
        warnings.warn("intent_classifier read before lifespan initialization", RuntimeWarning, stacklevel=2)
    return _ctx.intent_classifier


# ---------------------------------------------------------------------------
# Cache accessors (5 caches + memory_context)
# ---------------------------------------------------------------------------

def get_component_model_cache() -> Dict[str, Dict[str, Any]]:
    return _ctx.component_model_cache

def get_orch_feature_flag_cache() -> Dict[str, Any]:
    return _ctx.orch_feature_flag_cache

def get_tool_schema_cache() -> Dict[str, Any]:
    return _ctx.tool_schema_cache

def get_tool_config_cache() -> Dict[str, Any]:
    return _ctx.tool_config_cache

def get_origin_placeholder_cache() -> Dict[str, Any]:
    return _ctx.origin_placeholder_cache

def get_memory_context() -> Dict[str, Any]:
    return _ctx.memory_context


# ---------------------------------------------------------------------------
# Public readiness API (R2-M2)
# ---------------------------------------------------------------------------

def is_ready() -> bool:
    """Return True iff every required singleton has been set non-None."""
    return all(getattr(_ctx, name) is not None for name in _REQUIRED_SINGLETONS)


def missing_required() -> List[str]:
    """Return the names of required singletons that are still None."""
    return [name for name in _REQUIRED_SINGLETONS if getattr(_ctx, name) is None]


# ---------------------------------------------------------------------------
# Test helpers (R2-M2)
# ---------------------------------------------------------------------------

def reset_for_test() -> None:
    """Clear ALL state — context AND strict-mode flag. Called by test fixtures.

    Resets both _ctx AND _strict_warn_in_test_mode to prevent test-isolation
    leaks of warning behavior across tests.
    """
    global _ctx, _strict_warn_in_test_mode
    _ctx = RuntimeContext()
    _strict_warn_in_test_mode = False


def enable_strict_test_mode() -> None:
    """In tests, reading an uninitialized slot issues a RuntimeWarning."""
    global _strict_warn_in_test_mode
    _strict_warn_in_test_mode = True


# ---------------------------------------------------------------------------
# Shutdown (R2-H1 Pattern A — sole shutdown path after Phase 1.2)
# ---------------------------------------------------------------------------

async def close_all() -> None:
    """Shut down all clients with .close()/.aclose()/close_all().

    This is the SOLE shutdown path for managed clients after Phase 1.2.
    Lifespan no longer calls explicit close() on each client — close_all()
    owns it atomically to avoid any double-close window.

    Close method resolution order per client: aclose → close_all → close.
    The first callable method found is used; only one is called per client.

    admin_client is NOT managed here — it is a per-call client from
    shared.admin_config and is closed explicitly in lifespan if present.

    Clients that ARE closed by close_all():
      - cache_client.close()               (shared.cache)
      - session_manager.close()            (orchestrator.session_manager)
      - rag_client.close()                 (orchestrator.rag_client)
      - mode_client.aclose()               (httpx.AsyncClient convention)
      - parallel_search_engine.close_all() (orchestrator.search_providers.parallel_search)

    Clients in the close_order that have NO close method (silently skipped):
      - ha_client  — HomeAssistantClient has no close(); its underlying
                     httpx.AsyncClient is leaked at shutdown (pre-existing
                     OSS behavior; tracked separately).
      - llm_router — LLMRouter has no close() method (pre-existing).
    """
    close_order = (
        "ha_client",
        "llm_router",
        "cache_client",
        "session_manager",
        "rag_client",
        "mode_client",
        "parallel_search_engine",
    )
    for slot_name in close_order:
        client = getattr(_ctx, slot_name, None)
        if client is None:
            continue
        # Pick the right close method per client.
        # mode_client uses aclose() (httpx convention);
        # parallel_search_engine uses close_all();
        # others use close().
        for method_name in ("aclose", "close_all", "close"):
            method = getattr(client, method_name, None)
            if callable(method):
                try:
                    result = method()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    # Log but don't raise — shutdown is best-effort.
                    logger.warning(
                        "runtime_close_failed",
                        extra={"slot": slot_name, "method": method_name, "err": str(exc)},
                    )
                break  # Only call one close-style method per client
