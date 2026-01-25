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

from app.database import get_db, check_db_connection, init_db, DEV_MODE, seed_dev_data, seed_oss_defaults, seed_oss_features, seed_oss_conversation_settings, OSS_DEFAULT_MODEL, OSS_OLLAMA_URL, OSS_AUTO_PULL_MODELS, OSS_SEED_DEFAULTS
from app.services.calendar_sync import start_background_sync, stop_background_sync
from app.auth.oidc import (
    oauth,
    get_authentik_userinfo,
    create_access_token,
    get_or_create_user,
    get_current_user,
)
from app.models import User

# Import API route modules
from app.routes import (
    policies, secrets, devices, audit, users, servers, services, rag_connectors, voice_tests,
    hallucination_checks, multi_intent, validation_models, conversation, llm_backends, settings,
    intent_routing, features, external_api_keys, tool_calling, base_knowledge, analytics,
    service_registry, guest_mode, component_models, service_control, internal, gateway_config,
    sms, sms_webhook, room_groups, guests, user_sessions, calendar_sources, memories,
    directions_settings, emerging_intents, music_config, room_audio, user_api_keys, room_tv,
    voice_config, voice_interfaces, mcp_security, websocket, pipeline_events, tool_proposals,
    site_scraper, performance_presets, voice_automations, alerts, follow_me, model_config,
    model_downloads, ha_pipelines, cloud_providers, cloud_llm_usage, rag_service_bypass,
    dashboard, integrations, escalation, debug_logs, modules
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
    REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
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
    cookie_https_only=False,  # Set True in production with HTTPS
)

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly in production
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
app.include_router(servers.router)
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


# Startup event: Initialize database and check connections
@app.on_event("startup")
async def startup_event():
    """Initialize database and verify connections on startup."""
    logger.info("athena_admin_startup", version="2.0.0", dev_mode=DEV_MODE)

    if DEV_MODE:
        # DEV_MODE: Use SQLite in-memory, skip external database checks
        logger.info("dev_mode_startup", message="Using SQLite in-memory database")

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
            else:
                logger.info("oss_defaults_seeding_disabled", reason="ATHENA_SEED_DEFAULTS=false")

            # Configure OAuth/OIDC from database
            try:
                from app.auth.oidc import configure_oauth_client
                configure_oauth_client()
                logger.info("oidc_configuration_loaded")
            except Exception as e:
                logger.error("oidc_configuration_failed", error=str(e))
        else:
            logger.error("database_connection_failed")

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


# Authentication routes
@app.get("/auth/login")
@app.get("/api/auth/login")
async def auth_login(request: Request, db: Session = Depends(get_db)):
    """Initiate OIDC login flow with Authentik."""
    # Explicitly load session for starsessions compatibility with authlib
    await load_session(request)

    # Demo mode bypass for development/testing
    if os.getenv("DEMO_MODE", "false").lower() == "true" or os.getenv("OIDC_CLIENT_ID") == "demo-mode":
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

        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8080")
        return RedirectResponse(url=f"{frontend_url}?token={demo_token}")

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

    logger.info("auth_callback_received", query_params=str(request.query_params))
    try:
        # Exchange authorization code for tokens
        # Skip ID token validation - we fetch userinfo directly
        token = await oauth.authentik.authorize_access_token(
            request,
            claims_options={
                "iss": {"essential": False},
                "aud": {"essential": False},
                "exp": {"essential": False}
            }
        )
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

        # Redirect to frontend with token
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8080")
        return RedirectResponse(url=f"{frontend_url}?token={jwt_token}")

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
        "role": current_user.role,
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None,
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
async def get_system_status():
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


@app.get("/services")
async def list_services():
    """List all configured services."""
    return {
        "services": [
            {"name": name, "port": port, "url": f"http://{MAC_STUDIO_IP}:{port}"}
            for name, port in SERVICE_PORTS.items()
        ]
    }


@app.post("/test-query")
async def test_query(query: str = "what is 2+2?"):
    """Test a query against the orchestrator."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.post(
                f"http://{MAC_STUDIO_IP}:8001/v1/chat/completions",
                json={"messages": [{"role": "user", "content": query}]}
            )

            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "response": data["choices"][0]["message"]["content"],
                    "metadata": data.get("athena_metadata", {})
                }
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}",
                    "details": response.text
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }


# Mount static files for frontend (must be LAST, after all API routes)
# This serves the admin frontend at the root path
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
