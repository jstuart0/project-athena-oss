"""
Local authentication routes.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy.orm import Session
import structlog
from starsessions import load_session

from app.auth.oidc import create_access_token
from app.database import get_db
from app.models import User
from app.utils.passwords import hash_password, verify_password
from app.utils.rate_limit import login_rate_limit_dep
from shared.config import get_config

logger = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Module-level dummy hash for constant-time user lookup (xander:38).
# Pre-computed once at import; verify_password against this on the user-not-found
# path so unknown usernames pay the same PBKDF2 cost as known ones.
_DUMMY_PBKDF2_HASH = hash_password("dummy-password-not-used-for-auth")


class LocalLoginRequest(BaseModel):
    username: str
    password: str


async def _enforce_minimum_delay(start_monotonic: float) -> None:
    """Equalize all failure paths to >= login_minimum_delay_ms wall time.

    Anchored at handler entry, applied just before the failure raise.
    Successful logins skip this (helper called only in failure branches).
    """
    floor_seconds = get_config().login_minimum_delay_ms / 1000.0
    elapsed = time.monotonic() - start_monotonic
    if elapsed < floor_seconds:
        await asyncio.sleep(floor_seconds - elapsed)


@router.post(
    "/local-login",
    dependencies=[Depends(login_rate_limit_dep)],   # xander:46 / xander:32 / ian-#1
)
async def local_login(payload: LocalLoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate a local Athena account."""
    start = time.monotonic()
    await load_session(request)
    cfg = get_config()
    now = datetime.now(timezone.utc)
    username = payload.username.strip()

    user = db.query(User).filter(User.username == username).first()

    # Branch 1: user not found OR not local OR no password hash.
    # Verify against dummy hash so we pay PBKDF2 cost regardless (xander:38).
    if not user or user.auth_provider != "local" or not user.password_hash:
        verify_password(payload.password, _DUMMY_PBKDF2_HASH)
        await _enforce_minimum_delay(start)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Branch 2: inactive.  Decision D — return 401 generic to match wrong-password
    # and locked branches.  Previously 403, which was a status-code oracle for
    # "this username exists but is disabled" (codex-r1 MEDIUM).  All four failure
    # branches now return identical 401 + "Invalid username or password", honoring
    # the campaign's enumeration-protection claim.
    if not user.active:
        # xander:50 — pay PBKDF2 cost before floor sleep so this branch's wall time
        # matches branches 1 and 4. Without this the inactive/locked branches are
        # statistically distinguishable from not-found/wrong-password on slow hardware.
        verify_password(payload.password, _DUMMY_PBKDF2_HASH)
        await _enforce_minimum_delay(start)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Branch 3: locked.  Lazy-expire if the lock has passed.  OQ3 resolution:
    # locked -> 401 generic (don't disclose lockout state).
    #
    # SQLite stores DateTime(timezone=True) as a naive ISO string and reloads it
    # without tzinfo; PostgreSQL returns tz-aware values.  Normalise to UTC before
    # comparing so the handler is database-agnostic (tessa:7 / xander:41).
    locked_until_utc = user.locked_until
    if locked_until_utc is not None and locked_until_utc.tzinfo is None:
        locked_until_utc = locked_until_utc.replace(tzinfo=timezone.utc)

    if locked_until_utc and locked_until_utc > now:
        # xander:50 — pay PBKDF2 cost before floor sleep so this branch's wall time
        # matches branches 1 and 4. Without this the inactive/locked branches are
        # statistically distinguishable from not-found/wrong-password on slow hardware.
        verify_password(payload.password, _DUMMY_PBKDF2_HASH)
        await _enforce_minimum_delay(start)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    elif locked_until_utc and locked_until_utc <= now:
        # Lock expired — atomic clear via UPDATE (xander:39).
        db.execute(
            sa.update(User)
            .where(User.id == user.id)
            .values(failed_login_count=0, locked_until=None)
        )
        db.commit()
        db.refresh(user)

    # Branch 4: wrong password.  Atomic increment + conditional lock (xander:39).
    if not verify_password(payload.password, user.password_hash):
        new_count_q = (
            sa.update(User)
            .where(User.id == user.id)
            .values(failed_login_count=User.failed_login_count + 1)
            .returning(User.failed_login_count)
        )
        new_count = db.execute(new_count_q).scalar_one()
        if new_count >= cfg.login_lockout_threshold:
            db.execute(
                sa.update(User)
                .where(User.id == user.id)
                # xander:51 — only set locked_until if NULL (idempotent). Past-threshold
                # requests are no-ops; the original 30-min expiry is preserved. Prevents
                # an attacker pinning an account locked indefinitely by submitting one
                # bad-password request every 30 min.
                .where(User.locked_until.is_(None))
                .values(locked_until=now + timedelta(minutes=cfg.login_lockout_minutes))
            )
            logger.warning(
                "local_login_account_locked",
                user_id=user.id,
                username=username,            # xander:53 — log username for traceability
                failed_count=new_count,
                lockout_minutes=cfg.login_lockout_minutes,
            )
        db.commit()
        await _enforce_minimum_delay(start)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Branch 5: success.  Reset counters, issue token.
    db.execute(
        sa.update(User)
        .where(User.id == user.id)
        .values(failed_login_count=0, locked_until=None, last_login=now)
    )
    db.commit()
    db.refresh(user)

    token = create_access_token({
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
    })
    request.session["access_token"] = token
    request.session["user_id"] = int(user.id)
    request.session["auth_method"] = "local"
    logger.info("local_user_authenticated", user_id=user.id, username=user.username)

    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "auth_provider": user.auth_provider,
            "role": user.role,
            "last_login": user.last_login.isoformat() if user.last_login else None,
        }
    }
