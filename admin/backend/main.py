"""
Project Athena - Admin Interface Backend

Provides REST API for monitoring and managing Athena services.
Deploys to thor Kubernetes cluster.
"""

import os
import httpx
import socket
import subprocess
import base64
from typing import Dict, List, Any
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starsessions import SessionMiddleware, load_session
from starsessions.stores.redis import RedisStore
from redis.asyncio import Redis
from sqlalchemy.orm import Session
import structlog

from app.database import get_db, check_db_connection, init_db, DEV_MODE, seed_dev_data, seed_oss_defaults, seed_oss_features, seed_oss_conversation_settings, seed_oss_service_registry, seed_oss_base_knowledge, OSS_DEFAULT_MODEL, OSS_OLLAMA_URL, OSS_AUTO_PULL_MODELS, OSS_SEED_DEFAULTS
from shared.config import get_config
from app.services.calendar_sync import start_background_sync, stop_background_sync
from app.services.health_poller import start_health_polling, stop_health_polling
from app.auth.oidc import (
    oauth,
    get_authentik_userinfo,
    create_access_token,
    get_or_create_user,
    get_current_user,
)
from app.auth import oidc as oidc_auth
from app.models import User

# Import API route modules
from app.routes import (
    policies, secrets, devices, audit, users, services, rag_connectors, voice_tests,
    hallucination_checks, multi_intent, validation_models, conversation, llm_backends, settings,
    intent_routing, features, external_api_keys, tool_calling, base_knowledge, analytics,
    service_registry, guest_mode, component_models, service_control, internal, gateway_config,
    sms, sms_webhook, room_groups, guests, user_sessions, calendar_sources, memories,
    directions_settings, emerging_intents, music_config, room_audio, user_api_keys, room_tv,
    voice_config, voice_interfaces, mcp_security, websocket, pipeline_events, tool_proposals,
    site_scraper, performance_presets, voice_automations, alerts, follow_me, model_config,
    model_downloads, ha_pipelines, cloud_providers, cloud_llm_usage, rag_service_bypass,
    dashboard, integrations, escalation, debug_logs, modules, local_auth, oss_profiles,
    conversations
)

logger = structlog.get_logger()

if DEV_MODE:
    logger.info("dev_mode_active", message="Running in development mode with SQLite in-memory database")

# Configuration
# Service host IPs - must be set via environment variables (no hardcoded defaults)
MAC_STUDIO_IP = os.getenv("MAC_STUDIO_IP", "localhost")
MAC_MINI_IP = os.getenv("MAC_MINI_IP", "localhost")

# Mac Studio services
SERVICE_PORTS = {
    "gateway": 8000,
    "orchestrator": 8001,
    "weather": 8010,
    "airports": 8011,
    "flights": 8012,
    "events": 8013,
    "streaming": 8014,
    "news": 8015,
    "stocks": 8016,
    "sports": 8017,
    "websearch": 8018,
    "dining": 8019,
    "recipes": 8020,
    "directions": 8022,
    "validators": 8030,
    "ollama": 11434,
}

# Mac mini services (data layer + voice)
MAC_MINI_PORTS = {
    "qdrant": 6333,
    "redis": 6379,
    "piper_wyoming": 10200,
    "piper_rest": 10201,
    "whisper_wyoming": 10300,
    "whisper_rest": 10301,
}

app = FastAPI(
    title="Project Athena Admin API",
    description="Admin interface for monitoring and managing Athena services",
    version="2.0.0",  # Version 2 with authentication
    redirect_slashes=False  # Disable automatic trailing slash redirects
)

# Session middleware (Redis in production, in-memory in DEV_MODE)
SESSION_SECRET = os.getenv("SESSION_SECRET_KEY", "dev-secret-change-in-production")

if DEV_MODE:
    # DEV_MODE: Use in-memory session store (no Redis required)
    from starsessions.stores.memory import InMemoryStore
    session_store = InMemoryStore()
    logger.info("dev_mode_session_store", store="in_memory")
else:
    # Production: Use Redis backend
    REDIS_URL = get_config().redis_url
    redis_client = Redis.from_url(REDIS_URL)
    session_store = RedisStore(connection=redis_client, prefix="athena_session:")

# Session middleware - note: WebSocket connections bypass session via scope type check
# in starsessions internals
app.add_middleware(
    SessionMiddleware,
    store=session_store,
    lifetime=3600,  # 1 hour session timeout
    cookie_name="athena_session",
    cookie_same_site="lax",
    cookie_https_only=not DEV_MODE,  # True in production (HTTPS required), False in dev
)

