"""Guard: names, addresses, locations and phone numbers never reach a log call.

Scans every logger call in src/, apps/ and admin/backend/app/ for a
house-derived or caller-identity value: a keyword whose name is one of the
keys below, or an identifier anywhere inside a keyword value or positional
argument (f-strings, %/+ formatting, ``or``/``if`` expressions, tuples,
lists, dicts including ``extra={...}`` keys, ``str()``/``repr()``,
``.format()`` and the receivers of method calls). Log presence or ids
instead: ``has_guest_name=bool(x)``, ``guest_id=``, ``phone_last4=x[-4:]``,
``location_set=True``.

A finding is one logger call. The comparison is by count per
(path, enclosing function) against ALLOWLIST, so a new site anywhere,
including inside an allowlisted function, fails. Two reasons are allowed:
the admin audit actor (the signed-in operator's own username/email) and a
small set of ticketed RAG location logs in images this change doesn't
rebuild.

Stdlib only: this runs on the unit-min CI requirements.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "apps", "admin/backend/app")
SKIP = {
    "src/jetson/athena_lite_llm.py": "PEP 701 f-string at :134; not a runtime image, parse fails on 3.11",
}

HOUSE_KEYS = (
    "guest_name", "owner_name", "home_address", "address", "phone", "phone_number", "from_number",
    "guest_email", "guest_phone", "speaker_first_name", "location", "location_override",
)
CALLER_KEYS = ("email", "identity", "username", "display_name")
_KEY_TOKENS = tuple(tuple(k.split("_")) for k in HOUSE_KEYS + CALLER_KEYS)

_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "msg", "log", "bind"})
_EXEMPT_CALLS = frozenset({"bool", "len"})

OPERATOR_AUDIT = "operator-audit: the signed-in operator's own identity, logged as the audit actor"
DEFERRED = "deferred-ticketed: search location logged by a RAG image this change doesn't rebuild (follow-up)"

# (path, enclosing function) -> (count, reason)
ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("admin/backend/app/auth/oidc.py", "get_or_create_user"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/database.py", "seed_dev_data"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/alerts.py", "acknowledge_all_alerts"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/alerts.py", "delete_alert"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/alerts.py", "update_alert"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/audit.py", "list_audit_logs"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/audit.py", "undo_audit_action"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "bulk_create_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "create_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "delete_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "get_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "update_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "create_calendar_source"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "delete_calendar_source"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "get_calendar_source"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "sync_all_calendar_sources"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "test_calendar_source"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "test_ical_url"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "update_calendar_source"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/cloud_llm_usage.py", "purge_old_usage"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/cloud_providers.py", "remove_api_key"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/cloud_providers.py", "setup_provider"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/component_models.py", "toggle_component_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/component_models.py", "update_component_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "create_sports_team"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "delete_sports_team"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_clarification_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_clarification_type"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_conversation_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_device_rule"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_sports_team"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversations.py", "evaluate_turn"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/dashboard.py", "get_dashboard_data"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/devices.py", "create_device"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/devices.py", "delete_device"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/devices.py", "update_device"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/emerging_intents.py", "merge_intents"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/emerging_intents.py", "promote_intent"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/emerging_intents.py", "reject_intent"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "activate_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "cancel_override"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "clone_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "create_override"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "create_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "create_rule"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "delete_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "delete_rule"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "toggle_rule"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "update_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/escalation.py", "update_rule"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/external_api_keys.py", "create_external_api_key"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/external_api_keys.py", "delete_external_api_key"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/external_api_keys.py", "update_external_api_key"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "get_feature"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "get_feature_impact"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "get_what_if_scenarios"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "list_features"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "toggle_feature"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "update_feature"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/features.py", "update_feature_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/gateway_config.py", "reset_gateway_config"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/gateway_config.py", "update_gateway_config"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/guest_mode.py", "create_guest_mode_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/guest_mode.py", "get_guest_mode_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/guest_mode.py", "update_guest_mode_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/guests.py", "create_guest"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/guests.py", "delete_guest"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/guests.py", "update_guest"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/ha_pipelines.py", "set_preferred_pipeline"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/hallucination_checks.py", "create_hallucination_check"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/hallucination_checks.py", "delete_hallucination_check"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/hallucination_checks.py", "update_hallucination_check"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "create_intent_pattern"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "create_intent_routing"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "create_provider_routing"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "delete_intent_pattern"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "delete_intent_routing"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "delete_provider_routing"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "get_intent_patterns"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "get_intent_routing"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "get_provider_routing"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "toggle_strategy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "update_intent_pattern"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "update_intent_routing"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "update_provider_routing"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/intent_routing.py", "update_strategy_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "create_backend"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "delete_backend"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "get_backend"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "get_metrics"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "list_backends"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "toggle_backend"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/llm_backends.py", "update_backend"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/local_auth.py", "local_login"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "add_allowed_domain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "add_blocked_domain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "get_mcp_security"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "list_pending_approvals"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "remove_allowed_domain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "remove_blocked_domain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "review_tool_approval"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/mcp_security.py", "update_mcp_security"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/memories.py", "create_guest_session"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/memories.py", "create_memory"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/memories.py", "delete_memory"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/memories.py", "promote_memory"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/memories.py", "update_memory"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/memories.py", "update_memory_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "apply_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "create_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "delete_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "get_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "list_configs"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "toggle_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/model_config.py", "update_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/multi_intent.py", "create_intent_chain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/multi_intent.py", "delete_intent_chain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/multi_intent.py", "update_intent_chain"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/multi_intent.py", "update_multi_intent_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/performance_presets.py", "activate_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/performance_presets.py", "capture_current_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/performance_presets.py", "create_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/performance_presets.py", "delete_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/performance_presets.py", "duplicate_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/performance_presets.py", "update_preset"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "create_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "delete_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "rollback_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "update_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_connectors.py", "create_connector"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_connectors.py", "delete_connector"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_connectors.py", "test_connector"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_connectors.py", "update_connector"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_service_bypass.py", "delete_bypass_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_service_bypass.py", "toggle_bypass"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_service_bypass.py", "update_bypass_config"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "add_alias"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "add_member"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "create_room_group"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "delete_room_group"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "get_room_group"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "remove_alias"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "remove_member"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/room_groups.py", "update_room_group"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/secrets.py", "create_secret"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/secrets.py", "delete_secret"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/secrets.py", "reveal_secret"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/secrets.py", "update_secret"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/service_control.py", "_execute_resolved_action"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/service_control.py", "load_ollama_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/service_control.py", "restart_service_by_port"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/service_control.py", "start_service_by_port"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/service_control.py", "stop_service_by_port"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/service_control.py", "unload_ollama_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/services.py", "delete_service"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/services.py", "register_service"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/services.py", "update_service"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "get_oidc_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_assistant_profile"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_directions_origin_placeholders"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_house_layout_settings"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_llm_memory_settings"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_oidc_settings"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_ollama_url"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_privacy_settings"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/settings.py", "save_tool_proposal_settings"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/site_scraper.py", "update_config"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/sms.py", "send_sms_manually"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/routes/sms.py", "update_sms_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "add_tool_api_key_requirement"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "create_tool"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "delete_tool"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "delete_tool_api_key_requirement"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "discover_mcp_tools"): (8, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "get_aggregated_metrics"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "get_mcp_status"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "get_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "get_tool"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "get_tool_api_key_requirements"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "get_tool_stats"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "list_available_api_keys"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "list_metrics"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "list_tools"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "list_tools_with_api_keys"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "list_triggers"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "refresh_tool_registry"): (4, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "toggle_tool"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "toggle_tool_by_name"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "toggle_trigger"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "update_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "update_tool"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "update_tool_api_key_requirement"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_calling.py", "update_trigger"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_proposals.py", "approve_tool_proposal"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_proposals.py", "delete_tool_proposal"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/tool_proposals.py", "reject_tool_proposal"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/user_api_keys.py", "revoke_api_key"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/user_sessions.py", "delete_session"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "change_my_password"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "create_user"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "deactivate_user"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "reactivate_user"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "reset_local_user_password"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "update_user"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/validation_models.py", "create_validation_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/validation_models.py", "delete_validation_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/validation_models.py", "update_validation_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_automations.py", "delete_automation"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_config.py", "restart_voice_services_proxy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_config.py", "set_active_stt_model"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_config.py", "set_active_tts_voice"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_config.py", "update_voice_service"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_interfaces.py", "create_voice_interface"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_interfaces.py", "delete_voice_interface"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_interfaces.py", "get_voice_interface"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_interfaces.py", "list_voice_interfaces"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_interfaces.py", "update_voice_interface"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_tests.py", "save_test_feedback"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_tests.py", "test_full_pipeline"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_tests.py", "test_llm_processing"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_tests.py", "test_rag_query"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_tests.py", "test_speech_to_text"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/voice_tests.py", "test_text_to_speech"): (1, OPERATOR_AUDIT),
    ("src/rag/onecall/main.py", "geocode_location"): (2, DEFERRED),
    ("src/rag/serpapi_events/main.py", "local_events_endpoint"): (1, DEFERRED),
    ("src/rag/serpapi_events/main.py", "search_events_endpoint"): (1, DEFERRED),
    ("src/rag/serpapi_events/main.py", "search_google_events"): (1, DEFERRED),
    ("src/rag/weather/main.py", "geocode_location"): (3, DEFERRED),
}


def _matched_keys(identifier: str) -> set[str]:
    tokens = tuple(t for t in identifier.lower().split("_") if t)
    return {"_".join(key) for key in _KEY_TOKENS if len(tokens) >= len(key) and tokens[-len(key):] == key}


def _matches(identifier: str) -> bool:
    return bool(_matched_keys(identifier))


# Which keys each allowlist reason may cover: an operator-audit entry may
# log only the operator's own caller identity, a deferred entry only the
# search location. Any other key inside an allowlisted call still fails.
REASON_KEYS = {
    OPERATOR_AUDIT: frozenset(CALLER_KEYS),
    DEFERRED: frozenset({"location"}),
}


def _final_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _final_name(node.func)
    return ""


def _is_logger_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _LOG_METHODS
        and "log" in _final_name(func.value).lower()
    )


def _is_negative_tail_slice(node: ast.Subscript) -> bool:
    sl = node.slice
    return (
        isinstance(sl, ast.Slice)
        and sl.upper is None
        and isinstance(sl.lower, ast.UnaryOp)
        and isinstance(sl.lower.op, ast.USub)
    )


def _exempt(node: ast.expr) -> bool:
    if isinstance(node, ast.Compare):
        return True
    if isinstance(node, ast.Subscript) and _is_negative_tail_slice(node):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _EXEMPT_CALLS


def _identifiers(node: ast.AST):
    """Every identifier a logged value can carry, walking sub-expressions."""
    if node is None or _exempt(node):
        return
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            yield key.value
        yield from _identifiers(node.value)
    elif isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                yield node.args[0].value
            yield from _identifiers(func.value)
        for arg in node.args:
            yield from _identifiers(arg)
        for kw in node.keywords:
            yield from _identifiers(kw.value)
    elif isinstance(node, ast.Dict):
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                yield key.value
            elif key is not None:
                yield from _identifiers(key)
        for value in node.values:
            yield from _identifiers(value)
    elif isinstance(node, (ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.BoolOp, ast.IfExp, ast.Tuple,
                           ast.List, ast.Set, ast.UnaryOp, ast.Starred)):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                yield from _identifiers(child)


def _call_hits(call: ast.Call) -> list[str]:
    hits = []
    for kw in call.keywords:
        if kw.arg and _matches(kw.arg) and not _exempt(kw.value):
            hits.append(kw.arg)
        hits.extend(i for i in _identifiers(kw.value) if _matches(i))
    for arg in call.args:
        hits.extend(i for i in _identifiers(arg) if _matches(i))
    return hits


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.calls = 0
        self.found: list[tuple[str, int, list[str]]] = []

    def _visit_function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if _is_logger_call(node):
            self.calls += 1
            hits = _call_hits(node)
            if hits:
                self.found.append((self.stack[-1] if self.stack else "<module>", node.lineno, sorted(set(hits))))
        self.generic_visit(node)


def find_pii_logs(source: str, filename: str = "<string>") -> tuple[list[tuple[str, int, list[str]]], int]:
    finder = _Finder()
    finder.visit(ast.parse(source, filename=filename))
    return finder.found, finder.calls


def _scan_files() -> list[Path]:
    files = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = path.relative_to(REPO_ROOT).parts
            if "tests" in parts or "node_modules" in parts:
                continue
            files.append(path)
    return files


def scan() -> tuple[Counter, int, int, list[str]]:
    found, parsed, calls, lines, _keys = _scan()
    return found, parsed, calls, lines


def _scan():
    found: Counter = Counter()
    keys_by_pair: dict[tuple[str, str], set[str]] = {}
    parsed = calls = 0
    lines: list[str] = []
    for path in _scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SKIP:
            continue
        try:
            hits, n = find_pii_logs(path.read_text(encoding="utf-8"), rel)
        except SyntaxError as exc:
            raise AssertionError(f"{rel} doesn't parse ({exc}); fix it or add it to SKIP with a reason") from exc
        parsed += 1
        calls += n
        for func, lineno, keys in hits:
            found[(rel, func)] += 1
            keys_by_pair.setdefault((rel, func), set()).update(k for i in keys for k in _matched_keys(i))
            lines.append(f"{rel}:{lineno} ({func}) {','.join(keys)}")
    return found, parsed, calls, lines, keys_by_pair


def reason_violations(keys_by_pair, allowlist) -> dict:
    """Allowlisted pairs that log a key their reason doesn't cover."""
    out = {}
    for pair, (_count, reason) in allowlist.items():
        extra = keys_by_pair.get(pair, set()) - REASON_KEYS[reason]
        if extra:
            out[pair] = sorted(extra)
    return out


