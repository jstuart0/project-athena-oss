"""Every admin-backend route carries a permission-bearing auth dependency,
or is on the one reviewed public list.

- ``INTENTIONALLY_PUBLIC``: routes that must stay reachable without a
  credential, each with the precondition that makes that safe. Nothing else
  may answer anonymously: a new anonymous route fails (b) until it's gated.
- ``GATED``: every reviewed route, pinned to the exact auth factory and
  permission it carries. It is the guest-data routes plus
  ``REVIEWED_BY_FILE``, the 93 operations that used to answer anonymously
  (ATHENA-168), keyed by the route file that defines them.

``REVIEWED_BY_FILE`` and ``REVIEWED_GROUPS`` are pure literals: the stdlib
scanners under ``tests/unit`` read them with ``ast.literal_eval`` instead of
importing this module (which imports the app).

Every authenticated route must also carry a permission-bearing dependency:
one exposing ``required_permission`` (the two factories, memories' reader
and maintainer guards) or a service-only guard that admits no user at all.
The operations that authenticate without one (a bare ``get_current_user``
or ``verify_service_or_oidc``) are frozen in ``tests/route_auth_legacy.py``
and compared exactly, so a new route can't join them.

Both allowlists are compared per walked route (``shared.route_walk``), so a
route registered twice or hidden from the OpenAPI schema can't hide.

The two factories resolve the user themselves (``get_current_user`` is
called inside them), so ``app.dependency_overrides[get_current_user]``
doesn't reach them. Tests that exercise them authenticate with real tokens
(see ``test_guest_routes_auth.py``).
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI

from app.auth.oidc import get_current_user
from app.routes import guest_mode, internal, memories, sms_webhook
from app.utils import service_auth
from main import app
from shared.route_walk import dependency_calls, iter_api_routes

from tests.route_auth_legacy import LEGACY_PERMISSIONLESS, LEGACY_PERMISSIONLESS_COUNT

BACKEND = Path(__file__).resolve().parents[1]

_AUTH_CALLS = {
    get_current_user,
    service_auth.verify_service_or_oidc,
    service_auth.verify_service_api_key,
    internal.require_service_key_401,
    sms_webhook.validate_twilio_signature,
    memories.require_memory_reader,
    memories.require_memory_maintainer,
    guest_mode._guest_mode_config_auth,
}
_FACTORY_INNERS = {
    "user": "require_user_permission.<locals>._dependency",
    "service_or_user": "require_service_or_user_permission.<locals>._dependency",
}


def _factory_kind(call):
    if getattr(call, "__module__", None) != "app.utils.service_auth":
        return None
    qualname = getattr(call, "__qualname__", "")
    for kind, name in _FACTORY_INNERS.items():
        if qualname == name:
            return kind
    return None


def is_auth(call) -> bool:
    """Identity membership, or a factory inner matched by module + qualname.
    Never a bare name: a local function called ``get_current_user`` isn't
    auth (test (i))."""
    try:
        if call in _AUTH_CALLS:
            return True
    except TypeError:
        return False
    return _factory_kind(call) is not None


def _operations(application):
    """[(method, path, walked)] for every walked API route and method,
    duplicates and include_in_schema=False routes kept."""
    return [
        (method, walked.path, walked)
        for walked in iter_api_routes(application)
        for method in sorted(walked.methods)
    ]


def _is_authenticated(walked) -> bool:
    return any(is_auth(call) for call in dependency_calls(walked))


INTENTIONALLY_PUBLIC = {
    ("GET", "/health"): "liveness only; returns no data",
    ("GET", "/api/auth/login"): "OIDC redirect; mints a token only in demo mode, which the production startup gates refuse (test h)",
    ("GET", "/auth/login"): "OIDC redirect; mints a token only in demo mode, which the production startup gates refuse (test h)",
    ("GET", "/api/auth/callback"): "OIDC code exchange; authlib validates the IdP's token",
    ("GET", "/auth/callback"): "OIDC code exchange; authlib validates the IdP's token",
    ("GET", "/api/auth/logout"): "clears the caller's own session",
    ("GET", "/auth/logout"): "clears the caller's own session",
    ("GET", "/api/auth/methods"): "lists the enabled sign-in methods",
    ("GET", "/api/auth/session-token"): "returns a token only from a server-side session that callback/demo login populated",
    ("POST", "/api/auth/local-login"): "password login behind the per-IP rate limit, per-user lockout and timing floor",
    ("GET", "/api/calendar-sources/types"): "static list of source types",
    ("GET", "/api/settings/assistant-profile/public"): "persona only; response keys pinned by test h",
    ("GET", "/api/settings/privacy/public"): "one boolean; response keys pinned by test h",
}

# kind, permission. kind: "service_or_user" (X-Service-Key, or a user holding
# the permission), "user" (a signed-in user only; any service key is refused),
# "service" (X-Service-Key only).
REVIEWED_BY_FILE = {
    "alerts.py": {
        ("GET", "/api/alerts/public/active-by-type"): ("user", "read:alerts"),
        ("POST", "/api/alerts/public/create"): ("service_or_user", "write"),
        ("POST", "/api/alerts/public/resolve-by-entity"): ("service_or_user", "write"),
    },
    "cloud_llm_usage.py": {
        ("GET", "/api/cloud-llm-usage/alerts"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/analytics/by-intent"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/analytics/daily"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/analytics/hourly"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/recent"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/summary/month"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/summary/range"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/summary/today"): ("user", "read"),
        ("GET", "/api/cloud-llm-usage/summary/week"): ("user", "read"),
        ("POST", "/api/cloud-llm-usage"): ("service_or_user", "write"),
    },
    "cloud_providers.py": {
        ("GET", "/api/cloud-providers"): ("user", "read"),
        ("GET", "/api/cloud-providers/health/all"): ("user", "read"),
        ("GET", "/api/cloud-providers/pricing/{provider}"): ("user", "read"),
        ("GET", "/api/cloud-providers/pricing/{provider}/{model_id}"): ("service_or_user", "read"),
        ("GET", "/api/cloud-providers/{provider}"): ("user", "read"),
        ("GET", "/api/cloud-providers/{provider}/health"): ("user", "read"),
    },
    "component_models.py": {
        ("GET", "/api/component-models/component/{component_name}"): ("service_or_user", "read"),
        ("GET", "/api/component-models/public"): ("service_or_user", "read"),
    },
    "directions_settings.py": {
        ("GET", "/api/directions-settings/public"): ("service_or_user", "read"),
    },
    "escalation.py": {
        ("GET", "/api/escalation/metrics/prometheus"): ("user", "read"),
        ("GET", "/api/escalation/presets/active/public"): ("service_or_user", "read"),
        ("GET", "/api/escalation/presets/public"): ("service_or_user", "read"),
        ("GET", "/api/escalation/state/{session_id}/public"): ("service_or_user", "read"),
        ("POST", "/api/escalation/events/internal"): ("service_or_user", "write"),
        ("POST", "/api/escalation/state/internal"): ("service_or_user", "write"),
        ("PUT", "/api/escalation/state/{session_id}/decrement"): ("service_or_user", "write"),
    },
    "features.py": {
        ("GET", "/api/features/public"): ("service_or_user", "read"),
    },
    "follow_me.py": {
        ("GET", "/api/follow-me/internal/config"): ("service_or_user", "read"),
    },
    "gateway_config.py": {
        ("GET", "/api/gateway-config/public"): ("service_or_user", "read"),
    },
    "ha_pipelines.py": {
        ("GET", "/api/ha-pipelines/health"): ("user", "read"),
        ("GET", "/api/ha-pipelines/modes"): ("user", "read"),
        ("GET", "/api/ha-pipelines/pipelines"): ("user", "read"),
        ("GET", "/api/ha-pipelines/pipelines/preferred"): ("user", "read"),
    },
    "intent_routing.py": {
        ("GET", "/api/intent-routing/providers/public"): ("service_or_user", "read"),
        ("GET", "/api/intent-routing/routing/public"): ("service_or_user", "read"),
        ("GET", "/api/intent-routing/strategy/configs/public"): ("service_or_user", "read"),
        ("GET", "/api/intent-routing/strategy/configs/{intent_name}"): ("service_or_user", "read"),
    },
    "llm_backends.py": {
        ("GET", "/api/llm-backends/public"): ("service_or_user", "read"),
        ("GET", "/api/llm-backends/public/mlx-applicability"): ("user", "read"),
        ("POST", "/api/llm-backends/metrics"): ("service_or_user", "write"),
    },
    "mcp_security.py": {
        ("GET", "/api/mcp-security/public"): ("service_or_user", "read"),
        ("POST", "/api/mcp-security/check-domain"): ("service_or_user", "read"),
    },
    "model_config.py": {
        ("GET", "/api/model-configs/presets"): ("user", "read"),
        ("GET", "/api/model-configs/public"): ("service_or_user", "read"),
        ("GET", "/api/model-configs/public/{model_name:path}"): ("service_or_user", "read"),
    },
    "model_downloads.py": {
        ("POST", "/api/model-downloads/internal/{download_id}/progress"): ("service", None),
    },
    "modules.py": {
        ("GET", "/api/modules/"): ("user", "read"),
        ("GET", "/api/modules/admin-tabs"): ("user", "read"),
        ("GET", "/api/modules/enabled"): ("user", "read"),
        ("GET", "/api/modules/{module_id}"): ("user", "read"),
        ("POST", "/api/modules/refresh-all"): ("user", "write"),
        ("POST", "/api/modules/{module_id}/refresh"): ("user", "write"),
    },
    "music_config.py": {
        ("GET", "/api/music-config/browser-playback"): ("user", "read"),
        ("GET", "/api/music-config/internal"): ("service_or_user", "read"),
    },
    "performance_presets.py": {
        ("GET", "/api/presets/public/active"): ("service_or_user", "read"),
    },
    "rag_service_bypass.py": {
        ("GET", "/api/rag-service-bypass"): ("user", "read"),
        ("GET", "/api/rag-service-bypass/{service_name}"): ("user", "read"),
    },
    "room_audio.py": {
        ("GET", "/api/room-audio/internal"): ("service_or_user", "read"),
        ("GET", "/api/room-audio/internal/{room_name}"): ("service_or_user", "read"),
    },
    "room_tv.py": {
        ("GET", "/api/room-tv/apps"): ("service_or_user", "read"),
        ("GET", "/api/room-tv/features"): ("service_or_user", "read"),
        ("GET", "/api/room-tv/internal"): ("service_or_user", "read"),
        ("GET", "/api/room-tv/internal/{room_name}"): ("service_or_user", "read"),
    },
    "service_registry.py": {
        ("GET", "/api/service-registry/services/{service_name}"): ("user", "read"),
        ("GET", "/api/service-registry/services/{service_name}/url"): ("service_or_user", "read"),
    },
    "tool_calling.py": {
        ("GET", "/api/tool-calling/settings/public"): ("service_or_user", "read"),
        ("GET", "/api/tool-calling/tools/by-name/{tool_name}/api-keys/public"): ("service_or_user", "read"),
        ("GET", "/api/tool-calling/tools/stats/public"): ("service_or_user", "read"),
        ("GET", "/api/tool-calling/tools/{tool_id}/api-keys/public"): ("service_or_user", "read"),
        ("GET", "/api/tool-calling/triggers/public"): ("service_or_user", "read"),
    },
    "tool_proposals.py": {
        ("GET", "/api/tool-proposals"): ("user", "read"),
        ("GET", "/api/tool-proposals/stats/summary"): ("user", "read"),
        ("GET", "/api/tool-proposals/{proposal_id}"): ("user", "read"),
        ("POST", "/api/tool-proposals"): ("service_or_user", "write"),
    },
    "voice_config.py": {
        ("GET", "/api/voice-config/health"): ("service_or_user", "read"),
        ("GET", "/api/voice-config/internal/all"): ("service_or_user", "read"),
        ("GET", "/api/voice-config/internal/stt"): ("service_or_user", "read"),
        ("GET", "/api/voice-config/internal/tts"): ("service_or_user", "read"),
        ("GET", "/api/voice-config/running-config"): ("user", "read"),
        ("GET", "/api/voice-config/services"): ("user", "read"),
        ("GET", "/api/voice-config/services/{service_type}"): ("user", "read"),
        ("GET", "/api/voice-config/stt/active"): ("user", "read"),
        ("GET", "/api/voice-config/stt/models"): ("user", "read"),
        ("GET", "/api/voice-config/tts/active"): ("user", "read"),
        ("GET", "/api/voice-config/tts/voices"): ("user", "read"),
    },
    "voice_interfaces.py": {
        ("GET", "/api/voice-interfaces/engines/public/stt"): ("service_or_user", "read"),
        ("GET", "/api/voice-interfaces/engines/public/tts"): ("service_or_user", "read"),
        ("GET", "/api/voice-interfaces/internal/config/{interface_name}"): ("service_or_user", "read"),
        ("GET", "/api/voice-interfaces/public"): ("service_or_user", "read"),
        ("GET", "/api/voice-interfaces/public/{interface_name}"): ("service_or_user", "read"),
    },
}

# The order the routes are gated in (plan steps 6.1, 6.2, 6.3).
REVIEWED_GROUPS = {
    "g1": ["alerts.py", "cloud_llm_usage.py", "cloud_providers.py", "escalation.py", "tool_proposals.py", "llm_backends.py", "model_downloads.py", "modules.py", "mcp_security.py"],
    "g2": ["component_models.py", "directions_settings.py", "features.py", "follow_me.py", "gateway_config.py", "intent_routing.py", "model_config.py", "performance_presets.py", "rag_service_bypass.py", "service_registry.py", "tool_calling.py"],
    "g3": ["ha_pipelines.py", "music_config.py", "room_audio.py", "room_tv.py", "voice_config.py", "voice_interfaces.py"],
}
REVIEWED = {op: pin for ops in REVIEWED_BY_FILE.values() for op, pin in ops.items()}
PROGRESS_OP = ("POST", "/api/model-downloads/internal/{download_id}/progress")

VA = "/api/voice-automations"
GUEST_DATA_GATED = {
    # guests.py
    ("GET", "/api/guests"): ("user", "read"),
    ("GET", "/api/guests/current"): ("user", "read"),
    ("GET", "/api/guests/by-events"): ("user", "read"),
    ("GET", "/api/guests/{guest_id}"): ("user", "read"),
    ("POST", "/api/guests/current/add"): ("user", "write"),
    # user_sessions.py
    ("POST", "/api/user-sessions"): ("user", "write"),
    ("GET", "/api/user-sessions/device/{device_id}"): ("service_or_user", "read"),
    ("GET", "/api/user-sessions/{session_id}"): ("user", "read"),
    ("PATCH", "/api/user-sessions/{session_id}/last-seen"): ("user", "write"),
    ("DELETE", "/api/user-sessions/device/{device_id}"): ("user", "write"),
    # room_groups.py
    ("GET", "/api/room-groups"): ("service_or_user", "read"),
    ("GET", "/api/room-groups/resolve/{query_term}"): ("service_or_user", "read"),
    ("GET", "/api/room-groups/available-rooms"): ("user", "read"),
    # settings.py
    ("GET", "/api/settings/llm-memory"): ("user", "read"),
    ("GET", "/api/settings/tool-proposals"): ("user", "read"),
    ("GET", "/api/settings/ollama-url"): ("user", "read"),
    ("GET", "/api/settings/house-layout"): ("service_or_user", "read"),
    ("GET", "/api/settings/directions-origin-placeholders"): ("service_or_user", "read"),
    ("GET", "/api/settings/ollama-url/internal"): ("service_or_user", "read"),
    # llm_backends.py
    ("GET", "/api/llm-backends/model/{model_name}"): ("service_or_user", "read"),
    # sms.py
    ("GET", "/api/sms/internal/current-preferences"): ("service_or_user", "read"),
    ("POST", "/api/sms/internal/log-send"): ("service", None),
    # debug_logs.py
    ("GET", "/api/debug-logs/status"): ("user", "read"),
    ("GET", "/api/debug-logs/files"): ("user", "read"),
    ("GET", "/api/debug-logs/search"): ("user", "read"),
    ("GET", "/api/debug-logs/tail/{filename}"): ("user", "read"),
    # ha_pipelines.py
    ("POST", "/api/ha-pipelines/mode/set"): ("user", "write"),
    # voice_automations.py (GET "" was user-only before: the D11 change)
    ("GET", VA): ("service_or_user", "read"),
    ("GET", f"{VA}/guest/{{guest_name}}/archived"): ("service_or_user", "read"),
    ("GET", f"{VA}/internal/by-guest-name/{{guest_name}}"): ("service_or_user", "read"),
    ("POST", VA): ("service_or_user", "write"),
    ("POST", f"{VA}/{{automation_id}}/archive"): ("service_or_user", "write"),
    ("POST", f"{VA}/{{automation_id}}/restore"): ("service_or_user", "write"),
    ("POST", f"{VA}/guest-departure/{{session_id}}"): ("service_or_user", "write"),
    ("POST", f"{VA}/{{automation_id}}/triggered"): ("service_or_user", "write"),
    ("POST", f"{VA}/archive-guest"): ("service_or_user", "write"),
    ("POST", f"{VA}/restore-guest"): ("service_or_user", "write"),
    # pipeline_events.py
    ("GET", "/api/pipeline-events"): ("user", "read"),
    ("GET", "/api/pipeline-events/sessions"): ("user", "read"),
    ("GET", "/api/pipeline-events/stats"): ("user", "read"),
    ("GET", "/api/pipeline-events/{session_id}"): ("user", "read"),
    ("POST", "/api/pipeline-events/emit"): ("service_or_user", "write"),
    # emerging_intents.py (the orchestrator's internal routes)
    ("GET", "/api/internal/emerging-intents"): ("service_or_user", "read"),
    ("POST", "/api/internal/emerging-intents"): ("service_or_user", "write"),
    ("POST", "/api/internal/emerging-intents/{intent_id}/increment"): ("service_or_user", "write"),
    ("POST", "/api/internal/intent-metrics"): ("service_or_user", "write"),
}
GATED = {**GUEST_DATA_GATED, **REVIEWED}

# The hard delete stays user-only: a service key must never reach it
# (it removes the admin row while the HA automation keeps running).
USER_ONLY_PINNED = {("DELETE", f"{VA}/{{automation_id}}")}

CALLER_SCOPED = {
    ("GET", VA),
    ("GET", f"{VA}/guest/{{guest_name}}/archived"),
    ("POST", VA),
    ("POST", f"{VA}/{{automation_id}}/archive"),
    ("POST", f"{VA}/{{automation_id}}/restore"),
    ("POST", f"{VA}/guest-departure/{{session_id}}"),
    ("POST", f"{VA}/{{automation_id}}/triggered"),
    ("GET", f"{VA}/internal/by-guest-name/{{guest_name}}"),
    ("POST", f"{VA}/archive-guest"),
    ("POST", f"{VA}/restore-guest"),
}


def _walked_by_op():
    found = collections.defaultdict(list)
    for method, path, walked in _operations(app):
        found[(method, path)].append(walked)
    return found


# (a) --------------------------------------------------------------------

def test_walk_is_not_vacuous_and_sees_hidden_routes():
    ops = _operations(app)
    assert len(ops) >= 600, len(ops)
    by_op = _walked_by_op()
    assert ("GET", "/api/guests/current") in by_op
    assert ("GET", "/health") in by_op
    hidden = ("POST", "/api/internal/guest-mode/verify-pin")
    assert hidden in by_op
    assert not by_op[hidden][0].route.include_in_schema
    assert all(_is_authenticated(w) for w in by_op[hidden])


# (b) --------------------------------------------------------------------

def test_unauthenticated_routes_are_exactly_the_public_list():
    unauth = {(m, p) for m, p, w in _operations(app) if not _is_authenticated(w)}
    allowed = set(INTENTIONALLY_PUBLIC)
    extra = sorted(unauth - allowed)
    missing = sorted(allowed - unauth)
    assert not extra and not missing, (
        f"{len(extra)} unauthenticated route(s) on no list: {extra}\n"
        f"{len(missing)} listed route(s) now authenticated or gone: {missing}"
    )


# (c) --------------------------------------------------------------------

def test_lists_are_disjoint_and_reasoned():
    public, gated = set(INTENTIONALLY_PUBLIC), set(GATED)
    assert len(public) == 13
    assert len(GUEST_DATA_GATED) == 46
    assert len(gated) == 139
    assert sum(len(ops) for ops in REVIEWED_BY_FILE.values()) == 93
    assert len(REVIEWED_BY_FILE) == 26
    # No operation under two files, and none already pinned by the
    # guest-data table (a dict merge would hide either).
    assert len(REVIEWED) == 93
    assert not set(REVIEWED) & set(GUEST_DATA_GATED)
    assert public & gated == set()
    assert not USER_ONLY_PINNED & (public | gated)
    assert all(r.strip() for r in INTENTIONALLY_PUBLIC.values())


# (d) --------------------------------------------------------------------

def _gated_problems():
    by_op = _walked_by_op()
    problems = []
    for op, (kind, permission) in sorted(GATED.items()):
        walked_list = by_op.get(op)
        if not walked_list:
            problems.append((op, "route not found"))
            continue
        for walked in walked_list:
            calls = dependency_calls(walked)
            inners = [c for c in calls if _factory_kind(c) is not None]
            if kind == "service":
                if inners:
                    problems.append((op, f"unexpected factory {inners}"))
                if service_auth.verify_service_api_key not in calls:
                    problems.append((op, "verify_service_api_key missing"))
                continue
            if len(inners) != 1:
                problems.append((op, f"{len(inners)} factory inners"))
                continue
            inner = inners[0]
            if _factory_kind(inner) != kind:
                problems.append((op, f"kind {_factory_kind(inner)} != {kind}"))
            expected_kinds = ("service", "user") if kind == "service_or_user" else ("user",)
            if tuple(getattr(inner, "caller_kinds", ())) != expected_kinds:
                problems.append((op, f"caller_kinds {getattr(inner, 'caller_kinds', None)}"))
            if getattr(inner, "required_permission", None) != permission:
                problems.append((op, f"permission {getattr(inner, 'required_permission', None)} != {permission}"))
    return problems


def test_gated_routes_carry_the_pinned_factory_and_permission():
    problems = _gated_problems()
    assert not problems, f"{len(problems)} gated route problem(s): {problems}"


def test_gated_named_members():
    assert GATED[("POST", "/api/guests/current/add")] == ("user", "write")
    assert GATED[("GET", "/api/user-sessions/device/{device_id}")] == ("service_or_user", "read")
    assert GATED[("GET", "/api/internal/emerging-intents")] == ("service_or_user", "read")
    assert GATED[("GET", "/api/features/public")] == ("service_or_user", "read")
    assert GATED[PROGRESS_OP] == ("service", None)
    assert GATED[("GET", "/api/alerts/public/active-by-type")] == ("user", "read:alerts")
    assert GATED[("POST", "/api/modules/refresh-all")] == ("user", "write")
    assert GATED[("POST", "/api/tool-proposals")] == ("service_or_user", "write")
    assert GATED[("GET", "/api/tool-proposals")] == ("user", "read")
    # Read by a scraper or the UI, never by a holder of the shared key.
    assert GATED[("GET", "/api/escalation/metrics/prometheus")] == ("user", "read")
    assert GATED[("GET", "/api/cloud-providers/{provider}/health")] == ("user", "read")


def test_reviewed_table_totals():
    pins = list(REVIEWED.values())
    assert collections.Counter(kind for kind, _ in pins) == {"service_or_user": 50, "user": 42, "service": 1}
    assert collections.Counter(perm for _, perm in pins) == {"read": 81, "read:alerts": 1, "write": 10, None: 1}
    grouped = [name for files in REVIEWED_GROUPS.values() for name in files]
    assert sorted(grouped) == sorted(REVIEWED_BY_FILE), "every file is in exactly one group"
    assert tuple(
        sum(len(REVIEWED_BY_FILE[name]) for name in REVIEWED_GROUPS[group]) for group in ("g1", "g2", "g3")
    ) == (42, 23, 28)


# The one non-GET that changes nothing: it answers allow/deny for a URL.
NON_MUTATING_POSTS = {("POST", "/api/mcp-security/check-domain")}


def test_reviewed_writes_need_write():
    non_get = {op: pin for op, pin in REVIEWED.items() if op[0] != "GET"}
    assert len(non_get) == 12, sorted(non_get)
    lenient = {op for op, pin in non_get.items() if pin != ("service", None) and pin[1] != "write"}
    assert lenient == NON_MUTATING_POSTS


def test_only_local_login_is_a_public_write():
    assert {op for op in INTENTIONALLY_PUBLIC if op[0] != "GET"} == {("POST", "/api/auth/local-login")}


def test_hard_delete_stays_user_only():
    by_op = _walked_by_op()
    for op in USER_ONLY_PINNED:
        assert by_op.get(op), op
        for walked in by_op[op]:
            calls = dependency_calls(walked)
            assert get_current_user in calls, op
            assert not [c for c in calls if _factory_kind(c) is not None], op
            assert service_auth.verify_service_or_oidc not in calls, op


# (e) --------------------------------------------------------------------

def test_optional_user_is_not_auth():
    from app.auth.oidc import get_optional_user
    from app.utils.service_auth import require_user_permission

    router = APIRouter()

    @router.get("/api/zz-optional")
    async def optional(user=Depends(get_optional_user)):
        return {}

    @router.get("/api/zz-optional-and-guarded", dependencies=[Depends(require_user_permission("read"))])
    async def optional_and_guarded(user=Depends(get_optional_user)):
        return {}

    toy = FastAPI()
    toy.include_router(router)
    walked = {path: w for _m, path, w in _operations(toy)}
    assert sorted(walked) == ["/api/zz-optional", "/api/zz-optional-and-guarded"]
    assert get_optional_user in dependency_calls(walked["/api/zz-optional"]), "the walk sees the dependency"
    assert _is_authenticated(walked["/api/zz-optional"]) is False
    assert _is_authenticated(walked["/api/zz-optional-and-guarded"]) is True


# (f) --------------------------------------------------------------------

_DOCS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

_NON_API_SCRIPT = """
import json
from main import app
from shared.route_walk import iter_api_routes, iter_routes
api = {id(w.route) for w in iter_api_routes(app)}
print("NONAPI=" + json.dumps(sorted({getattr(w.route, "path", "") for w in iter_routes(app) if id(w.route) not in api})))
"""


@pytest.mark.parametrize("dev_mode", ["true", "false"])
def test_non_api_routes(dev_mode):
    env = {
        **os.environ,
        "DEV_MODE": dev_mode,
        "DATABASE_URL": "sqlite:///:memory:",
        "QDRANT_URL": "http://127.0.0.1:1",
        "SERVICE_API_KEY": "test-service-key-for-hardening-tests",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _NON_API_SCRIPT], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [l for l in proc.stdout.splitlines() if l.startswith("NONAPI=")][-1]
    found = set(json.loads(line[len("NONAPI="):]))
    expected = {"/ws/admin-jarvis", ""} | (_DOCS if dev_mode == "true" else set())
    assert found == expected


# (g) --------------------------------------------------------------------

def test_only_known_duplicate_registration():
    counts = collections.Counter((m, p) for m, p, _ in _operations(app))
    assert {op for op, n in counts.items() if n > 1} == {("GET", "/api/audit/recent")}


# (h) --------------------------------------------------------------------

@pytest.fixture
def _auth_on(monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)


def test_public_assistant_profile_keys(client, _auth_on):
    resp = client.get("/api/settings/assistant-profile/public")
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {
        "assistant_name", "project_name", "identity", "persona_traits",
        "communication_style", "guardrails",
    }


def test_public_privacy_keys(client, _auth_on):
    resp = client.get("/api/settings/privacy/public")
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"analytics_mode_enabled"}


def _served_endpoint(method, path):
    """The endpoint main.app actually serves. Resolved through the app, not
    by importing a module: test_rate_limit_active.py evicts and re-imports
    every app./main/shared. module mid-suite, so a fresh import can be a
    different module object from the one the app's routes use."""
    for walked in iter_api_routes(app):
        if walked.path == path and method in walked.methods:
            return walked.route.endpoint
    raise AssertionError((method, path))


