"""Unit tests for orchestrator.nodes._runtime.

Tests round-trip set/get for every singleton and cache accessor, the public
readiness API (is_ready / missing_required), test-mode helpers
(enable_strict_test_mode / reset_for_test), and the close_all() shutdown path.

Note: async tests use asyncio.run() directly rather than pytest-asyncio to
avoid introducing a new test dependency in this phase.
"""
import asyncio
import sys
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Insert src/ so `orchestrator.*` imports resolve without the package installed.
sys.path.insert(0, "src")

from orchestrator.nodes import _runtime


# ---------------------------------------------------------------------------
# Fixture: always start with a clean runtime
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_runtime():
    """Reset runtime before (and after) every test."""
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake(name: str) -> MagicMock:
    m = MagicMock()
    m.__repr__ = lambda self: f"<fake {name}>"
    return m


# ---------------------------------------------------------------------------
# 16 service singleton round-trips
# ---------------------------------------------------------------------------

def test_ha_client_roundtrip():
    fake = _make_fake("ha_client")
    _runtime.set_ha_client(fake)
    assert _runtime.get_ha_client() is fake


def test_llm_router_roundtrip():
    fake = _make_fake("llm_router")
    _runtime.set_llm_router(fake)
    assert _runtime.get_llm_router() is fake


def test_cache_client_roundtrip():
    fake = _make_fake("cache_client")
    _runtime.set_cache_client(fake)
    assert _runtime.get_cache_client() is fake


def test_session_manager_roundtrip():
    fake = _make_fake("session_manager")
    _runtime.set_session_manager(fake)
    assert _runtime.get_session_manager() is fake


def test_rag_client_roundtrip():
    fake = _make_fake("rag_client")
    _runtime.set_rag_client(fake)
    assert _runtime.get_rag_client() is fake


def test_parallel_search_engine_roundtrip():
    fake = _make_fake("parallel_search_engine")
    _runtime.set_parallel_search_engine(fake)
    assert _runtime.get_parallel_search_engine() is fake


def test_result_fusion_roundtrip():
    fake = _make_fake("result_fusion")
    _runtime.set_result_fusion(fake)
    assert _runtime.get_result_fusion() is fake


def test_mode_client_roundtrip():
    fake = _make_fake("mode_client")
    _runtime.set_mode_client(fake)
    assert _runtime.get_mode_client() is fake


def test_entity_manager_roundtrip():
    fake = _make_fake("entity_manager")
    _runtime.set_entity_manager(fake)
    assert _runtime.get_entity_manager() is fake


def test_smart_controller_roundtrip():
    fake = _make_fake("smart_controller")
    _runtime.set_smart_controller(fake)
    assert _runtime.get_smart_controller() is fake


def test_sequence_executor_roundtrip():
    fake = _make_fake("sequence_executor")
    _runtime.set_sequence_executor(fake)
    assert _runtime.get_sequence_executor() is fake


def test_automation_agent_roundtrip():
    fake = _make_fake("automation_agent")
    _runtime.set_automation_agent(fake)
    assert _runtime.get_automation_agent() is fake


def test_music_handler_roundtrip():
    fake = _make_fake("music_handler")
    _runtime.set_music_handler(fake)
    assert _runtime.get_music_handler() is fake


def test_tv_handler_roundtrip():
    fake = _make_fake("tv_handler")
    _runtime.set_tv_handler(fake)
    assert _runtime.get_tv_handler() is fake


def test_follow_me_service_roundtrip():
    fake = _make_fake("follow_me_service")
    _runtime.set_follow_me_service(fake)
    assert _runtime.get_follow_me_service() is fake


def test_intent_classifier_roundtrip():
    fake = _make_fake("intent_classifier")
    _runtime.set_intent_classifier(fake)
    assert _runtime.get_intent_classifier() is fake


# ---------------------------------------------------------------------------
# 5 cache accessors + memory_context
# ---------------------------------------------------------------------------

def test_component_model_cache_default_is_empty_dict():
    cache = _runtime.get_component_model_cache()
    assert isinstance(cache, dict)
    assert cache == {}


def test_component_model_cache_is_mutable_in_place():
    cache = _runtime.get_component_model_cache()
    cache["key"] = {"value": 1}
    assert _runtime.get_component_model_cache() == {"key": {"value": 1}}


def test_orch_feature_flag_cache_roundtrip():
    cache = _runtime.get_orch_feature_flag_cache()
    cache["feat"] = True
    assert _runtime.get_orch_feature_flag_cache()["feat"] is True


