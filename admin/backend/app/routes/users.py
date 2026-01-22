"""
User management API routes.

Provides CRUD operations for user accounts and RBAC.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/users", tags=["users"])


class UserUpdate(BaseModel):
    """Request model for updating a user."""
    role: str = None  # 'owner', 'operator', 'viewer', 'support'
    active: bool = None


class UserResponse(BaseModel):
    """Response model for user data."""
    id: int
    username: str
    email: str
    full_name: str = None
    role: str
    active: bool
    last_login: str = None
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
    logger.info("audit_log_created", action=action, resource_type='user',
                resource_id=target_user.id)


@router.get("", response_model=List[UserResponse])
async def list_users(
    active_only: bool = False,
    role: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all users."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(User)

    if active_only:
        query = query.filter(User.active == True)
    if role:
        query = query.filter(User.role == role)

    users = query.order_by(User.username).all()

    return [
        UserResponse(
            id=u.id,
            username=u.username,
            email=u.email,
            full_name=u.full_name,
            role=u.role,
            active=u.active,
            last_login=u.last_login.isoformat() if u.last_login else None,
            created_at=u.created_at.isoformat()
        )
        for u in users
    ]


@router.get("/roles")
async def list_roles(
    current_user: User = Depends(get_current_user)
):
    """List available user roles and their permissions."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    return {
        "roles": [
            {
                "name": "owner",
                "display_name": "Owner",
                "description": "Full system access including user management",
                "permissions": ["read", "write", "delete", "manage_users", "manage_secrets", "view_audit"]
            },
            {
                "name": "operator",
                "display_name": "Operator",
                "description": "Can view and modify system configuration",
                "permissions": ["read", "write", "view_audit"]
            },
            {
                "name": "viewer",
                "display_name": "Viewer",
                "description": "Read-only access to monitor system status",
                "permissions": ["read"]
            },
            {
                "name": "support",
                "display_name": "Support",
                "description": "Read access plus audit log viewing for support tasks",
                "permissions": ["read", "view_audit"]
            }
        ]
    }


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific user by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        active=user.active,
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_at=user.created_at.isoformat()
    )


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update a user's role or active status.

    Requires manage_users permission (owner role).
    """
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent users from modifying themselves
    if user.id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="Cannot modify your own account. Ask another owner to change your role."
        )

    # Store old values for audit
    old_value = {'role': user.role, 'active': user.active}

    # Update role if provided
    if user_data.role is not None:
        valid_roles = ['owner', 'operator', 'viewer', 'support']
        if user_data.role not in valid_roles:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}"
            )
        user.role = user_data.role

    # Update active status if provided
    if user_data.active is not None:
        user.active = user_data.active

    db.commit()
    db.refresh(user)

    # Audit log
    new_value = {'role': user.role, 'active': user.active}
    create_audit_log(
        db, current_user, 'update', user,
        old_value=old_value, new_value=new_value, request=request
    )

    logger.info("user_updated", user_id=user.id, username=user.username,
                old_role=old_value['role'], new_role=user.role,
                modified_by=current_user.username)

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        active=user.active,
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_at=user.created_at.isoformat()
    )


@router.delete("/{user_id}", status_code=204)
async def deactivate_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Deactivate a user (soft delete by setting active=False).

    Requires manage_users permission (owner role).
    """
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent users from deactivating themselves
    if user.id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="Cannot deactivate your own account"
        )

    # Soft delete
    old_value = {'active': user.active}
    user.active = False
    db.commit()

    # Audit log
    new_value = {'active': False}
    create_audit_log(
        db, current_user, 'deactivate', user,
        old_value=old_value, new_value=new_value, request=request
    )

    logger.warning("user_deactivated", user_id=user.id, username=user.username,
                   deactivated_by=current_user.username)

    return None


@router.post("/{user_id}/reactivate", response_model=UserResponse)
async def reactivate_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Reactivate a deactivated user.

    Requires manage_users permission (owner role).
    """
    if not current_user.has_permission('manage_users'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to manage users")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.active:
        raise HTTPException(status_code=400, detail="User is already active")

    # Reactivate
    old_value = {'active': False}
    user.active = True
    db.commit()
    db.refresh(user)

    # Audit log
    new_value = {'active': True}
    create_audit_log(
        db, current_user, 'reactivate', user,
        old_value=old_value, new_value=new_value, request=request
    )

    logger.info("user_reactivated", user_id=user.id, username=user.username,
                reactivated_by=current_user.username)

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        active=user.active,
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_at=user.created_at.isoformat()
    )


@router.get("/me/permissions")
async def get_my_permissions(
    current_user: User = Depends(get_current_user)
):
    """Get current user's permissions based on their role."""
    permissions = {
        'owner': ['read', 'write', 'delete', 'manage_users', 'manage_secrets', 'view_audit'],
        'operator': ['read', 'write', 'view_audit'],
        'viewer': ['read'],
        'support': ['read', 'view_audit'],
    }

    user_permissions = permissions.get(current_user.role, [])

    return {
        "user_id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
        "permissions": user_permissions,
        "can_read": "read" in user_permissions,
        "can_write": "write" in user_permissions,
        "can_delete": "delete" in user_permissions,
        "can_manage_users": "manage_users" in user_permissions,
        "can_manage_secrets": "manage_secrets" in user_permissions,
        "can_view_audit": "view_audit" in user_permissions,
    }