def test_login_mints_no_token_outside_demo_mode(client, _auth_on, monkeypatch):
    from starlette.responses import RedirectResponse

    login_globals = _served_endpoint("GET", "/api/auth/login").__globals__
    clear = login_globals["get_config"].cache_clear

    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("OIDC_CLIENT_ID", "real-client")
    clear()
    minted = []

    def _no_token(*args, **kwargs):
        minted.append(1)
        raise AssertionError("login minted a token outside demo mode")

    class _Provider:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse(url="https://idp.example/authorize", status_code=302)

    class _OAuth:
        authentik = _Provider()

    monkeypatch.setitem(login_globals, "create_access_token", _no_token)
    monkeypatch.setitem(login_globals, "oauth", _OAuth())
    try:
        resp = client.get("/api/auth/login", follow_redirects=False)
        assert resp.status_code == 302, resp.text
        assert resp.headers["location"].startswith("https://idp.example/")
        assert minted == []
        session = client.get("/api/auth/session-token")
        assert "access_token" not in session.text
    finally:
        clear()


# (i) --------------------------------------------------------------------

def test_a_local_function_named_get_current_user_is_not_auth():
    def get_current_user():  # noqa: F811 -- the point: same name, not the real one
        return None

    router = APIRouter()

    @router.get("/spoof")
    async def spoof(user=Depends(get_current_user)):
        return {}

    toy = FastAPI()
    toy.include_router(router)
    ops = _operations(toy)
    assert [(m, p) for m, p, _ in ops] == [("GET", "/spoof")]
    assert not _is_authenticated(ops[0][2])