def test_tool_schema_cache_roundtrip():
    cache = _runtime.get_tool_schema_cache()
    cache["schema"] = [{"type": "object"}]
    assert _runtime.get_tool_schema_cache()["schema"] == [{"type": "object"}]


def test_tool_config_cache_roundtrip():
    cache = _runtime.get_tool_config_cache()
    cache["cfg"] = {"timeout": 30}
    assert _runtime.get_tool_config_cache()["cfg"] == {"timeout": 30}


def test_origin_placeholder_cache_roundtrip():
    cache = _runtime.get_origin_placeholder_cache()
    cache["origin"] = "home"
    assert _runtime.get_origin_placeholder_cache()["origin"] == "home"


def test_memory_context_roundtrip():
    ctx = _runtime.get_memory_context()
    ctx["session_1"] = {"context": {}, "expires_at": 9999.0}
    assert _runtime.get_memory_context()["session_1"]["expires_at"] == 9999.0


# ---------------------------------------------------------------------------
# is_ready() and missing_required()
# ---------------------------------------------------------------------------

def _install_all_required():
    """Install a fake for every required (unconditionally-initialized) singleton.

    Only the 6 singletons that are always initialized regardless of HA config.
    HA-conditioned singletons (ha_client, entity_manager, smart_controller,
    sequence_executor, automation_agent, music_handler, tv_handler,
    follow_me_service) are intentionally NOT set here — they are optional from
    the K8s readiness perspective.
    """
    _runtime.set_llm_router(_make_fake("llm_router"))
    _runtime.set_cache_client(_make_fake("cache_client"))
    _runtime.set_session_manager(_make_fake("session_manager"))
    _runtime.set_rag_client(_make_fake("rag_client"))
    _runtime.set_mode_client(_make_fake("mode_client"))
    _runtime.set_intent_classifier(_make_fake("intent_classifier"))


def test_is_ready_false_when_nothing_set():
    assert _runtime.is_ready() is False


def test_is_ready_false_when_only_some_set():
    _runtime.set_llm_router(_make_fake("llm_router"))
    assert _runtime.is_ready() is False


def test_is_ready_true_when_all_required_set():
    _install_all_required()
    assert _runtime.is_ready() is True


def test_is_ready_true_without_optional_singletons():
    # parallel_search_engine, result_fusion, and all HA-conditioned singletons
    # are NOT required — installing only the 6 unconditional singletons is enough
    _install_all_required()
    # Optional slots remain None
    assert _runtime.get_parallel_search_engine() is None
    assert _runtime.get_result_fusion() is None
    assert _runtime.get_ha_client() is None
    assert _runtime.get_entity_manager() is None
    assert _runtime.is_ready() is True


def test_missing_required_returns_all_names_when_nothing_set():
    missing = _runtime.missing_required()
    assert set(missing) == set(_runtime._REQUIRED_SINGLETONS)


def test_missing_required_returns_subset_when_some_set():
    # Set two required singletons; ha_client is NOT required (HA-conditioned)
    _runtime.set_llm_router(_make_fake("llm_router"))
    _runtime.set_cache_client(_make_fake("cache_client"))
    missing = _runtime.missing_required()
    assert "llm_router" not in missing
    assert "cache_client" not in missing
    # ha_client is optional — setting it doesn't affect required count
    assert "ha_client" not in _runtime._REQUIRED_SINGLETONS
    # All other required singletons still missing
    assert len(missing) == len(_runtime._REQUIRED_SINGLETONS) - 2


def test_missing_required_returns_empty_when_all_set():
    _install_all_required()
    assert _runtime.missing_required() == []


# BLOCK 2 — regression guard tests (per ian gate, Phase 1.2)

def test_is_ready_true_with_only_unconditional_singletons():
    """K8s readiness must pass with ONLY the 6 unconditional singletons set.

    HA-conditioned slots (ha_client, entity_manager, smart_controller,
    sequence_executor, automation_agent, music_handler, tv_handler,
    follow_me_service) and optional slots (parallel_search_engine,
    result_fusion) must remain None and their getters must return None —
    they must not block readiness.  This guards against the regression where
    including HA-conditioned names in _REQUIRED_SINGLETONS caused
    is_ready() to return False forever in no-HA OSS deployments.
    """
    _install_all_required()  # Only the 6 unconditional singletons

    assert _runtime.is_ready() is True

    # All HA-conditioned slots stay None
    for slot in (
        "ha_client", "entity_manager", "smart_controller",
        "sequence_executor", "automation_agent", "music_handler",
        "tv_handler", "follow_me_service",
    ):
        assert getattr(_runtime._ctx, slot) is None, f"{slot} should be None"

    # Their getters also return None
    assert _runtime.get_ha_client() is None
    assert _runtime.get_entity_manager() is None
    assert _runtime.get_smart_controller() is None
    assert _runtime.get_sequence_executor() is None
    assert _runtime.get_automation_agent() is None
    assert _runtime.get_music_handler() is None
    assert _runtime.get_tv_handler() is None
    assert _runtime.get_follow_me_service() is None

    # Optional singletons also None
    assert _runtime.get_parallel_search_engine() is None
    assert _runtime.get_result_fusion() is None


