"""
Hallucination check API routes.

Provides CRUD operations for anti-hallucination validation rules.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, HallucinationCheck

logger = structlog.get_logger()

router = APIRouter(prefix="/api/hallucination-checks", tags=["hallucination-checks"])


class HallucinationCheckCreate(BaseModel):
    """Request model for creating a hallucination check."""
    name: str
    display_name: str
    description: str = None
    check_type: str  # 'required_elements', 'fact_checking', 'confidence_threshold', 'cross_validation'
    applies_to_categories: List[str] = []
    enabled: bool = True
    severity: str = 'warning'  # 'error', 'warning', 'info'
    configuration: dict
    error_message_template: str = None
    auto_fix_enabled: bool = False
    auto_fix_prompt_template: str = None
    require_cross_model_validation: bool = False
    confidence_threshold: float = 0.7
    priority: int = 100


class HallucinationCheckUpdate(BaseModel):
    """Request model for updating a hallucination check."""
    display_name: str = None
    description: str = None
    applies_to_categories: List[str] = None
    enabled: bool = None
    severity: str = None
    configuration: dict = None
    error_message_template: str = None
    auto_fix_enabled: bool = None
    auto_fix_prompt_template: str = None
    require_cross_model_validation: bool = None
    confidence_threshold: float = None
    priority: int = None


@router.get("")
async def list_hallucination_checks(
    enabled_only: bool = False,
    check_type: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all hallucination checks."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(HallucinationCheck)

    if enabled_only:
        query = query.filter(HallucinationCheck.enabled == True)
    if check_type:
        query = query.filter(HallucinationCheck.check_type == check_type)

    checks = query.order_by(HallucinationCheck.priority, HallucinationCheck.name).all()
    return {"hallucination_checks": [c.to_dict() for c in checks]}


@router.get("/{check_id}")
async def get_hallucination_check(
    check_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific hallucination check by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    check = db.query(HallucinationCheck).filter(HallucinationCheck.id == check_id).first()
    if not check:
        raise HTTPException(status_code=404, detail="Hallucination check not found")

    return check.to_dict()


@router.post("", status_code=201)
async def create_hallucination_check(
    check_data: HallucinationCheckCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new hallucination check."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if check with same name already exists
    existing = db.query(HallucinationCheck).filter(HallucinationCheck.name == check_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Hallucination check '{check_data.name}' already exists")

    # Create check
    check = HallucinationCheck(
        name=check_data.name,
        display_name=check_data.display_name,
        description=check_data.description,
        check_type=check_data.check_type,
        applies_to_categories=check_data.applies_to_categories,
        enabled=check_data.enabled,
        severity=check_data.severity,
        configuration=check_data.configuration,
        error_message_template=check_data.error_message_template,
        auto_fix_enabled=check_data.auto_fix_enabled,
        auto_fix_prompt_template=check_data.auto_fix_prompt_template,
        require_cross_model_validation=check_data.require_cross_model_validation,
        confidence_threshold=check_data.confidence_threshold,
        priority=check_data.priority,
        created_by=current_user.username
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    logger.info("hallucination_check_created", check_id=check.id, name=check.name, user=current_user.username)

    return check.to_dict()


@router.put("/{check_id}")
async def update_hallucination_check(
    check_id: int,
    check_data: HallucinationCheckUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing hallucination check."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    check = db.query(HallucinationCheck).filter(HallucinationCheck.id == check_id).first()
    if not check:
        raise HTTPException(status_code=404, detail="Hallucination check not found")

    # Update fields
    if check_data.display_name is not None:
        check.display_name = check_data.display_name
    if check_data.description is not None:
        check.description = check_data.description
    if check_data.applies_to_categories is not None:
        check.applies_to_categories = check_data.applies_to_categories
    if check_data.enabled is not None:
        check.enabled = check_data.enabled
    if check_data.severity is not None:
        check.severity = check_data.severity
    if check_data.configuration is not None:
        check.configuration = check_data.configuration
    if check_data.error_message_template is not None:
        check.error_message_template = check_data.error_message_template
    if check_data.auto_fix_enabled is not None:
        check.auto_fix_enabled = check_data.auto_fix_enabled
    if check_data.auto_fix_prompt_template is not None:
        check.auto_fix_prompt_template = check_data.auto_fix_prompt_template
    if check_data.require_cross_model_validation is not None:
        check.require_cross_model_validation = check_data.require_cross_model_validation
    if check_data.confidence_threshold is not None:
        check.confidence_threshold = check_data.confidence_threshold
    if check_data.priority is not None:
        check.priority = check_data.priority

    db.commit()
    db.refresh(check)

    logger.info("hallucination_check_updated", check_id=check.id, name=check.name, user=current_user.username)

    return check.to_dict()


@router.delete("/{check_id}", status_code=204)
async def delete_hallucination_check(
    check_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a hallucination check."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    check = db.query(HallucinationCheck).filter(HallucinationCheck.id == check_id).first()
    if not check:
        raise HTTPException(status_code=404, detail="Hallucination check not found")

    check_name = check.name

    db.delete(check)
    db.commit()

    logger.info("hallucination_check_deleted", check_id=check_id, name=check_name, user=current_user.username)

    return None
