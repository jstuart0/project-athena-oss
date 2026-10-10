"""Guard: names, addresses, locations, phone numbers and whole request
payloads never reach a log call or an audit row.

Scans every logger call, and every ``AuditLog(...)`` constructor, in src/,
apps/ and admin/backend/app/ for a house-derived or caller-identity value:
a keyword whose name is one of the keys below, or an identifier anywhere
inside a keyword value or positional argument (f-strings, %/+ formatting,
``or``/``if`` expressions, tuples, lists, dicts including ``extra={...}``
keys, ``str()``/``repr()``, ``.format()`` and the receivers of method
calls). Also flagged: a whole payload (``update_data``, tool ``arguments``/
``args``, ``changes``, ``payload``, a ``.model_dump()``/``.dict()``), which
can carry any of those values, and ``.name`` on a guest/booking/member/
participant. Log presence, ids or key names instead:
``has_guest_name=bool(x)``, ``guest_id=``, ``phone_last4=x[-4:]``,
``location_set=True``, ``changed_fields=sorted(update_data.keys())``,
``arg_keys=payload_keys(arguments)``; audit rows go through
``redact_phone_fields(...)``.

A finding is one logger call. The comparison is by count per
(path, enclosing function) against ALLOWLIST, so a new site anywhere,
including inside an allowlisted function, fails. Two reasons are allowed:
the admin audit actor (the signed-in operator's own username/email) and a
small set of ticketed RAG location logs in images this change doesn't
rebuild.

User and assistant text: a head slice ``x[:N]`` / ``x[0:N]`` (literal
N > 4, no step) anywhere in a logged value is a finding, whatever it's
called, unless the sliced value's own name is an id, sid, hash or digest
(``session_id[:8]``). A ``*_preview`` name is a finding too, and so is any
logger call in the semantic cache that mentions ``cache_key`` (the key is
built from the query). Log a length instead: ``query_len=len(query)``,
``error_type=type(e).__name__``. Text findings take no allowlist reason.

Stdlib only: this runs on the unit-min CI requirements.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "apps", "admin/backend/app", "admin/backend/main.py")
SKIP = {
    "src/jetson/athena_lite_llm.py": "a syntax error at :134 (an unclosed call); not a runtime image, parses on no Python version",
}

HOUSE_KEYS = (
    "guest_name", "owner_name", "home_address", "address", "phone", "phone_number", "from_number", "to_number",
    "guest_email", "guest_phone", "speaker_first_name", "location", "location_override",
)
CALLER_KEYS = ("email", "identity", "username", "display_name")
TEXT_KEYS = ("preview",)
_KEY_TOKENS = tuple(tuple(k.split("_")) for k in HOUSE_KEYS + CALLER_KEYS + TEXT_KEYS)

_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "msg", "log", "bind"})
_AUDIT_SINKS = frozenset({"AuditLog"})
_EXEMPT_CALLS = frozenset({"bool", "len", "redact_phone_fields", "payload_keys"})
_MAX_TAIL_SLICE = 4
# Whole payloads: any of these can carry every key above.
PAYLOAD_NAMES = frozenset({"update_data", "arguments", "args", "tool_args", "changes", "payload"})
_PAYLOAD_METHODS = frozenset({"model_dump", "dict"})
_PERSON_RECEIVERS = ("guest", "booking", "member", "participant", "automation")
# Whole user or assistant text, logged unsliced: any value whose own name is
# one of these (``query=query``, ``text=response.text``, ``message.message``).
FULL_TEXT_NAMES = frozenset({"query", "text", "message", "body", "answer", "utterance", "transcript"})
# A head slice of more than this many characters is text, unless the sliced
# value is named as an identifier.
_MAX_HEAD_SLICE = 4
_ID_NAME = re.compile(r"(^|_)(id|sid|hash|digest|hexdigest|sha|sha256)$")
# The semantic cache builds cache_key from the query text.
CACHE_KEY_FILE = "src/orchestrator/semantic_cache.py"

OPERATOR_AUDIT = "operator-audit: the signed-in operator's own identity (and client IP), logged as the audit actor"
DEFERRED = "deferred-ticketed: search location logged by a RAG image this change doesn't rebuild (follow-up)"

# (path, enclosing function) -> (count, reason)
ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("admin/backend/app/auth/oidc.py", "get_or_create_user"): (2, OPERATOR_AUDIT),
    ("admin/backend/app/database.py", "seed_dev_data"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/alerts.py", "acknowledge_all_alerts"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/alerts.py", "delete_alert"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/alerts.py", "update_alert"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/audit.py", "list_audit_logs"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/audit.py", "undo_audit_action"): (3, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "bulk_create_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "create_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "delete_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "_audit"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "get_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/base_knowledge.py", "update_base_knowledge"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/calendar_sources.py", "_audit"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/conversation.py", "create_audit_log"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "create_sports_team"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "delete_sports_team"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_clarification_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_clarification_type"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_conversation_settings"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_device_rule"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversation.py", "update_sports_team"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/conversations.py", "evaluate_turn"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/dashboard.py", "get_dashboard_data"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/devices.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/guest_mode.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/policies.py", "create_audit_log"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "create_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "delete_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "rollback_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/policies.py", "update_policy"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/rag_connectors.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/secrets.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/services.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/telemetry.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/app/routes/user_api_keys.py", "create_audit_log"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/user_api_keys.py", "revoke_api_key"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/user_sessions.py", "delete_session"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "change_my_password"): (1, OPERATOR_AUDIT),
    ("admin/backend/app/routes/users.py", "create_audit_log"): (1, OPERATOR_AUDIT),
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
    ("admin/backend/main.py", "auth_callback"): (1, OPERATOR_AUDIT),
}


# Head-slice text logs deferred to a follow-up (ATHENA-169). Empty: every
# site in an image this change rebuilds was fixed, and the text rule takes
# no allowlist reason. An entry is allowed only outside those images (see
# test_frozen_text_sites_are_outside_rebuilt_images), with its count and
# the reason it can't be fixed yet.
# (path, enclosing function) -> (count, reason)
FROZEN_TEXT_SITES: dict[tuple[str, str], tuple[int, str]] = {}
REBUILT_IMAGE_ROOTS = ("src/orchestrator", "src/gateway", "src/shared", "src/sms", "apps/jarvis-web", "admin/backend/app",
                       "admin/backend/main.py")

# Whole-text logs (the full_text rule) not fixed here, each with its reason:
# - NOT_REBUILT: in an image or host process this change doesn't rebuild
#   (RAG services, the Control Agent, the Jetson service); ATHENA-169.
# - NOT_TEXT: reviewed, the value is developer-authored (an exception's
#   message, a config warning), not user or assistant text.
# Entries may log only full_text:* identifiers.
NOT_REBUILT = "deferred (ATHENA-169): whole text logged by an image this change doesn't rebuild"
NOT_TEXT = "reviewed: developer-authored message (exception or config warning), not user or assistant text"
FROZEN_FULL_TEXT_SITES: dict[tuple[str, str], tuple[int, str]] = {
    ("admin/backend/app/services/telemetry/sender.py", "_warn_once"): (1, NOT_TEXT),
    ("src/shared/errors.py", "athena_exception_handler"): (1, NOT_TEXT),
    ("src/control_agent/huggingface.py", "download_task"): (1, NOT_REBUILT),
    ("src/control_agent/huggingface.py", "search_models"): (1, NOT_REBUILT),
    ("src/control_agent/main.py", "watchdog_loop"): (2, NOT_REBUILT),
    ("src/jetson/llm_webhook_service.py", "conversation"): (1, NOT_REBUILT),
    ("src/rag/airports/main.py", "search_airports_api"): (1, NOT_REBUILT),
    ("src/rag/brightdata/main.py", "search"): (1, NOT_REBUILT),
    ("src/rag/brightdata/main.py", "web_search"): (1, NOT_REBUILT),
    ("src/rag/community_events/main.py", "search_events_endpoint"): (1, NOT_REBUILT),
    ("src/rag/news/main.py", "search_articles"): (2, NOT_REBUILT),
    ("src/rag/news/main.py", "search_news"): (2, NOT_REBUILT),
    ("src/rag/news/main.py", "search_newsapiai"): (1, NOT_REBUILT),
    ("src/rag/news/main.py", "search_webz"): (1, NOT_REBUILT),
    ("src/rag/price_compare/main.py", "aggregate_prices"): (2, NOT_REBUILT),
    ("src/rag/price_compare/main.py", "search_prices"): (2, NOT_REBUILT),
    ("src/rag/price_compare/providers/rapidapi.py", "search"): (2, NOT_REBUILT),
    ("src/rag/price_compare/providers/webscraper.py", "search"): (2, NOT_REBUILT),
    ("src/rag/recipes/main.py", "search_recipes"): (1, NOT_REBUILT),
    ("src/rag/seatgeek_events/main.py", "search_events_endpoint"): (2, NOT_REBUILT),
    ("src/rag/seatgeek_events/main.py", "search_seatgeek_events"): (1, NOT_REBUILT),
    ("src/rag/site_scraper/main.py", "search_and_scrape"): (1, NOT_REBUILT),
    ("src/rag/sports/main.py", "resolve_team_alias"): (1, NOT_REBUILT),
    ("src/rag/sports/main.py", "search_teams_parallel"): (3, NOT_REBUILT),
    ("src/rag/websearch/main.py", "news_search"): (1, NOT_REBUILT),
    ("src/rag/websearch/main.py", "search"): (2, NOT_REBUILT),
    ("src/rag/websearch/main.py", "search_news"): (2, NOT_REBUILT),
    ("src/rag/websearch/main.py", "web_search"): (1, NOT_REBUILT),
}


def _matched_keys(identifier: str) -> set[str]:
    tokens = tuple(t for t in identifier.lower().split("_") if t)
    return {"_".join(key) for key in _KEY_TOKENS if len(tokens) >= len(key) and tokens[-len(key):] == key}


def _is_phone_name(identifier: str) -> bool:
    """Any name that starts with ``phone`` (phone, phone_e164, phone_raw...)."""
    tokens = [t for t in identifier.lower().split("_") if t]
    return bool(tokens) and tokens[0] == "phone" and tokens[-1] != "last4"


def _matches(identifier: str) -> bool:
    return (bool(_matched_keys(identifier)) or identifier in PAYLOAD_NAMES or _is_phone_name(identifier)
            or identifier.startswith(("person_name:", "payload:", "text_slice:", "full_text:")))


# Which keys each allowlist reason may cover: an operator-audit entry may
# log only the operator's own caller identity, a deferred entry only the
# search location. Any other key inside an allowlisted call still fails.
REASON_KEYS = {
    OPERATOR_AUDIT: frozenset(CALLER_KEYS),
    DEFERRED: frozenset({"location"}),
}
# Exact identifiers a reason also covers: AuditLog's ip_address column holds
# the operator's own client address.
REASON_IDENTIFIERS = {
    OPERATOR_AUDIT: frozenset({"ip_address"}),
    # The same deferred RAG search calls log the query text too (the RAG
    # images aren't rebuilt by this change; see FROZEN_FULL_TEXT_SITES).
    DEFERRED: frozenset({"full_text:query"}),
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
    if isinstance(func, ast.Name) and func.id in _AUDIT_SINKS:
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _LOG_METHODS
        and "log" in _final_name(func.value).lower()
    )


def _is_short_tail_slice(node: ast.Subscript) -> bool:
    """``x[-N:]`` with a literal N of at most 4 (a phone's last four)."""
    sl = node.slice
    return (
        isinstance(sl, ast.Slice)
        and sl.upper is None
        and sl.step is None
        and isinstance(sl.lower, ast.UnaryOp)
        and isinstance(sl.lower.op, ast.USub)
        and isinstance(sl.lower.operand, ast.Constant)
        and isinstance(sl.lower.operand.value, int)
        and 1 <= sl.lower.operand.value <= _MAX_TAIL_SLICE
    )


def _head_slice_upper(node: ast.Subscript):
    """N for ``x[:N]`` / ``x[0:N]`` with a literal int N and no step, else None."""
    sl = node.slice
    if not isinstance(sl, ast.Slice) or sl.step is not None:
        return None
    if sl.lower is not None and not (isinstance(sl.lower, ast.Constant) and sl.lower.value == 0):
        return None
    if isinstance(sl.upper, ast.Constant) and isinstance(sl.upper.value, int) and not isinstance(sl.upper.value, bool):
        return sl.upper.value
    return None


def _text_slices(node: ast.AST, inspected: list):
    """``text_slice:<name>`` for every head slice of more than four
    characters in a logged value, unless the sliced value is named as an id.
    Exempt sub-expressions (len(), bool(), comparisons...) aren't entered.
    ``inspected`` counts every head slice seen, exempt ones included."""
    if node is None or (isinstance(node, ast.expr) and _exempt(node)):
        return
    if isinstance(node, ast.Subscript):
        upper = _head_slice_upper(node)
        if upper is not None:
            inspected.append(1)
            name = _final_name(node.value)
            if upper > _MAX_HEAD_SLICE and not _ID_NAME.search(name.lower()):
                yield f"text_slice:{name or '?'}"
    for child in ast.iter_child_nodes(node):
        yield from _text_slices(child, inspected)


def _exempt(node: ast.expr) -> bool:
    if isinstance(node, ast.Compare):
        return True
    if isinstance(node, ast.Subscript) and _is_short_tail_slice(node):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "keys":
        return True
    # A hash of a value isn't the value (hashlib...(query).hexdigest()).
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("hexdigest", "digest"):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _EXEMPT_CALLS


def _identifiers(node: ast.AST):
    """Every identifier a logged value can carry, walking sub-expressions."""
    if node is None or _exempt(node):
        return
    if isinstance(node, ast.Name):
        if node.id in FULL_TEXT_NAMES:
            yield f"full_text:{node.id}"
        yield node.id
    elif isinstance(node, ast.Attribute):
        receiver = _final_name(node.value).lower()
        if node.attr == "name" and any(p in receiver for p in _PERSON_RECEIVERS):
            yield f"person_name:{receiver}"
        if node.attr in FULL_TEXT_NAMES:
            yield f"full_text:{node.attr}"
        yield node.attr
    elif isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            yield key.value
        yield from _identifiers(node.value)
    elif isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _PAYLOAD_METHODS:
            yield f"payload:{func.attr}"
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


def _name_lookups(node: ast.AST):
    """Every ``x.get("name")`` / ``x["name"]`` in a logged value, not
    entering exempt sub-expressions (len(), bool(), ...)."""
    if node is None or (isinstance(node, ast.expr) and _exempt(node)):
        return
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "name"):
        yield node
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "name":
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _name_lookups(child)


