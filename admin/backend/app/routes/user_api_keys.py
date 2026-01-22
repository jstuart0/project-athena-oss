"""
User API key management routes.

Users can create, list, and revoke their own API keys.
Admins can manage any user's keys.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, UserAPIKey, AuditLog
from app.utils.api_keys import (
    generate_api_key,
    hash_api_key,
    extract_key_prefix,
    calculate_expiration,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/user-api-keys", tags=["user-api-keys"])


# ============================================================================
# Audit Logging
# ============================================================================

def create_audit_log(
    db: Session,
    user: User,
    action: str,
    key: UserAPIKey,
    request: Request = None,
    old_value: dict = None,
    new_value: dict = None,
):
    """Create audit log entry for API key operations."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='user_api_key',
        resource_id=key.id if key else None,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='user_api_key', resource_id=key.id if key else None)


# ============================================================================
# Request/Response Models
# ============================================================================

class UserAPIKeyCreate(BaseModel):
    """Request model for creating an API key."""
    name: str = Field(..., min_length=1, max_length=255, description="Display name for this key")
    scopes: List[str] = Field(
        ...,
        min_length=1,
        description="Permission scopes (e.g., 'read:devices', 'write:features', 'read:*')"
    )
    expires_in_days: Optional[int] = Field(
        90,
        ge=1,
        le=365,
        description="Days until expiration (1-365, default 90)"
    )
    reason: Optional[str] = Field(None, max_length=500, description="Why this key was created")


class UserAPIKeyResponse(BaseModel):
    """Response model for API key metadata (key never included after creation)."""
    id: int
    name: str
    key_prefix: str
    scopes: List[str]
    created_at: datetime
    expires_at: Optional[datetime]
    last_used_at: Optional[datetime]
    request_count: int
    revoked: bool
    revoked_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserAPIKeyCreated(UserAPIKeyResponse):
    """Response for key creation (includes raw key, shown ONLY ONCE)."""
    api_key: str = Field(..., description="Raw API key - SAVE THIS, it will never be shown again")


class UserAPIKeyRevoke(BaseModel):
    """Request model for revoking a key."""
    reason: Optional[str] = Field(None, max_length=500, description="Reason for revocation")


# ============================================================================
# Helper Functions
# ============================================================================

# Valid scope prefixes and their required permissions
SCOPE_PERMISSIONS = {
    'read': 'read',
    'write': 'write',
    'delete': 'delete',
    'manage': 'manage_secrets',
}


def validate_scopes(scopes: List[str], user: User) -> None:
    """
    Validate that user can create key with requested scopes.

    Prevents privilege escalation by ensuring key scopes are subset of user permissions.
    """
    if user.role == 'owner':
        return  # Owners can create any scopes

    for scope in scopes:
        scope_prefix = scope.split(':')[0]
        required_permission = SCOPE_PERMISSIONS.get(scope_prefix)

        if required_permission and not user.has_permission(required_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Cannot create key with scope '{scope}' - insufficient permissions"
            )


def to_response(key: UserAPIKey) -> UserAPIKeyResponse:
    """Convert model to response (excludes sensitive data)."""
    return UserAPIKeyResponse(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        scopes=key.scopes,
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        request_count=key.request_count,
        revoked=key.revoked,
        revoked_at=key.revoked_at,
    )


# ============================================================================
# Routes
# ============================================================================

@router.post("", response_model=UserAPIKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    key_data: UserAPIKeyCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new API key for the current user.

    **IMPORTANT**: The raw API key is returned ONLY in this response.
    Save it immediately - it cannot be retrieved again.

    The key can be used in the X-API-Key header for authentication.
    """
    # Validate scopes
    validate_scopes(key_data.scopes, current_user)

    # Check for duplicate name
    existing = db.query(UserAPIKey).filter(
        UserAPIKey.user_id == current_user.id,
        UserAPIKey.name == key_data.name,
        UserAPIKey.revoked == False
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"You already have an active API key named '{key_data.name}'"
        )

    # Generate key
    raw_key = generate_api_key()
    key_prefix = extract_key_prefix(raw_key)
    key_hash = hash_api_key(raw_key)

    # Calculate expiration
    expires_at = None
    if key_data.expires_in_days:
        expires_at = calculate_expiration(key_data.expires_in_days)

    # Create record
    new_key = UserAPIKey(
        user_id=current_user.id,
        name=key_data.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=key_data.scopes,
        expires_at=expires_at,
        created_by_id=current_user.id,
        created_reason=key_data.reason,
    )

    db.add(new_key)
    db.commit()
    db.refresh(new_key)

    logger.info(
        "user_api_key_created",
        user_id=current_user.id,
        key_id=new_key.id,
        key_name=key_data.name,
        scopes=key_data.scopes,
        expires_in_days=key_data.expires_in_days,
    )

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='create',
        key=new_key,
        request=request,
        new_value={
            'name': new_key.name,
            'key_prefix': new_key.key_prefix,
            'scopes': new_key.scopes,
            'expires_at': new_key.expires_at.isoformat() if new_key.expires_at else None,
            'reason': key_data.reason,
        }
    )

    # Return with raw key (only time it's shown)
    return UserAPIKeyCreated(
        id=new_key.id,
        name=new_key.name,
        key_prefix=new_key.key_prefix,
        scopes=new_key.scopes,
        created_at=new_key.created_at,
        expires_at=new_key.expires_at,
        last_used_at=new_key.last_used_at,
        request_count=new_key.request_count,
        revoked=new_key.revoked,
        api_key=raw_key,  # ONLY shown here
    )


@router.get("", response_model=List[UserAPIKeyResponse])
async def list_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all API keys for the current user.

    Returns both active and revoked keys (for audit purposes).
    Raw key values are never included.
    """
    keys = db.query(UserAPIKey).filter(
        UserAPIKey.user_id == current_user.id
    ).order_by(UserAPIKey.created_at.desc()).all()

    return [to_response(key) for key in keys]


@router.get("/{key_id}", response_model=UserAPIKeyResponse)
async def get_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get details for a specific API key.

    Users can only view their own keys.
    Admins (owners) can view any user's keys.
    """
    key = db.query(UserAPIKey).filter(UserAPIKey.id == key_id).first()

    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Permission check
    if key.user_id != current_user.id and current_user.role != 'owner':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own API keys"
        )

    return to_response(key)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: int,
    request: Request,
    revoke_data: UserAPIKeyRevoke = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Revoke an API key.

    Revocation is permanent - the key cannot be reactivated.
    The key record is kept for audit purposes.
    """
    key = db.query(UserAPIKey).filter(UserAPIKey.id == key_id).first()

    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Permission check
    if key.user_id != current_user.id and current_user.role != 'owner':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only revoke your own API keys"
        )

    if key.revoked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API key is already revoked"
        )

    # Soft-delete
    key.revoked = True
    key.revoked_at = datetime.utcnow()
    if revoke_data:
        key.revoked_reason = revoke_data.reason

    db.commit()

    logger.info(
        "user_api_key_revoked",
        key_id=key_id,
        key_name=key.name,
        revoked_by=current_user.username,
        reason=revoke_data.reason if revoke_data else None,
    )

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='revoke',
        key=key,
        request=request,
        old_value={
            'name': key.name,
            'key_prefix': key.key_prefix,
            'scopes': key.scopes,
            'created_at': key.created_at.isoformat() if key.created_at else None,
        },
        new_value={
            'revoked': True,
            'revoked_at': key.revoked_at.isoformat() if key.revoked_at else None,
            'revoked_reason': revoke_data.reason if revoke_data else None,
        }
    )

    return None