# (j) --------------------------------------------------------------------

def test_voice_automation_routes_carry_the_caller_scope():
    # The module the served route was defined in (see _served_endpoint).
    va_globals = _served_endpoint("POST", f"{VA}/{{automation_id}}/archive").__globals__
    scope_dep = va_globals.get("automation_caller_scope")
    assert scope_dep is not None, "voice_automations.automation_caller_scope is missing"
    scoped = {
        (m, p)
        for m, p, w in _operations(app)
        if p == VA or p.startswith(VA + "/")
        if scope_dep in dependency_calls(w)
    }
    assert len(scoped) >= 10
    assert ("POST", f"{VA}/{{automation_id}}/archive") in scoped
    assert scoped == CALLER_SCOPED


# (k) permission-bearing dependency on every authenticated route ------------

_SERVICE_ONLY = {
    service_auth.verify_service_api_key,
    internal.require_service_key_401,
    sms_webhook.validate_twilio_signature,
}


def is_permissioned(calls) -> bool:
    """A dependency that decides a permission (it exposes
    ``required_permission``), or a guard that admits a service and never a
    user (so no role can be under-checked)."""
    for call in calls:
        if getattr(call, "required_permission", None):
            return True
        try:
            if call in _SERVICE_ONLY:
                return True
        except TypeError:
            continue
    return False