def _person_context(call: ast.Call, func: str):
    """The person-ish subject of a log call (its event string, else its
    enclosing function), or None."""
    event = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str) else ""
    for text in (event, func):
        for receiver in _PERSON_RECEIVERS:
            if receiver in text.lower():
                return receiver
    return None


def _call_hits(call: ast.Call, *, filename: str = "", inspected: list | None = None, func: str = "") -> list[str]:
    inspected = [] if inspected is None else inspected
    hits = []
    context = _person_context(call, func)
    if context:
        for value in [kw.value for kw in call.keywords] + list(call.args[1:]):
            if any(True for _ in _name_lookups(value)):
                hits.append(f"person_name:{context}")
    values = [kw.value for kw in call.keywords] + list(call.args)
    for kw in call.keywords:
        if kw.arg and _matches(kw.arg) and not _exempt(kw.value):
            hits.append(kw.arg)
        hits.extend(i for i in _identifiers(kw.value) if _matches(i))
    for arg in call.args:
        hits.extend(i for i in _identifiers(arg) if _matches(i))
    for value in values:
        hits.extend(_text_slices(value, inspected))
    if filename.endswith(CACHE_KEY_FILE) and any(
        (isinstance(n, ast.Name) and n.id == "cache_key") or (isinstance(n, ast.keyword) and n.arg == "cache_key")
        for n in ast.walk(call)
    ):
        hits.append("text_slice:cache_key")
    return hits


