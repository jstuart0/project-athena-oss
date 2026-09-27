"""auth.py — Control Agent authentication (ATHENA-110).

FastAPI dependency gating every *mutating* Control Agent route behind
`X-Service-Key`. Applied via `dependencies=[Depends(require_service_caller)]`
on each route decorator in main.py, never as a blanket app-level dependency
— read-only routes stay open.

The Control Agent has no `DEV_MODE` concept (unlike admin-backend and the
orchestrator): it is a single-tenant host agent that runs one deployment's
services, not a multi-mode server with a local-dev bypass. There is
therefore no step 3 "allow if dev mode" the way
`orchestrator/ingress_auth.py::require_service_caller` has one — every
mutating route requires a valid key, unconditionally, in every environment.

Gated (mutating) routes, enumerated in main.py at each decorator:
  /process/start|stop|restart/{port}
  /docker/start|stop|restart/{container_name}
  /ollama/start|stop|restart
  /huggingface/download (POST), /huggingface/download/{job_id} (DELETE),
    /huggingface/import-to-ollama (POST), /huggingface/downloaded (DELETE)
  /watchdog/enable|disable|exclude/{port}|include/{port}

NOT gated (read-only, no destructive action; unauthenticated LAN visibility
is an accepted trade for simple operator tooling — curl-based health
checks without a key):
  /health, /docker/list, /docker/status/{name}, /ollama/status,
  /ollama/health, /process/list, /process/status/{port},
  /huggingface/search, /huggingface/repo/{repo_id}/files,
  /huggingface/download/{job_id}/status, /huggingface/downloaded (GET),
  /debug-logs/status|files|search|tail/{filename}, /watchdog/status

Decision order:
1. Configured key (`SERVICE_API_KEY`) is empty -> 503 "service key not
   configured" for EVERY request, key present or not (fail closed — a
   route that's supposed to require auth must never fail open just
   because nobody set the env var). Logged once at process startup via
   `warn_if_service_key_unset()`, not per-request, to avoid log spam.
2. Header present and matches (constant-time) -> allow.
3. Header missing, empty, or present-but-wrong -> 401. A wrong key logs
   WARNING `control_agent_bad_service_key`; a missing key does not (that's
   the expected shape of every unauthenticated probe on a LAN host and
   would be pure log spam).
"""
from __future__ import annotations

import hmac
import os

import structlog
from fastapi import HTTPException, Request

logger = structlog.get_logger()

_warned_key_unset = False


def _configured_key() -> str:
    return os.getenv("SERVICE_API_KEY", "").strip()


def warn_if_service_key_unset() -> None:
    """Call once, at process startup (from `lifespan`). Logs a single
    WARNING if SERVICE_API_KEY is unset/empty, since every mutating
    Control Agent route will 503 until it's configured. Idempotent —
    safe to call more than once; only the first call logs."""
    global _warned_key_unset
    if _warned_key_unset:
        return
    _warned_key_unset = True
    if not _configured_key():
        logger.warning(
            "control_agent_service_key_unset",
            note=(
                "SERVICE_API_KEY is empty; every mutating Control Agent route "
                "(process/docker/ollama/watchdog/huggingface-download) will "
                "return 503 until it is set."
            ),
        )


def reset_for_test() -> None:
    """Test-only: reset the one-time startup-warning flag."""
    global _warned_key_unset
    _warned_key_unset = False


async def require_service_caller(request: Request) -> None:
    """FastAPI dependency: gate a mutating route behind X-Service-Key."""
    configured_key = _configured_key()
    header_value = request.headers.get("X-Service-Key")

    if not configured_key:
        raise HTTPException(status_code=503, detail="service key not configured")

    if header_value and hmac.compare_digest(header_value, configured_key):
        return

    if header_value:
        logger.warning(
            "control_agent_bad_service_key",
            path=request.url.path,
            client_host=request.client.host if request.client else None,
        )

    raise HTTPException(status_code=401, detail="Invalid or missing service key")