def test_missing_required_returns_unset_required_only():
    """missing_required() lists only truly-required singletons still unset.

    Install 5 of the 6 required singletons (omit intent_classifier); assert
    missing_required() returns exactly ["intent_classifier"] and is_ready()
    is False.  HA-conditioned slots being None must not appear in the list.
    """
    _runtime.set_llm_router(_make_fake("llm_router"))
    _runtime.set_cache_client(_make_fake("cache_client"))
    _runtime.set_session_manager(_make_fake("session_manager"))
    _runtime.set_rag_client(_make_fake("rag_client"))
    _runtime.set_mode_client(_make_fake("mode_client"))
    # intent_classifier intentionally omitted

    missing = _runtime.missing_required()
    assert missing == ["intent_classifier"]
    assert _runtime.is_ready() is False

    # HA-conditioned slots are None but must NOT appear in missing_required()
    assert "ha_client" not in missing
    assert "entity_manager" not in missing
    assert "follow_me_service" not in missing


def test_is_ready_indifferent_to_optional_singletons():
    """Optional singleton presence/absence must not change is_ready() outcome.

    Step 1: only the 6 required singletons → is_ready() is True.
    Step 2: also install ha_client, parallel_search_engine → still True.
    Confirms neither the HA-conditioned nor the search-optional slots gate
    readiness in either direction.
    """
    _install_all_required()
    assert _runtime.is_ready() is True  # Step 1

    # Step 2: add optional singletons
    _runtime.set_ha_client(_make_fake("ha_client"))
    _runtime.set_parallel_search_engine(_make_fake("parallel_search_engine"))
    _runtime.set_result_fusion(_make_fake("result_fusion"))
    _runtime.set_entity_manager(_make_fake("entity_manager"))

    assert _runtime.is_ready() is True  # Still True — optionals don't gate


# ---------------------------------------------------------------------------
# reset_for_test() — clears both _ctx and strict-mode flag
# ---------------------------------------------------------------------------

def test_reset_for_test_clears_singletons():
    _runtime.set_ha_client(_make_fake("ha_client"))
    _runtime.set_llm_router(_make_fake("llm_router"))
    _runtime.reset_for_test()
    assert _runtime.get_ha_client() is None
    assert _runtime.get_llm_router() is None


def test_reset_for_test_clears_caches():
    _runtime.get_component_model_cache()["k"] = "v"
    _runtime.reset_for_test()
    assert _runtime.get_component_model_cache() == {}


def test_reset_for_test_clears_strict_mode():
    _runtime.enable_strict_test_mode()
    _runtime.reset_for_test()
    # After reset, reading an unset slot should NOT warn
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _runtime.get_ha_client()
    assert len(w) == 0


# ---------------------------------------------------------------------------
# enable_strict_test_mode() — uninitialized reads issue RuntimeWarning
# ---------------------------------------------------------------------------

def test_strict_mode_warns_on_uninitialized_read():
    _runtime.enable_strict_test_mode()
    with pytest.warns(RuntimeWarning, match="ha_client read before lifespan initialization"):
        _runtime.get_ha_client()


def test_strict_mode_warns_for_each_unset_required_singleton():
    _runtime.enable_strict_test_mode()
    names = [
        ("llm_router", _runtime.get_llm_router),
        ("cache_client", _runtime.get_cache_client),
        ("session_manager", _runtime.get_session_manager),
        ("rag_client", _runtime.get_rag_client),
        ("mode_client", _runtime.get_mode_client),
        ("entity_manager", _runtime.get_entity_manager),
        ("smart_controller", _runtime.get_smart_controller),
        ("sequence_executor", _runtime.get_sequence_executor),
        ("automation_agent", _runtime.get_automation_agent),
        ("music_handler", _runtime.get_music_handler),
        ("tv_handler", _runtime.get_tv_handler),
        ("follow_me_service", _runtime.get_follow_me_service),
        ("intent_classifier", _runtime.get_intent_classifier),
    ]
    for name, getter in names:
        with pytest.warns(RuntimeWarning, match=f"{name} read before lifespan initialization"):
            getter()