class _Finder(ast.NodeVisitor):
    def __init__(self, filename: str = "") -> None:
        self.filename = filename
        self.stack: list[str] = []
        self.calls = 0
        self.inspected: list = []
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
            hits = _call_hits(node, filename=self.filename, inspected=self.inspected,
                              func=self.stack[-1] if self.stack else "")
            if hits:
                self.found.append((self.stack[-1] if self.stack else "<module>", node.lineno, sorted(set(hits))))
        self.generic_visit(node)


def find_pii_logs(source: str, filename: str = "<string>") -> tuple[list[tuple[str, int, list[str]]], int]:
    found, calls, _inspected = _find(source, filename)
    return found, calls


def _find(source: str, filename: str = "<string>"):
    finder = _Finder(filename)
    finder.visit(ast.parse(source, filename=filename))
    return finder.found, finder.calls, len(finder.inspected)


def _scan_files() -> list[Path]:
    files = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        for path in ([base] if base.is_file() else sorted(base.rglob("*.py"))):
            parts = path.relative_to(REPO_ROOT).parts
            if "tests" in parts or "node_modules" in parts:
                continue
            files.append(path)
    return files


def scan() -> tuple[Counter, int, int, list[str]]:
    found, parsed, calls, lines, _keys = _scan()
    return found, parsed, calls, lines