def test_reason_violations_self_test():
    allowlist = {("a.py", "f"): (1, OPERATOR_AUDIT), ("b.py", "g"): (1, DEFERRED)}
    assert reason_violations({("a.py", "f"): {"username"}, ("b.py", "g"): {"location"}}, allowlist) == {}
    assert reason_violations({("a.py", "f"): {"username", "guest_name"}}, allowlist) == {("a.py", "f"): ["guest_name"]}
    assert reason_violations({("b.py", "g"): {"phone_number"}}, allowlist) == {("b.py", "g"): ["phone_number"]}


def test_log_calls_match_the_allowlist():
    found, parsed, calls, lines, keys_by_pair = _scan()
    assert parsed >= 230, f"only {parsed} files parsed; the scan roots moved"
    assert calls >= 2900, f"only {calls} logger calls found; the logger-call detector broke"
    expected = Counter({key: count for key, (count, _reason) in ALLOWLIST.items()})
    assert sum(expected.values()) >= 200
    new = found - expected
    gone = expected - found
    assert not new and not gone, (
        "logged PII drifted from the allowlist.\n"
        f"New (log presence or an id instead): {dict(new)}\n"
        f"Gone (drop from ALLOWLIST): {dict(gone)}\n" + "\n".join(lines)
    )
    assert ("src/orchestrator/main.py", "tool_call_node") not in found
    assert not reason_violations(keys_by_pair, ALLOWLIST)