def _permissionless(application):
    return {
        (m, p)
        for m, p, w in _operations(application)
        if _is_authenticated(w) and not is_permissioned(dependency_calls(w))
    }


def test_authenticated_routes_without_a_permission_are_exactly_the_frozen_list():
    found = _permissionless(app)
    new = sorted(found - LEGACY_PERMISSIONLESS)
    gone = sorted(LEGACY_PERMISSIONLESS - found)
    assert not new and not gone, (
        f"{len(new)} authenticated route(s) with no permission-bearing dependency "
        f"(use require_user_permission / require_service_or_user_permission): {new}\n"
        f"{len(gone)} frozen route(s) now permissioned or gone (remove them from "
        f"tests/route_auth_legacy.py): {gone}"
    )


def test_frozen_list_population():
    assert len(LEGACY_PERMISSIONLESS) == LEGACY_PERMISSIONLESS_COUNT
    assert LEGACY_PERMISSIONLESS_COUNT >= 400
    # The user-only hard delete checks 'delete' in its handler; it's frozen,
    # not permissioned.
    assert ("DELETE", "/api/voice-automations/{automation_id}") in LEGACY_PERMISSIONLESS
    assert not LEGACY_PERMISSIONLESS & set(GATED)
    assert not LEGACY_PERMISSIONLESS & set(INTENTIONALLY_PUBLIC)