_INSPECTED: list = []


def _scan():
    found: Counter = Counter()
    keys_by_pair: dict[tuple[str, str], set[str]] = {}
    parsed = calls = inspected = 0
    lines: list[str] = []
    for path in _scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SKIP:
            continue
        try:
            hits, n, k = _find(path.read_text(encoding="utf-8"), rel)
        except SyntaxError as exc:
            raise AssertionError(f"{rel} doesn't parse ({exc}); fix it or add it to SKIP with a reason") from exc
        parsed += 1
        calls += n
        inspected += k
        for func, lineno, keys in hits:
            found[(rel, func)] += 1
            keys_by_pair.setdefault((rel, func), set()).update(keys)
            lines.append(f"{rel}:{lineno} ({func}) {','.join(keys)}")
    _INSPECTED[:] = [inspected]
    return found, parsed, calls, lines, keys_by_pair


def _covered(identifier: str, reason: str) -> bool:
    if identifier in REASON_IDENTIFIERS[reason]:
        return True
    keys = _matched_keys(identifier)
    return bool(keys) and keys <= REASON_KEYS[reason] and identifier not in PAYLOAD_NAMES


def reason_violations(identifiers_by_pair, allowlist) -> dict:
    """Allowlisted pairs that log something their reason doesn't cover."""
    out = {}
    for pair, (_count, reason) in allowlist.items():
        extra = {i for i in identifiers_by_pair.get(pair, set()) if not _covered(i, reason)}
        if extra:
            out[pair] = sorted(extra)
    return out


