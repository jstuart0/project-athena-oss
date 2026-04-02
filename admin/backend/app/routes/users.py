"""
User management API routes.

Provides CRUD operations for user accounts and RBAC.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, AuditLog
from app.utils.passwords import hash_password, validate_password_strength

logger = structlog.get_logger()

router = APIRouter(prefix="/api/users", tags=["users"])


class UserUpdate(BaseModel):
    """Request model for updating a user."""
    role: Optional[str] = None
    active: Optional[bool] = None
    auth_provider: Optional[str] = None
    password: Optional[str] = None


class UserCreate(BaseModel):
    """Request model for creating a user."""
    username: str
    email: EmailStr
    full_name: Optional[str] = None
    auth_provider: str = "local"
    role: str = "viewer"
    password: Optional[str] = None


class PasswordResetRequest(BaseModel):
    """Request model for resetting a local user's password."""
    password: str


class ChangeMyPasswordRequest(BaseModel):
    """Request model for a local user changing their own password."""
    current_password: str
    new_password: str


class UserResponse(BaseModel):
    """Response model for user data."""
    id: int
    username: str
    email: str
    full_name: Optional[str] = None
    auth_provider: str
    role: str
    active: bool
    last_login: Optional[str] = None
    created_at: str

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    target_user: User,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='user',
        resource_id=target_user.id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='user', resource_id=target_user.id)


def _user_to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        auth_provider=user.auth_provider,
        role=user.role,
        active=user.active,
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_at=user.created_at.isoformat()
    )


def _validate_role(role: str):
    valid_roles = ['owner', 'operator', 'viewer', 'support']
    if role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")


def _validate_auth_provider(auth_provider: str):
    valid_auth_providers = ['local', 'oidc']
    if auth_provider not in valid_auth_providers:
        raise HTTPException(status_code=400, detail=f"Invalid auth_provider. Must be one of: {', '.join(valid_auth_providers)}")