def test_every_gated_route_is_permissioned():
    by_op = _walked_by_op()
    bare = sorted(op for op in GATED if not all(is_permissioned(dependency_calls(w)) for w in by_op[op]))
    assert not bare, f"{len(bare)} gated route(s) with no permission-bearing dependency: {bare}"


def test_a_new_bare_user_route_is_not_permissioned():
    """Negative control: a route guarded only by get_current_user (or
    verify_service_or_oidc) authenticates but isn't permissioned, so it fails
    the frozen-list comparison; the same route with the factory passes."""
    from app.utils.service_auth import require_service_or_user_permission, require_user_permission

    router = APIRouter()

    @router.get("/api/zz-bare-user")
    async def bare_user(user=Depends(get_current_user)):
        return {}

    @router.get("/api/zz-bare-dual", dependencies=[Depends(service_auth.verify_service_or_oidc)])
    async def bare_dual():
        return {}

    @router.get("/api/zz-user-perm", dependencies=[Depends(require_user_permission("read"))])
    async def user_perm():
        return {}

    @router.get("/api/zz-svc-perm", dependencies=[Depends(require_service_or_user_permission("read"))])
    async def svc_perm():
        return {}

    @router.get("/api/zz-service-only", dependencies=[Depends(service_auth.verify_service_api_key)])
    async def service_only():
        return {}

    toy = FastAPI()
    toy.include_router(router)
    assert _permissionless(toy) == {("GET", "/api/zz-bare-user"), ("GET", "/api/zz-bare-dual")}