def test_reason_violations_self_test():
    allowlist = {("a.py", "f"): (1, OPERATOR_AUDIT), ("b.py", "g"): (1, DEFERRED)}
    ok = {("a.py", "f"): {"username", "current_user_email", "ip_address"}, ("b.py", "g"): {"location"}}
    assert reason_violations(ok, allowlist) == {}
    assert reason_violations({("a.py", "f"): {"username", "guest_name"}}, allowlist) == {("a.py", "f"): ["guest_name"]}
    assert reason_violations({("a.py", "f"): {"home_address"}}, allowlist) == {("a.py", "f"): ["home_address"]}
    assert reason_violations({("a.py", "f"): {"update_data"}}, allowlist) == {("a.py", "f"): ["update_data"]}
    assert reason_violations({("b.py", "g"): {"phone_number"}}, allowlist) == {("b.py", "g"): ["phone_number"]}


def test_log_calls_match_the_allowlist():
    found, parsed, calls, lines, keys_by_pair = _scan()
    assert parsed >= 230, f"only {parsed} files parsed; the scan roots moved"
    assert calls >= 2900, f"only {calls} logger calls found; the logger-call detector broke"
    assert _INSPECTED[0] >= 30, f"only {_INSPECTED[0]} head slices inspected; the slice rule is dead"
    expected = Counter({key: count for key, (count, _reason) in ALLOWLIST.items()})
    expected.update({key: count for key, (count, _reason) in FROZEN_TEXT_SITES.items()})
    expected.update({key: count for key, (count, _reason) in FROZEN_FULL_TEXT_SITES.items()})
    assert sum(expected.values()) >= 200
    new = found - expected
    gone = expected - found
    assert not new and not gone, (
        "logged PII drifted from the allowlist.\n"
        f"New (log presence or an id instead): {dict(new)}\n"
        f"Gone (drop from ALLOWLIST): {dict(gone)}\n" + "\n".join(lines)
    )
    assert ("src/orchestrator/main.py", "tool_call_node") not in found
    assert ("src/orchestrator/main.py", "execute_single_tool") not in found
    assert keys_by_pair[("admin/backend/app/routes/sms.py", "update_sms_settings")] == {"username"}
    assert ("admin/backend/app/routes/guests.py", "get_guest") not in found
    assert ("src/orchestrator/helpers.py", "maybe_post_synthesis_fallback") not in found
    assert ("src/orchestrator/nodes/route_control.py", "_resolve_pending_write_confirmation") not in found
    assert not reason_violations(keys_by_pair, ALLOWLIST)


