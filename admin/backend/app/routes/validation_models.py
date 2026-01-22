"""
Cross-validation models API routes.

Provides configuration for multi-model validation to reduce hallucinations.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, CrossValidationModel

logger = structlog.get_logger()

router = APIRouter(prefix="/api/validation-models", tags=["validation-models"])


class ValidationModelCreate(BaseModel):
    """Request model for creating a validation model."""
    name: str
    model_id: str  # e.g., 'phi3:mini', 'llama3.1:8b-q4'
    model_type: str  # 'primary', 'validation', 'fallback'
    endpoint_url: str = None
    enabled: bool = True
    use_for_categories: List[str] = []
    temperature: float = 0.1
    max_tokens: int = 200
    timeout_seconds: int = 30
    weight: float = 1.0
    min_confidence_required: float = 0.5


class ValidationModelUpdate(BaseModel):
    """Request model for updating a validation model."""
    model_id: str = None
    model_type: str = None
    endpoint_url: str = None
    enabled: bool = None
    use_for_categories: List[str] = None
    temperature: float = None
    max_tokens: int = None
    timeout_seconds: int = None
    weight: float = None
    min_confidence_required: float = None


@router.get("")
async def list_validation_models(
    enabled_only: bool = False,
    model_type: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all cross-validation models."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(CrossValidationModel)

    if enabled_only:
        query = query.filter(CrossValidationModel.enabled == True)
    if model_type:
        query = query.filter(CrossValidationModel.model_type == model_type)

    models = query.order_by(CrossValidationModel.model_type, CrossValidationModel.name).all()
    return {"validation_models": [m.to_dict() for m in models]}


@router.get("/{model_id}")
async def get_validation_model(
    model_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific validation model by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    model = db.query(CrossValidationModel).filter(CrossValidationModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Validation model not found")

    return model.to_dict()


@router.post("", status_code=201)
async def create_validation_model(
    model_data: ValidationModelCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new cross-validation model."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if model with same name already exists
    existing = db.query(CrossValidationModel).filter(CrossValidationModel.name == model_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Validation model '{model_data.name}' already exists")

    # Create model
    model = CrossValidationModel(
        name=model_data.name,
        model_id=model_data.model_id,
        model_type=model_data.model_type,
        endpoint_url=model_data.endpoint_url,
        enabled=model_data.enabled,
        use_for_categories=model_data.use_for_categories,
        temperature=model_data.temperature,
        max_tokens=model_data.max_tokens,
        timeout_seconds=model_data.timeout_seconds,
        weight=model_data.weight,
        min_confidence_required=model_data.min_confidence_required
    )
    db.add(model)
    db.commit()
    db.refresh(model)

    logger.info("validation_model_created", model_id=model.id, name=model.name, user=current_user.username)

    return model.to_dict()


@router.put("/{model_id}")
async def update_validation_model(
    model_id: int,
    model_data: ValidationModelUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing validation model."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    model = db.query(CrossValidationModel).filter(CrossValidationModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Validation model not found")

    # Update fields
    if model_data.model_id is not None:
        model.model_id = model_data.model_id
    if model_data.model_type is not None:
        model.model_type = model_data.model_type
    if model_data.endpoint_url is not None:
        model.endpoint_url = model_data.endpoint_url
    if model_data.enabled is not None:
        model.enabled = model_data.enabled
    if model_data.use_for_categories is not None:
        model.use_for_categories = model_data.use_for_categories
    if model_data.temperature is not None:
        model.temperature = model_data.temperature
    if model_data.max_tokens is not None:
        model.max_tokens = model_data.max_tokens
    if model_data.timeout_seconds is not None:
        model.timeout_seconds = model_data.timeout_seconds
    if model_data.weight is not None:
        model.weight = model_data.weight
    if model_data.min_confidence_required is not None:
        model.min_confidence_required = model_data.min_confidence_required

    db.commit()
    db.refresh(model)

    logger.info("validation_model_updated", model_id=model.id, name=model.name, user=current_user.username)

    return model.to_dict()


@router.delete("/{model_id}", status_code=204)
async def delete_validation_model(
    model_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a validation model."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    model = db.query(CrossValidationModel).filter(CrossValidationModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Validation model not found")

    model_name = model.name

    db.delete(model)
    db.commit()

    logger.info("validation_model_deleted", model_id=model_id, name=model_name, user=current_user.username)

    return None
