"""Every admin-backend route carries a permission-bearing auth dependency,
or is on one of two reviewed lists.

- ``INTENTIONALLY_PUBLIC``: routes that must stay reachable without a
  credential, each with the precondition that makes that safe.
- ``UNREVIEWED_UNAUTHENTICATED``: routes that were anonymous when this test
  was written and haven't been reviewed yet (ATHENA-168). The list is frozen:
  a new anonymous route fails (b) until it's gated or reviewed here, and
  gating one of these fails (b) until it's removed from the list.
- ``GATED``: the routes the guest-data hardening gated, pinned to the exact
  auth factory and permission each one carries.

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

_UNREVIEWED_REASON = "anonymous before the guest-data hardening; unreviewed (ATHENA-168)"
_UNREVIEWED_BY_FILE = {
    "alerts.py": ["GET /api/alerts/public/active-by-type", "POST /api/alerts/public/create", "POST /api/alerts/public/resolve-by-entity"],
    "cloud_llm_usage.py": ["GET /api/cloud-llm-usage/alerts", "GET /api/cloud-llm-usage/analytics/by-intent", "GET /api/cloud-llm-usage/analytics/daily", "GET /api/cloud-llm-usage/analytics/hourly", "GET /api/cloud-llm-usage/recent", "GET /api/cloud-llm-usage/summary/month", "GET /api/cloud-llm-usage/summary/range", "GET /api/cloud-llm-usage/summary/today", "GET /api/cloud-llm-usage/summary/week", "POST /api/cloud-llm-usage"],
    "cloud_providers.py": ["GET /api/cloud-providers", "GET /api/cloud-providers/health/all", "GET /api/cloud-providers/pricing/{provider}", "GET /api/cloud-providers/pricing/{provider}/{model_id}", "GET /api/cloud-providers/{provider}", "GET /api/cloud-providers/{provider}/health"],
    "component_models.py": ["GET /api/component-models/component/{component_name}", "GET /api/component-models/public"],
    "directions_settings.py": ["GET /api/directions-settings/public"],
    "escalation.py": ["GET /api/escalation/metrics/prometheus", "GET /api/escalation/presets/active/public", "GET /api/escalation/presets/public", "GET /api/escalation/state/{session_id}/public", "POST /api/escalation/events/internal", "POST /api/escalation/state/internal", "PUT /api/escalation/state/{session_id}/decrement"],
    "features.py": ["GET /api/features/public"],
    "follow_me.py": ["GET /api/follow-me/internal/config"],
    "gateway_config.py": ["GET /api/gateway-config/public"],
    "ha_pipelines.py": ["GET /api/ha-pipelines/health", "GET /api/ha-pipelines/modes", "GET /api/ha-pipelines/pipelines", "GET /api/ha-pipelines/pipelines/preferred"],
    "intent_routing.py": ["GET /api/intent-routing/providers/public", "GET /api/intent-routing/routing/public", "GET /api/intent-routing/strategy/configs/public", "GET /api/intent-routing/strategy/configs/{intent_name}"],
    "llm_backends.py": ["GET /api/llm-backends/public", "GET /api/llm-backends/public/mlx-applicability", "POST /api/llm-backends/metrics"],
    "mcp_security.py": ["GET /api/mcp-security/public", "POST /api/mcp-security/check-domain"],
    "model_config.py": ["GET /api/model-configs/presets", "GET /api/model-configs/public", "GET /api/model-configs/public/{model_name:path}"],
    "model_downloads.py": ["POST /api/model-downloads/internal/{download_id}/progress"],
    "modules.py": ["GET /api/modules/", "GET /api/modules/admin-tabs", "GET /api/modules/enabled", "GET /api/modules/{module_id}", "POST /api/modules/refresh-all", "POST /api/modules/{module_id}/refresh"],
    "music_config.py": ["GET /api/music-config/browser-playback", "GET /api/music-config/internal"],
    "performance_presets.py": ["GET /api/presets/public/active"],
    "rag_service_bypass.py": ["GET /api/rag-service-bypass", "GET /api/rag-service-bypass/{service_name}"],
    "room_audio.py": ["GET /api/room-audio/internal", "GET /api/room-audio/internal/{room_name}"],
    "room_tv.py": ["GET /api/room-tv/apps", "GET /api/room-tv/features", "GET /api/room-tv/internal", "GET /api/room-tv/internal/{room_name}"],
    "service_registry.py": ["GET /api/service-registry/services/{service_name}", "GET /api/service-registry/services/{service_name}/url"],
    "tool_calling.py": ["GET /api/tool-calling/settings/public", "GET /api/tool-calling/tools/by-name/{tool_name}/api-keys/public", "GET /api/tool-calling/tools/stats/public", "GET /api/tool-calling/tools/{tool_id}/api-keys/public", "GET /api/tool-calling/triggers/public"],
    "tool_proposals.py": ["GET /api/tool-proposals", "GET /api/tool-proposals/stats/summary", "GET /api/tool-proposals/{proposal_id}", "POST /api/tool-proposals"],
    "voice_config.py": ["GET /api/voice-config/health", "GET /api/voice-config/internal/all", "GET /api/voice-config/internal/stt", "GET /api/voice-config/internal/tts", "GET /api/voice-config/running-config", "GET /api/voice-config/services", "GET /api/voice-config/services/{service_type}", "GET /api/voice-config/stt/active", "GET /api/voice-config/stt/models", "GET /api/voice-config/tts/active", "GET /api/voice-config/tts/voices"],
    "voice_interfaces.py": ["GET /api/voice-interfaces/engines/public/stt", "GET /api/voice-interfaces/engines/public/tts", "GET /api/voice-interfaces/internal/config/{interface_name}", "GET /api/voice-interfaces/public", "GET /api/voice-interfaces/public/{interface_name}"],
}
UNREVIEWED_UNAUTHENTICATED = {
    tuple(op.split(" ", 1)): _UNREVIEWED_REASON
    for ops in _UNREVIEWED_BY_FILE.values()
    for op in ops
}

VA = "/api/voice-automations"
GATED = {
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

def test_unauthenticated_routes_are_exactly_the_reviewed_lists():
    unauth = {(m, p) for m, p, w in _operations(app) if not _is_authenticated(w)}
    allowed = set(INTENTIONALLY_PUBLIC) | set(UNREVIEWED_UNAUTHENTICATED)
    extra = sorted(unauth - allowed)
    missing = sorted(allowed - unauth)
    assert not extra and not missing, (
        f"{len(extra)} unauthenticated route(s) on no list: {extra}\n"
        f"{len(missing)} listed route(s) now authenticated or gone: {missing}"
    )


# (c) --------------------------------------------------------------------

def test_lists_are_disjoint_and_reasoned():
    public, unreviewed, gated = set(INTENTIONALLY_PUBLIC), set(UNREVIEWED_UNAUTHENTICATED), set(GATED)
    assert len(public) == 13
    assert len(unreviewed) == 93
    assert len(gated) == 46
    assert not public & unreviewed
    assert not public & gated
    assert not unreviewed & gated
    assert not USER_ONLY_PINNED & (public | unreviewed | gated)
    assert all(r.strip() for r in INTENTIONALLY_PUBLIC.values())
    assert all(r.strip() for r in UNREVIEWED_UNAUTHENTICATED.values())


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
    by_op = _walked_by_op()
    op = ("GET", "/api/cloud-llm-usage/recent")
    assert op in by_op
    assert not any(_is_authenticated(w) for w in by_op[op])


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
    assert not LEGACY_PERMISSIONLESS & (set(INTENTIONALLY_PUBLIC) | set(UNREVIEWED_UNAUTHENTICATED))
    # Every gated route is permissioned.
    by_op = _walked_by_op()
    assert all(is_permissioned(dependency_calls(w)) for op in GATED for w in by_op[op])


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