@router.get("", response_model=List[UserResponse])
async def list_users(
    active_only: bool = False,
    role: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all users."""
    if not current_user.has_permission('read:users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(User)

    if active_only:
        query = query.filter(User.active == True)
    if role:
        query = query.filter(User.role == role)

    users = query.order_by(User.username).all()

    return [_user_to_response(u) for u in users]


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    user_data: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a local or OIDC user account."""
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    _validate_role(user_data.role)
    _validate_auth_provider(user_data.auth_provider)

    username = user_data.username.strip()
    full_name = user_data.full_name.strip() if user_data.full_name else None

    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(status_code=409, detail="Email already exists")

    password_hash = None
    if user_data.auth_provider == 'local':
        ok, message = validate_password_strength(user_data.password or "")
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        password_hash = hash_password(user_data.password)

    new_user = User(
        authentik_id=None,
        username=username,
        email=user_data.email,
        full_name=full_name,
        auth_provider=user_data.auth_provider,
        password_hash=password_hash,
        role=user_data.role,
        active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    create_audit_log(
        db, current_user, 'create', new_user,
        old_value=None,
        new_value={'auth_provider': new_user.auth_provider, 'role': new_user.role, 'active': new_user.active},
        request=request
    )

    logger.info("user_created_admin", user_id=new_user.id, username=new_user.username, auth_provider=new_user.auth_provider, created_by=current_user.username)
    return _user_to_response(new_user)


@router.get("/roles")
async def list_roles(current_user: User = Depends(get_current_user)):
    """List available user roles and their permissions."""
    if not current_user.has_permission('read:users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    return {
        "roles": [
            {"name": "owner", "display_name": "Owner", "description": "Full system access including user management", "permissions": ["read", "write", "delete", "manage_users", "manage_secrets", "view_audit"]},
            {"name": "operator", "display_name": "Operator", "description": "Can view and modify system configuration", "permissions": ["read", "write", "view_audit"]},
            {"name": "viewer", "display_name": "Viewer", "description": "Read-only access to dashboard and alerts", "permissions": ["read:dashboard", "read:alerts"]},
            {"name": "support", "display_name": "Support", "description": "Operational visibility plus audit access for support tasks", "permissions": ["read:dashboard", "read:alerts", "read:analytics", "view_audit"]},
        ]
    }


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Get a specific user by ID."""
    if not current_user.has_permission('read:users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _user_to_response(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a user's role, active status, or auth provider."""
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot modify your own account. Ask another owner to change your role.")

    old_value = {'role': user.role, 'active': user.active}

    if user_data.role is not None:
        _validate_role(user_data.role)
        user.role = user_data.role

    if user_data.active is not None:
        user.active = user_data.active

    if user_data.auth_provider is not None and user_data.auth_provider != user.auth_provider:
        _validate_auth_provider(user_data.auth_provider)
        old_value['auth_provider'] = user.auth_provider

        if user_data.auth_provider == 'local':
            ok, message = validate_password_strength(user_data.password or "")
            if not ok:
                raise HTTPException(status_code=400, detail=message)
            user.password_hash = hash_password(user_data.password)
            user.authentik_id = None
        else:
            user.password_hash = None
            user.authentik_id = None

        user.auth_provider = user_data.auth_provider

    db.commit()
    db.refresh(user)

    new_value = {'role': user.role, 'active': user.active, 'auth_provider': user.auth_provider}
    create_audit_log(db, current_user, 'update', user, old_value=old_value, new_value=new_value, request=request)

    logger.info("user_updated", user_id=user.id, username=user.username, old_role=old_value['role'], new_role=user.role, modified_by=current_user.username)
    return _user_to_response(user)


@router.delete("/{user_id}", status_code=204)
async def deactivate_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Deactivate a user (soft delete by setting active=False)."""
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    old_value = {'active': user.active}
    user.active = False
    db.commit()

    create_audit_log(db, current_user, 'deactivate', user, old_value=old_value, new_value={'active': False}, request=request)
    logger.warning("user_deactivated", user_id=user.id, username=user.username, deactivated_by=current_user.username)
    return None


@router.post("/{user_id}/reactivate", response_model=UserResponse)
async def reactivate_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reactivate a deactivated user."""
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.active:
        raise HTTPException(status_code=400, detail="User is already active")

    user.active = True
    db.commit()
    db.refresh(user)

    create_audit_log(db, current_user, 'reactivate', user, old_value={'active': False}, new_value={'active': True}, request=request)
    logger.info("user_reactivated", user_id=user.id, username=user.username, reactivated_by=current_user.username)
    return _user_to_response(user)


@router.post("/me/password", response_model=UserResponse)
async def change_my_password(
    payload: ChangeMyPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Allow a local user to change their own password."""
    from app.utils.passwords import verify_password

    if current_user.auth_provider != 'local':
        raise HTTPException(status_code=400, detail="Password changes are only supported for local users")

    if not current_user.password_hash:
        raise HTTPException(status_code=400, detail="Local user does not have a password set")

    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    ok, message = validate_password_strength(payload.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    current_user.password_hash = hash_password(payload.new_password)
    db.commit()
    db.refresh(current_user)

    create_audit_log(db, current_user, 'change_password', current_user, old_value=None, new_value={'auth_provider': current_user.auth_provider}, request=request)
    logger.info("local_user_password_changed", user_id=current_user.id, username=current_user.username)
    return _user_to_response(current_user)


@router.post("/{user_id}/password", response_model=UserResponse)
async def reset_local_user_password(
    user_id: int,
    payload: PasswordResetRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reset a local user's password."""
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.auth_provider != 'local':
        raise HTTPException(status_code=400, detail="Password reset is only supported for local users")

    ok, message = validate_password_strength(payload.password)
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    user.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(user)

    create_audit_log(db, current_user, 'reset_password', user, old_value=None, new_value={'auth_provider': user.auth_provider}, request=request)
    logger.info("local_user_password_reset", user_id=user.id, username=user.username, reset_by=current_user.username)
    return _user_to_response(user)


@router.get("/me/permissions")
async def get_my_permissions(current_user: User = Depends(get_current_user)):
    """Get current user's permissions based on their role."""
    user_permissions = sorted(current_user.get_permissions())

    return {
        "user_id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
        "permissions": user_permissions,
        "allowed_pages": current_user.get_allowed_pages(),
        "can_read": any(permission == "read" or permission.startswith("read:") for permission in user_permissions),
        "can_write": any(permission == "write" or permission.startswith("write:") for permission in user_permissions),
        "can_delete": any(permission == "delete" or permission.startswith("delete:") for permission in user_permissions),
        "can_manage_users": "manage_users" in user_permissions,
        "can_manage_secrets": "manage_secrets" in user_permissions,
        "can_view_audit": "view_audit" in user_permissions,
    }
