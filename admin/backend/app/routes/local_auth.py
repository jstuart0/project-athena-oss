"""
Local authentication routes.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
import structlog
from starsessions import load_session

from app.auth.oidc import create_access_token
from app.database import get_db
from app.models import User
from app.utils.passwords import verify_password

logger = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LocalLoginRequest(BaseModel):
    username: str
    password: str


@router.post("/local-login")
async def local_login(payload: LocalLoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate a local Athena account."""
    await load_session(request)

    username = payload.username.strip()

    user = db.query(User).filter(User.username == username).first()
    if not user or user.auth_provider != "local" or not user.password_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    token = create_access_token({
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
    })

    request.session["access_token"] = token
    request.session["user_id"] = user.id
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