# CORS — restrict to explicitly configured origins in production.
# Set CORS_ALLOWED_ORIGINS to a comma-separated list (e.g. "https://admin.example.com").
# Defaults to localhost only so that the wildcard is never silently active in production.
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "")
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["http://localhost:8080"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API route modules
app.include_router(policies.router)
app.include_router(secrets.router)
app.include_router(devices.router)
app.include_router(audit.router)
app.include_router(users.router)
app.include_router(services.router)
app.include_router(rag_connectors.router)
app.include_router(voice_tests.router)
app.include_router(hallucination_checks.router)
app.include_router(multi_intent.router)
app.include_router(validation_models.router)
app.include_router(conversation.router)
app.include_router(llm_backends.router)
app.include_router(settings.router)
app.include_router(intent_routing.router)
app.include_router(features.router)
app.include_router(external_api_keys.router)
app.include_router(tool_calling.router)
app.include_router(base_knowledge.router)
app.include_router(analytics.router)
app.include_router(service_registry.router)
app.include_router(guest_mode.router)
app.include_router(component_models.router)
app.include_router(service_control.router)
app.include_router(internal.router)
app.include_router(gateway_config.router)
app.include_router(sms.router)
app.include_router(sms.tips_router)  # Tips endpoints at /api/tips
app.include_router(sms_webhook.router)
app.include_router(room_groups.router)
app.include_router(guests.router)
app.include_router(user_sessions.router)
app.include_router(calendar_sources.router)
app.include_router(memories.router)
app.include_router(directions_settings.router)
app.include_router(emerging_intents.router)
app.include_router(music_config.router)
app.include_router(room_audio.router)
app.include_router(user_api_keys.router)
app.include_router(room_tv.router)
app.include_router(voice_config.router)
app.include_router(voice_interfaces.router)
app.include_router(mcp_security.router)
app.include_router(websocket.router)
app.include_router(pipeline_events.router)
app.include_router(tool_proposals.router)
app.include_router(site_scraper.router)
app.include_router(performance_presets.router)
app.include_router(voice_automations.router)
app.include_router(alerts.router)
app.include_router(follow_me.router)
app.include_router(model_config.router)
app.include_router(model_downloads.router)
app.include_router(ha_pipelines.router)
app.include_router(cloud_providers.router)
app.include_router(cloud_llm_usage.router)
app.include_router(rag_service_bypass.router)
app.include_router(dashboard.router)
app.include_router(integrations.router)
app.include_router(escalation.router)
app.include_router(debug_logs.router)
app.include_router(modules.router)
app.include_router(local_auth.router)
app.include_router(oss_profiles.router)
app.include_router(conversations.router)


async def _enforce_oidc_runtime_gates() -> None:
    """
    MED-A + MED-E production OIDC safety gates (codex r1b NEW-OQ-2 — fail-closed).

    Called unconditionally in the production startup branch, AFTER configure_oauth_client()
    has had a chance to update oidc_auth.OIDC_ISSUER from the DB.  Running inside the
    `if check_db_connection():` block would let a transient DB failure silently skip
    both gates, making the service fail-open — ian:23.

    Raises SystemExit on any validation failure.
    """
    # MED-A — re-check the runtime OIDC issuer after configure_oauth_client()
    # because that function may load a different issuer from the DB
    # (secrets.oidc_provider_url), which the earlier env-var gate cannot see.
    # A compromised or misconfigured DB row would otherwise slip past and
    # produce a runtime issuer that's empty, a placeholder, or mismatched.
    _runtime_issuer = oidc_auth.OIDC_ISSUER or ""
    if not _runtime_issuer or _runtime_issuer.startswith("CONFIGURE_ME"):
        logger.critical(
            "oidc_runtime_issuer_unset",
            source="db_or_env",
            message="Runtime OIDC issuer is empty or placeholder after configure_oauth_client() (MED-A).",
        )
        raise SystemExit(
            "FATAL: OIDC_ISSUER (loaded at runtime from DB or env) is empty or a placeholder. "
            "If using DB-stored OIDC config, ensure the secrets.oidc_provider_url row is populated. "
            "If using env vars, ensure OIDC_ISSUER is set."
        )

    # MED-E — load the IdP's discovery document and assert it contains an
    # "issuer" field that matches the configured issuer.  authlib's iss validation is
    # conditional on the discovery doc returning "issuer"; if the doc is absent, malformed,
    # or missing the field, authlib silently skips iss validation even after claims_options
    # is removed.  Fail-closed: SystemExit on fetch failure OR missing "issuer" OR mismatch.
    # (per codex r1b NEW-OQ-2: option B accepted; acceptable boot-time dependency on IdP.)
    try:
        _discovery_metadata = await oauth.authentik.load_server_metadata()
    except Exception as _e:
        logger.critical(
            "oidc_discovery_metadata_fetch_failed",
            error=str(_e),
            issuer=_runtime_issuer,
            message="OIDC discovery metadata fetch failed (MED-E). Cannot validate iss without it.",
        )
        raise SystemExit(
            f"FATAL: OIDC discovery metadata fetch failed: {_e}. "
            "Cannot validate ID-token issuer without conformant discovery metadata. "
            "Ensure OIDC_ISSUER points to a reachable, conformant IdP."
        ) from _e

    if "issuer" not in _discovery_metadata:
        logger.critical(
            "oidc_discovery_missing_issuer",
            issuer=_runtime_issuer,
            message="Discovery doc has no 'issuer' field — authlib would silently skip iss validation (MED-E).",
        )
        raise SystemExit(
            "FATAL: OIDC discovery doc has no 'issuer' field. authlib silently skips "
            "iss validation in this case. Use a conformant IdP."
        )

    # Trailing-slash normalization: both Authentik and Keycloak may include or omit a
    # trailing slash; compare stripped forms to avoid a false mismatch.
    _config_issuer = _runtime_issuer.rstrip("/")
    _doc_issuer = _discovery_metadata["issuer"].rstrip("/")
    if _doc_issuer != _config_issuer:
        logger.critical(
            "oidc_discovery_issuer_mismatch",
            configured=_config_issuer,
            discovered=_doc_issuer,
            message="Discovery doc issuer != configured OIDC_ISSUER — token validation would silently fail (MED-E).",
        )
        raise SystemExit(
            f"FATAL: OIDC discovery doc returns issuer={_doc_issuer!r} but "
            f"OIDC_ISSUER is configured as {_config_issuer!r}. "
            "Token iss validation would silently fail. Align your IdP configuration."
        )

    logger.info(
        "oidc_discovery_issuer_verified",
        issuer=_doc_issuer,
        message="Discovery doc issuer matches configured OIDC_ISSUER (MED-E gate passed).",
    )


async def _ip_identifier(request: Request) -> str:
    """Use direct peer IP only — ignore X-Forwarded-For (xander:47).

    fastapi-limiter's default_identifier trusts X-Forwarded-For unconditionally,
    which lets a client rotate the header to bypass the per-IP rate limit.
    Athena's admin-backend is reached via in-cluster Service or in-pod localhost,
    so request.client.host is the trusted Kubernetes/Traefik peer.  If a deployer
    later puts a proxy in front that DOES want X-Forwarded-For respected, they
    must opt in explicitly and configure the proxy chain — never trust by default.
    """
    return request.client.host if request.client else "unknown"


async def _init_rate_limiter(redis_conn) -> None:
    """Initialize fastapi-limiter against the shared Redis client.

    On success: set app.utils.rate_limit.LIMITER_ACTIVE = True.
    On failure: log CRITICAL, leave LIMITER_ACTIVE False, continue startup.

    The login dep no-ops when LIMITER_ACTIVE is False — DEV_MODE (init never
    called) and Redis-down (init raises) both land here gracefully (D2 /
    codex-r1 LIMITER_ACTIVE single-positive-flag design).

    Takes redis_conn explicitly (xander:33): the production `else` branch binds
    `redis_client` as a local name inside startup_event; passing it as a
    parameter avoids any NameError if this helper is called from a test context
    where `redis_client` is not in scope.

    Campaign 3 / ATHENA-14 — Phase 3 (identifier= kwarg added in Phase 4 /
    xander:47).
    """
    from fastapi_limiter import FastAPILimiter
    from app.utils import rate_limit as rate_limit_mod

    try:
        await FastAPILimiter.init(redis_conn, identifier=_ip_identifier)
        rate_limit_mod.LIMITER_ACTIVE = True
        logger.info("rate_limiter_initialized")
    except Exception as e:
        # xander:49 — reset partial-init class state (redis/identifier/http_callback
        # slots may have been written before the exception); best-effort.
        try:
            await FastAPILimiter.close()
        except Exception:
            pass

        # xander:48 — strip embedded Redis credentials from the error message before
        # logging.  redis-py exceptions can include the full REDIS_URL (with password)
        # in the message when the connection fails.
        import re
        error_msg = str(e)
        if "redis://" in error_msg and "@" in error_msg:
            error_msg = re.sub(r"redis://[^@\s]+@", "redis://***:***@", error_msg)
        logger.critical("rate_limiter_init_failed", error=error_msg, error_type=type(e).__name__)
        # LIMITER_ACTIVE stays False; login dep no-ops; lockout layer still defends.


# Startup event: Initialize database and check connections
@app.on_event("startup")
async def startup_event():
    """Initialize database and verify connections on startup."""
    logger.info("athena_admin_startup", version="2.0.0", dev_mode=DEV_MODE)

    # Campaign 3 / ATHENA-14 — Phase 4 will add atomic UPDATE...RETURNING for lockout
    # (failed_login_count increment). Pre-placed at startup so the SQLite version
    # check fires before any login attempt can hit the unsupported query. Until
    # Phase 4 ships, lockout is NOT yet enforced — the migration adds dormant
    # columns. python:3.11-slim Docker base ships SQLite 3.40+; this guard is
    # defense-in-depth for self-host deployers on older Debian/Ubuntu base images.
    import sqlite3
    from urllib.parse import urlparse
    _db_url_scheme = urlparse(get_config().database_url or "").scheme.lower()
    if _db_url_scheme.startswith("sqlite") and sqlite3.sqlite_version_info < (3, 35, 0):
        raise SystemExit(
            f"FATAL: SQLite >= 3.35 required for UPDATE...RETURNING used by local-login lockout "
            f"(Campaign 3 / ATHENA-14); found {sqlite3.sqlite_version}. "
            f"Upgrade SQLite or run on PostgreSQL."
        )

    if DEV_MODE:
        # DEV_MODE: Use SQLite in-memory, skip external database checks
        logger.info("dev_mode_startup", message="Using SQLite in-memory database")

        # xander:6 — DEV_MODE auto-creates an unauthenticated owner admin (oidc.py:401-421).
        # Pairing it with a real DATABASE_URL lets that owner persist in production data.
        # Use startswith("sqlite") rather than SQLAlchemy URL parsing — the threat is a
        # deployer with a real PostgreSQL URL, not a malicious URL trying to evade detection;
        # prefix-check is sufficient for the legitimate-misconfig case (see plan D-tradeoff).
        _db_url = get_config().database_url
        if _db_url and not _db_url.startswith("sqlite"):
            logger.critical(
                "dev_mode_with_real_database",
                database_url_kind=_db_url.split("://", 1)[0],  # scheme only, NOT credentials
                message="DEV_MODE=true with a non-SQLite DATABASE_URL is a deploy-shape mismatch (xander:6).",
            )
            raise SystemExit(
                "FATAL: DEV_MODE=true is incompatible with a non-SQLite DATABASE_URL. "
                "DEV_MODE auto-creates an unauthenticated owner admin (oidc.py:401-421); pairing it "
                "with a real database grants owner-level API access without credentials. "
                "Set DEV_MODE=false for production, or use a SQLite DATABASE_URL for local dev."
            )

        # Initialize database schema
        try:
            init_db()
            logger.info("database_schema_initialized")

            # Seed dev data (admin user, default settings)
            seed_dev_data()
            logger.info("dev_mode_data_seeded")
        except Exception as e:
            logger.error("database_init_failed", error=str(e))

        # Skip OIDC configuration in DEV_MODE
        logger.info("dev_mode_oidc_skipped", message="OIDC not configured in DEV_MODE")

    else:
        # Production: Reject insecure default secrets and known OIDC placeholders at startup.
        # This prevents silent deployment with known-public placeholder values.
        #
        # Dict shape: var_key → (placeholder_value, kind)
        #   kind="secret"      — env var must be non-empty AND must not equal the placeholder
        #   kind="client_id"   — env var must not equal the placeholder (exact-match only;
        #                        empty OIDC_CLIENT_ID is caught by the standalone xander:17
        #                        check below, NOT by this loop)
        #
        # HIGH-A / xander:1 — client_id entries are read via get_config().oidc_client_id
        # (pydantic-settings strips whitespace), NOT os.getenv.  A raw os.getenv read would
        # allow " demo-mode" (leading space) to bypass the gate while the runtime check at
        # main.py:414 (which goes through get_config()) still activates the demo bypass.
        #
        # codex-M2 — _OIDC_CLIENT_ID_CONFIGURE_ME is a synthetic dict key used to register
        # the second OIDC_CLIENT_ID placeholder (the value scripts/create-secrets.sh writes
        # when the real client_id is absent).  Both entries read from get_config().oidc_client_id
        # via _read_var() below.  The synthetic key never touches os.getenv.
        _INSECURE_DEFAULTS: dict = {
            # key: (placeholder_value, kind)
            "SESSION_SECRET_KEY": ("dev-secret-change-in-production", "secret"),
            "JWT_SECRET": ("dev-secret-change-in-production", "secret"),
            "SERVICE_API_KEY": ("dev-service-key-change-in-production", "secret"),
            # xander:13 — demo-mode activates the demo auth bypass (oidc.py:401-421)
            "OIDC_CLIENT_ID": ("demo-mode", "client_id"),
            # codex-M2 — scripts/create-secrets.sh writes this placeholder when OIDC_CLIENT_ID
            # is absent; the script's contract says the backend rejects it.  Honor that contract.
            "_OIDC_CLIENT_ID_CONFIGURE_ME": ("CONFIGURE_ME_OIDC_CLIENT_ID", "client_id_configure_me"),
        }
        _MESSAGE_BY_KIND = {
            "secret": (
                "FATAL: {var}={val!r} is a publicly-known development placeholder. "
                "Generate a secure random value (e.g., `openssl rand -hex 32`) and set "
                "{var} in your environment or kubernetes secret."
            ),
            "client_id": (
                "FATAL: OIDC_CLIENT_ID is set to the publicly-known placeholder {val!r}, "
                "which activates the demo authentication bypass. "
                "Set OIDC_CLIENT_ID to your IdP's real client_id, or set DEV_MODE=true for development."
            ),
            "client_id_configure_me": (
                "FATAL: OIDC_CLIENT_ID is still set to {val!r}, the placeholder written by "
                "scripts/create-secrets.sh when OIDC_CLIENT_ID was unset at deploy time. "
                "Replace it with your IdP's real client_id before starting the admin backend."
            ),
        }

        def _read_var(name: str) -> str:
            # client_id entries share the same real env var (OIDC_CLIENT_ID) and must be read
            # through pydantic-settings so whitespace normalization matches the runtime check.
            if name in ("OIDC_CLIENT_ID", "_OIDC_CLIENT_ID_CONFIGURE_ME"):
                return get_config().oidc_client_id or ""
            return os.getenv(name, "")

        for _var, (_bad, _kind) in _INSECURE_DEFAULTS.items():
            _val = _read_var(_var)
            # secret: fail on empty OR matching placeholder.
            # client_id / client_id_configure_me: fail on exact-match only (exact equality).
            if (_kind == "secret" and (not _val or _val == _bad)) or (
                _kind in ("client_id", "client_id_configure_me") and _val == _bad
            ):
                # Map synthetic dict key back to the real env var name in logs and messages.
                _display_var = "OIDC_CLIENT_ID" if _var.startswith("_OIDC_CLIENT_ID") else _var
                logger.critical("insecure_default_secret_detected", var=_display_var, value_kind=_kind)
                raise SystemExit(
                    _MESSAGE_BY_KIND[_kind].format(var=_display_var, val=_bad)
                )

        # xander:16 — DEMO_MODE=true in production reaches the demo-admin bypass at
        # main.py:474 (auth_login issues an unauthenticated owner JWT for admin@demo.local)
        # regardless of OIDC config. Standalone check because DEMO_MODE is a boolean flag,
        # not a placeholder string — the _INSECURE_DEFAULTS dict shape doesn't cover it.
        if get_config().demo_mode:
            logger.critical(
                "demo_mode_in_production",
                message="DEMO_MODE=true is not permitted with DEV_MODE=false (xander:16).",
            )
            raise SystemExit(
                "FATAL: DEMO_MODE=true is not permitted in production. "
                "DEMO_MODE bypasses OIDC and creates an unauthenticated owner admin "
                "(main.py:474). Set DEMO_MODE=false, or set DEV_MODE=true for development."
            )

        # xander:17 — empty OIDC_CLIENT_ID lands at oauth.register(client_id=""), which
        # permissive IdPs may accept (confused-deputy risk). The _INSECURE_DEFAULTS loop
        # uses exact-match for client_id kinds (intentional, to support boundary tests),
        # so empty values pass through. Catch it here.
        if not get_config().oidc_client_id:
            logger.critical(
                "oidc_client_id_unset",
                message="OIDC_CLIENT_ID is empty (xander:17).",
            )
            raise SystemExit(
                "FATAL: OIDC_CLIENT_ID is empty. The OIDC client cannot be registered "
                "without a client_id. Set OIDC_CLIENT_ID to your IdP's real client_id, "
                "or set DEV_MODE=true for development."
            )

        # OIDC issuer hard-fail: empty or CONFIGURE_ME-style placeholder is unsafe
        # in production because it silently routes auth to no IdP (or the wrong one).
        # Escape valve: DEV_MODE=true (handled above).
        _oidc_issuer = get_config().oidc_issuer
        if not _oidc_issuer or _oidc_issuer.startswith("CONFIGURE_ME"):
            logger.critical("oidc_issuer_not_configured")
            raise SystemExit(
                "FATAL: OIDC_ISSUER must be set in production. "
                "Set DEV_MODE=true for development."
            )

        # Production: Check PostgreSQL connection
        if check_db_connection():
            logger.info("database_connection_healthy")

            # Initialize database schema (creates tables if they don't exist)
            try:
                init_db()
                logger.info("database_schema_initialized")
            except Exception as e:
                logger.error("database_schema_init_failed", error=str(e))

            # Seed OSS default configuration (LLM backends, component models, feature flags)
            if OSS_SEED_DEFAULTS:
                try:
                    seed_oss_defaults()
                    logger.info("oss_defaults_seeded")
                except Exception as e:
                    logger.error("oss_defaults_seeding_failed", error=str(e))

                try:
                    seed_oss_features()
                    logger.info("oss_features_seeded")
                except Exception as e:
                    logger.error("oss_features_seeding_failed", error=str(e))

                try:
                    seed_oss_conversation_settings()
                    logger.info("oss_conversation_settings_seeded")
                except Exception as e:
                    logger.error("oss_conversation_settings_seeding_failed", error=str(e))

                # Startup gate: alembic migration 055 must have run before we seed.
                # Assert the Phase 2 columns exist so the seed's UPSERT succeeds.
                # (bob C2 / otto C2 / ian I-H2 / ATHENA-1 Phase 2)
                try:
                    from sqlalchemy import inspect as _sa_inspect
                    from app.database import engine as _engine
                    _inspector = _sa_inspect(_engine)
                    _cols = {c['name'] for c in _inspector.get_columns('athena_service_registry')}
                    _required = {
                        'host', 'port', 'protocol', 'health_endpoint',
                        'control_method', 'is_running', 'last_response_time_ms',
                        'last_error', 'health_message', 'auto_start',
                        'description', 'api_key_encrypted',
                    }
                    _missing = _required - _cols
                    if _missing:
                        raise RuntimeError(
                            f"Admin-backend startup aborted: athena_service_registry table is missing "
                            f"columns {sorted(_missing)}. "
                            f"Run `alembic upgrade head` before starting admin-backend."
                        )
                    seed_oss_service_registry()
                    logger.info("oss_service_registry_seeded")
                except Exception as e:
                    logger.error("oss_service_registry_seeding_failed", error=str(e))
                    raise  # Propagate — a missing migration is a fatal misconfiguration.

                try:
                    seed_oss_base_knowledge()
                    logger.info("oss_base_knowledge_seeded")
                except Exception as e:
                    logger.error("oss_base_knowledge_seeding_failed", error=str(e))
            else:
                logger.info("oss_defaults_seeding_disabled", reason="ATHENA_SEED_DEFAULTS=false")

            # Configure OAuth/OIDC from database.
            # No try/except: an exception here (e.g., malformed DB row, oauth.register()
            # failure) propagates and the process exits.  _enforce_oidc_runtime_gates()
            # below is the safety net for partial-state cases (DB-path set a bad issuer).
            from app.auth.oidc import configure_oauth_client
            configure_oauth_client()
            logger.info("oidc_configuration_loaded")
        else:
            logger.error("database_connection_failed")

        # MED-A + MED-E (ian:23 fix): gates run UNCONDITIONALLY in the production branch
        # regardless of DB reachability.  Running them inside `if check_db_connection():`
        # made the service fail-open when the DB was transiently unreachable at boot —
        # neither gate would execute and the service would start without OIDC validation.
        await _enforce_oidc_runtime_gates()
        await _init_rate_limiter(redis_client)  # Campaign 3 / ATHENA-14 — Phase 3

    # Check and pull default LLM model if needed (background task)
    if OSS_AUTO_PULL_MODELS:
        try:
            await ensure_default_model()
        except Exception as e:
            logger.warning("model_check_failed", error=str(e), model=OSS_DEFAULT_MODEL)
    else:
        logger.info("auto_pull_disabled", reason="ATHENA_AUTO_PULL_MODELS=false")

    logger.info("athena_admin_ready", dev_mode=DEV_MODE)

    # Start background calendar sync task
    start_background_sync()

    # Start background health poll task (Phase 4 / ATHENA-1)
    # DEV_MODE has no Redis client; production passes redis_client for leader election.
    if DEV_MODE:
        start_health_polling()
    else:
        start_health_polling(redis_client=redis_client)


async def ensure_default_model():
    """
    Ensure the default LLM model is available in Ollama.

    Checks if the model exists and pulls it if not available.
    Runs as a background task to avoid blocking startup.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Check if Ollama is reachable
            try:
                response = await client.get(f"{OSS_OLLAMA_URL}/api/tags")
                if response.status_code != 200:
                    logger.warning("ollama_not_reachable", url=OSS_OLLAMA_URL)
                    return
            except Exception as e:
                logger.warning("ollama_connection_failed", url=OSS_OLLAMA_URL, error=str(e))
                return

            # Check if model exists
            models_data = response.json()
            models = models_data.get("models", [])
            model_names = [m.get("name", "") for m in models]

            if OSS_DEFAULT_MODEL in model_names:
                logger.info("default_model_available", model=OSS_DEFAULT_MODEL)
                return

            # Check if model name without tag exists (e.g., "qwen3:4b" might be stored as "qwen3:4b")
            model_base = OSS_DEFAULT_MODEL.split(":")[0]
            matching_models = [m for m in model_names if m.startswith(model_base)]
            if matching_models:
                logger.info("default_model_variant_available", model=OSS_DEFAULT_MODEL, found=matching_models[0])
                return

            # Model not found, attempt to pull it
            logger.info("pulling_default_model", model=OSS_DEFAULT_MODEL)

            # Use longer timeout for model pull
            async with httpx.AsyncClient(timeout=600.0) as pull_client:
                response = await pull_client.post(
                    f"{OSS_OLLAMA_URL}/api/pull",
                    json={"name": OSS_DEFAULT_MODEL, "stream": False}
                )

                if response.status_code == 200:
                    logger.info("default_model_pulled_successfully", model=OSS_DEFAULT_MODEL)
                else:
                    logger.warning("default_model_pull_failed",
                                 model=OSS_DEFAULT_MODEL,
                                 status=response.status_code,
                                 response=response.text[:200])

    except Exception as e:
        logger.warning("ensure_default_model_error", error=str(e), model=OSS_DEFAULT_MODEL)


# Shutdown event: Clean up background tasks
@app.on_event("shutdown")
async def shutdown_event():
    """Clean up background tasks on shutdown."""
    logger.info("athena_admin_shutdown")
    await stop_background_sync()
    await stop_health_polling()


# Authentication routes
@app.get("/auth/login")
@app.get("/api/auth/login")
async def auth_login(request: Request, db: Session = Depends(get_db)):
    """Initiate OIDC login flow with Authentik."""
    # Explicitly load session for starsessions compatibility with authlib
    await load_session(request)

    # Demo mode bypass for development/testing
    if get_config().demo_mode or get_config().oidc_client_id == "demo-mode":
        # Create demo userinfo dictionary
        demo_userinfo = {
            "sub": "demo-admin",
            "email": "admin@demo.local",
            "preferred_username": "admin",
            "name": "Demo Admin",
            "groups": ["admin"]
        }

        # Create or get demo user using the proper function signature
        demo_user = get_or_create_user(db=db, userinfo=demo_userinfo)
        if demo_user.role != "owner":
            demo_user.role = "owner"
            db.commit()
            db.refresh(demo_user)

        # Create demo token with proper data structure
        demo_token = create_access_token(
            data={
                "sub": demo_user.authentik_id,
                "user_id": demo_user.id,  # Required by get_current_user
                "email": demo_user.email,
                "username": demo_user.username,
                "full_name": demo_user.full_name,
                "groups": ["admin"]
            }
        )

        # xander:4 — store JWT in session; never emit JWT in URL.
        # xander:5 — explicit int() cast for JSON serialization safety on session store.
        request.session['access_token'] = demo_token
        request.session['user_id'] = int(demo_user.id)
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8080")
        # ?logged_in=1 is a client-side hint to clear stale localStorage before the
        # frontend fetches the session token (codex-H1 mitigation for shared devices).
        # NOT a security boundary — spoofing this param only triggers a localStorage clear.
        return RedirectResponse(url=f"{frontend_url}?logged_in=1")

    # Normal OAuth flow
    # Use explicit HTTPS redirect URI from environment (not request.url_for which returns HTTP)
    redirect_uri = os.getenv("OIDC_REDIRECT_URI", "http://localhost:8080/auth/callback")
    logger.info("auth_login_redirect", redirect_uri=redirect_uri)
    return await oauth.authentik.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
@app.get("/api/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    """
    OIDC callback endpoint.

    Exchanges authorization code for tokens, fetches user info,
    creates/updates user in database, and returns JWT token.
    """
    # Explicitly load session for starsessions compatibility with authlib
    await load_session(request)

    logger.info(
        "auth_callback_received",
        has_code=bool(request.query_params.get("code")),
        has_state=bool(request.query_params.get("state")),
    )
    try:
        # Exchange authorization code for tokens. authlib's parse_id_token validates
        # iss (against discovery metadata's "issuer" field), aud (against the registered
        # client_id via IDToken.validate_azp), and exp by default.  The claims_options
        # override that disabled these checks has been removed (xander:3 / Phase 3).
        # A startup gate (MED-E) asserts the discovery doc contains "issuer" so that
        # authlib's conditional iss validation is never silently skipped.
        token = await oauth.authentik.authorize_access_token(request)
        access_token = token.get('access_token')
        logger.debug("token_exchange_complete", has_access_token=bool(access_token))

        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

        # Get user info from Authentik using authlib's built-in method
        # This properly handles the userinfo endpoint discovery and auth
        userinfo = await oauth.authentik.userinfo(token=token)
        logger.debug("userinfo_received", userinfo_keys=list(userinfo.keys()) if userinfo else None)

        # Create or update user in database
        user = get_or_create_user(db, userinfo)

        # Create internal JWT token
        jwt_token = create_access_token({
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
        })

        # Store token in session
        request.session['access_token'] = jwt_token
        request.session['user_id'] = user.id

        logger.info("user_authenticated", user_id=user.id, username=user.username)

        # xander:4 — session already populated at the 'request.session' lines above;
        # redirect with a callback-landing signal only — no JWT in URL.
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8080")
        return RedirectResponse(url=f"{frontend_url}?logged_in=1")

    except Exception as e:
        logger.error("auth_callback_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Authentication failed")


@app.get("/auth/logout")
@app.get("/api/auth/logout")
async def auth_logout(request: Request):
    """Logout user and clear session."""
    # Explicitly load session for starsessions compatibility
    await load_session(request)
    request.session.clear()
    logger.info("user_logged_out")

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8080")
    return RedirectResponse(url=frontend_url)


@app.get("/auth/me")
@app.get("/api/auth/me")
async def auth_me(current_user: User = Depends(get_current_user)):
    """Get current authenticated user information."""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "auth_provider": current_user.auth_provider,
        "role": current_user.role,
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None,
    }


@app.get("/api/auth/methods")
async def get_auth_methods():
    """Expose enabled admin authentication methods for the frontend."""
    demo_mode = get_config().demo_mode or get_config().oidc_client_id == "demo-mode"
    oidc_enabled = demo_mode or bool(oidc_auth.OIDC_CLIENT_ID)
    return {
        "oidc_enabled": oidc_enabled,
        "local_enabled": True,
        "demo_mode": demo_mode,
    }


@app.get("/api/auth/session-token")
async def get_session_token(request: Request):
    """
    Get JWT token from session.

    This endpoint allows the frontend to retrieve the JWT token stored in session
    if localStorage is empty (e.g., after page refresh or when token was cleared).
    The frontend can then store it in localStorage for subsequent API calls.
    """
    await load_session(request)

    access_token = request.session.get('access_token')
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="No session token available"
        )

    return {"token": access_token}


class ServiceStatus(BaseModel):
    """Service status model."""
    name: str
    port: int
    healthy: bool
    status: str
    version: str = "unknown"
    error: str = None


class SystemStatus(BaseModel):
    """Overall system status model."""
    healthy_services: int
    total_services: int
    overall_health: str
    services: List[ServiceStatus]


@app.get("/health")
async def health_check():
    """Health check for admin API itself."""
    return {
        "status": "healthy",
        "service": "athena-admin",
        "version": "1.0.0"
    }


@app.get("/status", response_model=SystemStatus)
@app.get("/api/status", response_model=SystemStatus)
async def get_system_status(current_user: User = Depends(get_current_user)):
    """Get status of all Athena services."""
    service_statuses = []

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Check Mac Studio services
        for service_name, port in SERVICE_PORTS.items():
            status = ServiceStatus(
                name=f"{service_name} (studio)",
                port=port,
                healthy=False,
                status="unknown"
            )

            try:
                # Special handling for different service types
                if service_name == "whisper" or service_name == "piper":
                    # Whisper and Piper use Wyoming protocol (TCP), check via socket
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(2)
                        result = sock.connect_ex((MAC_STUDIO_IP, port))
                        sock.close()

                        if result == 0:
                            status.healthy = True
                            status.status = "running"
                        else:
                            status.status = "error"
                            status.error = f"Connection failed: {result}"
                        service_statuses.append(status)
                        continue  # Skip HTTP check
                    except Exception as e:
                        status.status = "error"
                        status.error = str(e)
                        service_statuses.append(status)
                        continue

                # HTTP-based health checks
                if service_name == "ollama":
                    url = f"http://{MAC_STUDIO_IP}:{port}/api/tags"
                else:
                    url = f"http://{MAC_STUDIO_IP}:{port}/health"

                response = await client.get(url)

                if response.status_code == 200:
                    data = response.json()
                    status.healthy = True
                    status.status = "running"
                    status.version = data.get("version", "unknown")
                elif response.status_code == 401:
                    # Gateway returns 401 for unauthenticated health checks
                    status.healthy = True
                    status.status = "running (auth required)"
                else:
                    status.status = f"error: HTTP {response.status_code}"
                    status.error = f"Unexpected status code: {response.status_code}"

            except httpx.TimeoutException:
                status.status = "timeout"
                status.error = "Service did not respond within timeout"
            except Exception as e:
                status.status = "error"
                status.error = str(e)

            service_statuses.append(status)

        # Check Mac mini services (optional - graceful degradation)
        for service_name, port in MAC_MINI_PORTS.items():
            status = ServiceStatus(
                name=f"{service_name} (mini)",
                port=port,
                healthy=False,
                status="not deployed"
            )

            try:
                # Wyoming protocol services (whisper/piper) use TCP, not HTTP
                if service_name in ("redis", "whisper_wyoming", "whisper_rest", "piper_wyoming", "piper_rest"):
                    # TCP socket check
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(2)
                        result = sock.connect_ex((MAC_MINI_IP, port))
                        sock.close()

                        if result == 0:
                            status.healthy = True
                            status.status = "running"
                        else:
                            status.status = "not deployed (optional)"
                            status.error = "Service not yet deployed - system works without this"
                    except Exception as e:
                        status.status = "not deployed (optional)"
                        status.error = "Service not yet deployed - system works without this"
                else:
                    # HTTP-based health checks for other services
                    if service_name == "qdrant":
                        url = f"http://{MAC_MINI_IP}:{port}/healthz"
                    else:
                        url = f"http://{MAC_MINI_IP}:{port}/health"

                    response = await client.get(url)

                    if response.status_code == 200:
                        status.healthy = True
                        status.status = "running"
                    else:
                        status.status = f"error: HTTP {response.status_code}"
                        status.error = f"Unexpected status code: {response.status_code}"

            except httpx.ConnectError:
                status.status = "not deployed (optional)"
                status.error = "Service not yet deployed - system works without this"
            except httpx.TimeoutException:
                status.status = "not deployed (optional)"
                status.error = "Service not yet deployed - system works without this"
            except Exception as e:
                status.status = "not deployed (optional)"
                status.error = "Service not yet deployed - system works without this"

            service_statuses.append(status)

        # Check SearXNG (cluster-local service)
        searxng_status = ServiceStatus(
            name="searxng (search)",
            port=8080,
            healthy=False,
            status="unknown"
        )

        try:
            url = "http://searxng.athena-admin.svc.cluster.local:8080/healthz"
            response = await client.get(url)

            if response.status_code == 200:
                searxng_status.healthy = True
                searxng_status.status = "running"
            else:
                searxng_status.status = f"error: HTTP {response.status_code}"
                searxng_status.error = f"Unexpected status code: {response.status_code}"

        except httpx.ConnectError:
            searxng_status.status = "not deployed"
            searxng_status.error = "SearXNG service not accessible"
        except httpx.TimeoutException:
            searxng_status.status = "timeout"
            searxng_status.error = "Service did not respond within timeout"
        except Exception as e:
            searxng_status.status = "error"
            searxng_status.error = str(e)

        service_statuses.append(searxng_status)

    healthy_count = sum(1 for s in service_statuses if s.healthy)
    total_count = len(service_statuses)

    overall_health = "healthy" if healthy_count == total_count else \
                    "degraded" if healthy_count > total_count * 0.5 else "critical"

    return SystemStatus(
        healthy_services=healthy_count,
        total_services=total_count,
        overall_health=overall_health,
        services=service_statuses
    )


# Mount static files for frontend (must be LAST, after all API routes)
# This serves the admin frontend at the root path
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
