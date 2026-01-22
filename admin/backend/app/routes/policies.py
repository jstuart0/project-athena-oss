"""
Policy management API routes.

Provides CRUD operations for orchestrator/RAG configuration policies.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Policy, PolicyVersion, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/policies", tags=["policies"])


class PolicyCreate(BaseModel):
    """Request model for creating a policy."""
    mode: str  # 'fast', 'medium', 'custom', 'rag'
    config: dict  # Full configuration as JSON
    description: str = None


class PolicyUpdate(BaseModel):
    """Request model for updating a policy."""
    mode: str = None
    config: dict = None
    description: str = None
    active: bool = None


class PolicyResponse(BaseModel):
    """Response model for policy data."""
    id: int
    mode: str
    config: dict
    version: int
    created_by: str
    created_at: str
    active: bool
    description: str = None

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    policy: Policy,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='policy',
        resource_id=policy.id,
        policy_id=policy.id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='policy', resource_id=policy.id)


@router.get("", response_model=List[PolicyResponse])
async def list_policies(
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all policies."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(Policy)
    if active_only:
        query = query.filter(Policy.active == True)

    policies = query.order_by(Policy.created_at.desc()).all()

    return [
        PolicyResponse(
            id=p.id,
            mode=p.mode,
            config=p.config,
            version=p.version,
            created_by=p.creator.username,
            created_at=p.created_at.isoformat(),
            active=p.active,
            description=p.description
        )
        for p in policies
    ]


@router.get("/{policy_id}", response_model=PolicyResponse)
async def get_policy(
    policy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific policy by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    policy = db.query(Policy).filter(Policy.id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    return PolicyResponse(
        id=policy.id,
        mode=policy.mode,
        config=policy.config,
        version=policy.version,
        created_by=policy.creator.username,
        created_at=policy.created_at.isoformat(),
        active=policy.active,
        description=policy.description
    )


@router.post("", response_model=PolicyResponse, status_code=201)
async def create_policy(
    policy_data: PolicyCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new policy."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate mode
    valid_modes = ['fast', 'medium', 'custom', 'rag']
    if policy_data.mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode. Must be one of: {', '.join(valid_modes)}"
        )

    # Create policy
    policy = Policy(
        mode=policy_data.mode,
        config=policy_data.config,
        version=1,
        created_by_id=current_user.id,
        active=True,
        description=policy_data.description
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)

    # Create initial version
    version = PolicyVersion(
        policy_id=policy.id,
        version=1,
        config=policy_data.config,
        created_by_id=current_user.id,
        change_description="Initial policy creation"
    )
    db.add(version)
    db.commit()

    # Audit log
    create_audit_log(
        db, current_user, 'create', policy,
        new_value={'mode': policy.mode, 'config': policy.config},
        request=request
    )

    logger.info("policy_created", policy_id=policy.id, mode=policy.mode, user=current_user.username)

    return PolicyResponse(
        id=policy.id,
        mode=policy.mode,
        config=policy.config,
        version=policy.version,
        created_by=current_user.username,
        created_at=policy.created_at.isoformat(),
        active=policy.active,
        description=policy.description
    )


@router.put("/{policy_id}", response_model=PolicyResponse)
async def update_policy(
    policy_id: int,
    policy_data: PolicyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing policy."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    policy = db.query(Policy).filter(Policy.id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    # Store old values for audit
    old_value = {'mode': policy.mode, 'config': policy.config, 'active': policy.active}

    # Track what changed
    changes = []
    if policy_data.mode is not None and policy_data.mode != policy.mode:
        policy.mode = policy_data.mode
        changes.append('mode')

    if policy_data.config is not None and policy_data.config != policy.config:
        policy.config = policy_data.config
        policy.version += 1
        changes.append('config')

        # Create new version
        version = PolicyVersion(
            policy_id=policy.id,
            version=policy.version,
            config=policy.config,
            created_by_id=current_user.id,
            change_description=f"Updated: {', '.join(changes)}"
        )
        db.add(version)

    if policy_data.description is not None:
        policy.description = policy_data.description

    if policy_data.active is not None and policy_data.active != policy.active:
        policy.active = policy_data.active
        changes.append('active')

    db.commit()
    db.refresh(policy)

    # Audit log
    new_value = {'mode': policy.mode, 'config': policy.config, 'active': policy.active}
    create_audit_log(db, current_user, 'update', policy, old_value=old_value, new_value=new_value, request=request)

    logger.info("policy_updated", policy_id=policy.id, changes=changes, user=current_user.username)

    return PolicyResponse(
        id=policy.id,
        mode=policy.mode,
        config=policy.config,
        version=policy.version,
        created_by=policy.creator.username,
        created_at=policy.created_at.isoformat(),
        active=policy.active,
        description=policy.description
    )


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a policy (soft delete by setting active=False)."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    policy = db.query(Policy).filter(Policy.id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    # Soft delete
    old_value = {'mode': policy.mode, 'config': policy.config, 'active': policy.active}
    policy.active = False
    db.commit()

    # Audit log
    create_audit_log(db, current_user, 'delete', policy, old_value=old_value, request=request)

    logger.info("policy_deleted", policy_id=policy.id, user=current_user.username)
    return None


@router.get("/{policy_id}/versions")
async def get_policy_versions(
    policy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get version history for a policy."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    policy = db.query(Policy).filter(Policy.id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    versions = db.query(PolicyVersion)\
        .filter(PolicyVersion.policy_id == policy_id)\
        .order_by(PolicyVersion.version.desc())\
        .all()

    return {
        "policy_id": policy_id,
        "current_version": policy.version,
        "versions": [
            {
                "version": v.version,
                "config": v.config,
                "created_by": v.creator.username,
                "created_at": v.created_at.isoformat(),
                "change_description": v.change_description
            }
            for v in versions
        ]
    }


@router.post("/{policy_id}/rollback/{version}", response_model=PolicyResponse)
async def rollback_policy(
    policy_id: int,
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Rollback policy to a previous version."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    policy = db.query(Policy).filter(Policy.id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    # Find the target version
    target_version = db.query(PolicyVersion)\
        .filter(PolicyVersion.policy_id == policy_id, PolicyVersion.version == version)\
        .first()

    if not target_version:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")

    # Store old value
    old_value = {'config': policy.config, 'version': policy.version}

    # Rollback
    policy.config = target_version.config
    policy.version += 1  # Create new version number

    # Create version entry for rollback
    new_version = PolicyVersion(
        policy_id=policy.id,
        version=policy.version,
        config=policy.config,
        created_by_id=current_user.id,
        change_description=f"Rolled back to version {version}"
    )
    db.add(new_version)
    db.commit()
    db.refresh(policy)

    # Audit log
    new_value = {'config': policy.config, 'version': policy.version}
    create_audit_log(db, current_user, 'rollback', policy, old_value=old_value, new_value=new_value, request=request)

    logger.info("policy_rolled_back", policy_id=policy.id, from_version=old_value['version'],
                to_version=version, new_version=policy.version, user=current_user.username)

    return PolicyResponse(
        id=policy.id,
        mode=policy.mode,
        config=policy.config,
        version=policy.version,
        created_by=policy.creator.username,
        created_at=policy.created_at.isoformat(),
        active=policy.active,
        description=policy.description
    )