def test_frozen_text_sites_are_outside_rebuilt_images():
    for (path, _func), (count, reason) in FROZEN_TEXT_SITES.items():
        assert count >= 1 and reason.strip(), path
        assert not path.startswith(REBUILT_IMAGE_ROOTS), path


def test_text_slice_detector_self_test():
    source = '''
def flagged(query, s, answer, a, msg_preview, e, cache_key, body):
    logger.info("x", q=query[:50])
    logger.info("x", t=s.text[0:80])
    logger.info(f"{answer[:100]}")
    logger.info("x", extra={"answer_preview": a[:100]})
    logger.info(f"{msg_preview}")
    logger.info("x", e=str(e)[:200])
    logger.info("x", k=cache_key[:50])
    logger.info("%s", body[:10])

def not_flagged(session_id, device_id, request_hash, phone, query, digest, word):
    logger.info("x", sid=session_id[:8])
    logger.info("x", d=device_id[:16])
    logger.info("x", h=request_hash[:12])
    logger.info("x", last4=phone[-4:])
    logger.info("x", n=len(query))
    logger.info("x", short=word[:4])
    logger.info("x", key=hashlib.sha256(query.encode()).hexdigest()[:16])
'''
    found, calls, inspected = _find(source)
    assert calls == 15
    assert [(func, line) for func, line, _keys in found] == [("flagged", n) for n in range(3, 11)]
    assert inspected == 12  # the 7 flagged head slices + session_id, device_id, request_hash, word[:4], hexdigest
    by_line = by_line_keys(found)
    assert by_line[3] == ["full_text:query", "text_slice:query"]
    assert by_line[6] == ["answer_preview", "text_slice:a"]
    assert by_line[7] == ["msg_preview"]
    assert by_line[8] == ["text_slice:str"]


def test_cache_key_rule_is_scoped_to_the_semantic_cache():
    source = '''
def f(cache_key, category):
    logger.info("cache_hit", key=cache_key, category=category)
    logger.info("cache_hit", category=category)
'''
    found, _calls, _inspected = _find(source, CACHE_KEY_FILE)
    assert [(line, keys) for _func, line, keys in found] == [(3, ["text_slice:cache_key"])]
    assert _find(source, "src/orchestrator/other.py")[0] == []


def test_every_allowlist_entry_has_a_reason():
    for key, (count, reason) in ALLOWLIST.items():
        assert count >= 1 and reason in (OPERATOR_AUDIT, DEFERRED), key
        if reason == OPERATOR_AUDIT:
            assert key[0].startswith(("admin/backend/app/", "admin/backend/main.py")), key


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
    db.add(AuditLog(action="x", new_value=update_data))
    AuditLog(new_value={"phone_number": p})
    logger.info("x", changes=update_data)
    logger.info(f"Calling tool with args: {arguments}")
    logger.info("x", name=guest.name)
    logger.info("x", body=request_model.model_dump())
    logger.info("x", tail=phone_number[-5:])

def not_flagged(p, g, guest_name, location):
    logger.info("x", phone_last4=p[-4:])
    logger.info("x", has_guest_name=bool(g))
    logger.info("x", n=len(guest_name))
    logger.info("x", location_set=location is not None)
    logger.info("x", changed_fields=sorted(update_data.keys()))
    logger.info("x", arg_keys=sorted(arguments.keys()))
    logger.info("x", arg_keys=payload_keys(tool_args))
    AuditLog(action="x", user_id=current_user_id, new_value=redact_phone_fields(update_data))
    logger.info("x", automation=automation.id)
    logger.info("x", phone_last2=phone_number[-2:])