def test_strict_mode_no_warn_after_set():
    _runtime.enable_strict_test_mode()
    _runtime.set_ha_client(_make_fake("ha_client"))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _runtime.get_ha_client()
    assert len(w) == 0


def test_optional_singletons_do_not_warn_in_strict_mode():
    """parallel_search_engine and result_fusion are nullable by design."""
    _runtime.enable_strict_test_mode()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _runtime.get_parallel_search_engine()
        _runtime.get_result_fusion()
    runtime_warnings = [x for x in w if issubclass(x.category, RuntimeWarning)]
    assert len(runtime_warnings) == 0


# ---------------------------------------------------------------------------
# close_all() — calls each client's close exactly once
# ---------------------------------------------------------------------------

def _fake_with_close_only(close_method_name: str) -> MagicMock:
    """Build a mock that only exposes one close-style method.

    Uses spec= so that getattr(mock, "aclose", None) returns None for methods
    not in the spec — preventing MagicMock's auto-attribute-creation from
    tricking the method-resolution order in close_all().
    """
    m = MagicMock(spec=[close_method_name])
    setattr(m, close_method_name, AsyncMock())
    return m


def test_close_all_calls_close_on_each_managed_client():
    """close_all() must call each registered client's close method exactly once."""
    fakes = {
        # close() clients
        "ha_client": _fake_with_close_only("close"),
        "llm_router": _fake_with_close_only("close"),
        "cache_client": _fake_with_close_only("close"),
        "session_manager": _fake_with_close_only("close"),
        "rag_client": _fake_with_close_only("close"),
        # mode_client uses aclose() (httpx convention)
        "mode_client": _fake_with_close_only("aclose"),
        # parallel_search_engine uses close_all()
        "parallel_search_engine": _fake_with_close_only("close_all"),
    }

    _runtime.set_ha_client(fakes["ha_client"])
    _runtime.set_llm_router(fakes["llm_router"])
    _runtime.set_cache_client(fakes["cache_client"])
    _runtime.set_session_manager(fakes["session_manager"])
    _runtime.set_rag_client(fakes["rag_client"])
    _runtime.set_mode_client(fakes["mode_client"])
    _runtime.set_parallel_search_engine(fakes["parallel_search_engine"])

    asyncio.run(_runtime.close_all())

    fakes["ha_client"].close.assert_called_once()
    fakes["llm_router"].close.assert_called_once()
    fakes["cache_client"].close.assert_called_once()
    fakes["session_manager"].close.assert_called_once()
    fakes["rag_client"].close.assert_called_once()
    fakes["mode_client"].aclose.assert_called_once()
    fakes["parallel_search_engine"].close_all.assert_called_once()


def test_close_all_skips_none_clients():
    """close_all() must not raise when some managed slots are None."""
    # Only set some clients; the rest remain None after reset_for_test()
    _runtime.set_ha_client(MagicMock(close=AsyncMock()))
    # All others left as None
    asyncio.run(_runtime.close_all())  # Should not raise


def test_close_all_continues_after_client_error():
    """close_all() logs but does not raise when a client's close raises."""
    failing = _fake_with_close_only("close")
    failing.close.side_effect = RuntimeError("boom")
    ok = _fake_with_close_only("close")

    _runtime.set_ha_client(failing)
    _runtime.set_llm_router(ok)

    asyncio.run(_runtime.close_all())  # Should not raise

    ok.close.assert_called_once()


def test_close_all_picks_aclose_over_close_all_over_close():
    """Method resolution: aclose wins over close_all wins over close."""
    # Client with all three methods — aclose should be the one called
    client = MagicMock(
        aclose=AsyncMock(),
        close_all=AsyncMock(),
        close=AsyncMock(),
    )
    _runtime.set_ha_client(client)
    asyncio.run(_runtime.close_all())

    client.aclose.assert_called_once()
    client.close_all.assert_not_called()
    client.close.assert_not_called()


def test_close_all_picks_close_all_when_no_aclose():
    """Method resolution: close_all wins when no aclose present."""
    client = MagicMock(spec=["close_all", "close"])
    client.close_all = AsyncMock()
    client.close = AsyncMock()

    _runtime.set_ha_client(client)
    asyncio.run(_runtime.close_all())

    client.close_all.assert_called_once()
    client.close.assert_not_called()
