"""Rate-limiter wiring for the local-login endpoint (xander:10 / ATHENA-14).

LIMITER_ACTIVE flag is set True only on successful FastAPILimiter.init().
Single positive truth condition handles all three states:
  - DEV_MODE never inits → False → dep no-ops
  - Redis-fail at startup → init raises → False → dep no-ops
  - Successful init → True → dep enforces

Importing this module does NOT initialize the limiter.  _init_rate_limiter()
in main.py performs init at startup.
"""
from fastapi import Request, Response
from fastapi_limiter.depends import RateLimiter

from shared.config import get_config

LIMITER_ACTIVE: bool = False


async def login_rate_limit_dep(request: Request, response: Response) -> None:
    """FastAPI dependency for /local-login.

    Resolves LIMITER_ACTIVE at REQUEST time (not import time — bob:1 / xander:32 /
    codex-r1 fix).  When the limiter wasn't initialized (DEV_MODE or Redis-down),
    this dep is a no-op and request flow continues to the lockout layer.
    """
    if not LIMITER_ACTIVE:
        return
    cfg = get_config()
    await RateLimiter(times=cfg.login_rate_limit_per_minute, seconds=60)(request, response)