'''
    found, calls = find_pii_logs(source)
    assert [line for _func, line, _keys in found] == list(range(5, 28))
    assert all(func == "flagged" for func, _line, _keys in found)
    assert calls == 33
    by_line = by_line_keys(found)
    assert by_line[20] == ["location"]
    assert by_line[21] == ["update_data"]
    assert by_line[22] == ["phone_number"]
    assert by_line[25] == ["person_name:guest"]
    assert by_line[26] == ["payload:model_dump"]
    assert by_line[27] == ["phone_number"]
    by_line = by_line_keys(found)
    assert by_line[18] == ["location_override"]
    assert by_line[19] == ["ip_address"]
    assert _matched_keys("current_user_username") == {"username"}
    assert _matched_keys("guest_name_len") == set()


def by_line_keys(found):
    return {line: keys for _func, line, keys in found}


# ---------------------------------------------------------------------------
# Whole text (unsliced), automation names, phone numbers
# ---------------------------------------------------------------------------

def test_frozen_full_text_sites():
    _found, _parsed, _calls, _lines, keys_by_pair = _scan()
    assert len(FROZEN_FULL_TEXT_SITES) >= 25, "the full-text rule found almost nothing; is it dead?"
    assert ("src/rag/websearch/main.py", "web_search") in FROZEN_FULL_TEXT_SITES
    for (path, func), (count, reason) in FROZEN_FULL_TEXT_SITES.items():
        assert count >= 1 and reason in (NOT_REBUILT, NOT_TEXT), (path, func)
        if reason == NOT_REBUILT:
            assert not path.startswith(REBUILT_IMAGE_ROOTS), path
        logged = keys_by_pair.get((path, func), set())
        assert logged and all(k.startswith("full_text:") for k in logged), (path, func, logged)
    assert sum(1 for _c, r in FROZEN_FULL_TEXT_SITES.values() if r == NOT_TEXT) <= 2


def test_full_text_detector_self_test():
    source = '''
def flagged(query, request, response, message, automation, to_number, phone_e164, t):
    logger.info("x", query=query)
    logger.info("x", q=request.query)
    logger.warning("x", error=response.text)
    logger.info(f"sent: {message}")
    logger.info("x", utterance=t.utterance)
    logger.info("x", name=automation.name)
    logger.info(f"to {to_number}")
    logger.info("x", phone=phone_e164)
    logger.info("x", body=r.body)

def not_flagged(query, response, message, to_number, automation):
    logger.info("x", query_len=len(query))
    logger.info("x", response_len=len(response.text))
    logger.info("x", message="a static message")
    logger.info(f"to ***{to_number[-4:]}")
    logger.info("x", phone_last4=to_number[-4:])
    logger.info("x", automation_id=automation.id)
    logger.info("x", has_text=bool(response.text))
'''
    found, calls, _inspected = _find(source)
    assert calls == 16
    assert [(func, line) for func, line, _keys in found] == [("flagged", n) for n in range(3, 12)]
    by_line = by_line_keys(found)
    assert "full_text:query" in by_line[3]
    assert "full_text:text" in by_line[5]
    assert by_line[8] == ["person_name:automation"]
    assert by_line[9] == ["to_number"]
    assert "phone_e164" in by_line[10]


def test_name_lookup_in_a_person_context_self_test():
    """``data.get("name")`` / ``row["name"]`` is a person's or an
    automation's name when the log event or the enclosing function is about
    an automation, guest, booking, member or participant."""
    source = '''
def create_voice_automation(data):
    logger.info("voice_automation_created", id=data.get("id"), name=data.get("name"))

def f(row):
    logger.info("guest_checked_in", who=row["name"])

def g(data):
    logger.info("automation_saved", label=str(data.get("name")))

def not_flagged(data, tool):
    logger.info("tool_registered", name=tool.get("name"))
    logger.info("voice_automation_created", id=data.get("id"), name_len=len(data.get("name") or ""))
    logger.info("guest_checked_in", has_name=bool(data.get("name")))
'''
    found, calls, _inspected = _find(source)
    assert calls == 6
    by_line = by_line_keys(found)
    assert sorted(by_line) == [3, 6, 9]
    assert by_line[3] == ["person_name:automation"]
    assert by_line[6] == ["person_name:guest"]