def test_every_allowlist_entry_has_a_reason():
    for key, (count, reason) in ALLOWLIST.items():
        assert count >= 1 and reason in (OPERATOR_AUDIT, DEFERRED), key
        if reason == OPERATOR_AUDIT:
            assert key[0].startswith("admin/backend/app/"), key


def test_pii_detector_self_test():
    source = '''
import logging

def flagged(g, a, o, n, user, booking, request, location, current_user, ip_address, _tool_owner_name, owner_name, home_address):
    logger.info("x", guest_name=g)
    logger.info(f"{home_address}")
    self.logger.warning("%s", user.phone_number)
    logging.getLogger(__name__).error("a " + booking.guest_name)
    log.bind(speaker_first_name=n)
    logger.info("x", extra={"address": a})
    logger.info("{}".format(owner_name))
    logger.info("x", name=_tool_owner_name)
    logger.info(f"{request.location}")
    logger.info(location=location)
    logger.info(f"in {location or 'x'}")
    LOGGER.info(f"{location}")
    logger.info("u", user=current_user.username)
    logger.info("x", location_override=o)
    logger.info("x", ip=ip_address)
    logger.info(f"{request.location.lower()}")

def not_flagged(p, g, guest_name, location):
    logger.info("x", phone_last4=p[-4:])
    logger.info("x", has_guest_name=bool(g))
    logger.info("x", n=len(guest_name))
    logger.info("x", location_set=location is not None)
'''
    found, calls = find_pii_logs(source)
    assert [line for _func, line, _keys in found] == list(range(5, 21))
    assert all(func == "flagged" for func, _line, _keys in found)
    assert calls == 20
    assert by_line_keys(found)[20] == ["location"]
    by_line = by_line_keys(found)
    assert by_line[18] == ["location_override"]
    assert by_line[19] == ["ip_address"]
    assert _matched_keys("current_user_username") == {"username"}
    assert _matched_keys("guest_name_len") == set()


def by_line_keys(found):
    return {line: keys for _func, line, keys in found}
