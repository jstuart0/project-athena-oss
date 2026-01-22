"""
Base Knowledge management API routes.

Provides endpoints for managing context-aware knowledge entries for voice assistant.
Supports property information, user mode context, and temporal data.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, BaseKnowledge

logger = structlog.get_logger()

router = APIRouter(prefix="/api/base-knowledge", tags=["base-knowledge"])


class BaseKnowledgeCreate(BaseModel):
    """Schema for creating a new base knowledge entry."""
    category: str  # 'property', 'location', 'user', 'temporal', 'general'
    key: str
    value: str
    applies_to: str = 'both'  # 'guest', 'owner', 'both'
    priority: int = 0
    extra_metadata: Optional[dict] = None
    enabled: bool = True
    description: Optional[str] = None


class BaseKnowledgeUpdate(BaseModel):
    """Schema for updating an existing base knowledge entry."""
    value: Optional[str] = None
    applies_to: Optional[str] = None
    priority: Optional[int] = None
    extra_metadata: Optional[dict] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None


class BaseKnowledgeResponse(BaseModel):
    """Schema for base knowledge response."""
    id: int
    category: str
    key: str
    value: str
    applies_to: str
    priority: int
    extra_metadata: Optional[dict]
    enabled: bool
    description: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class BaseKnowledgeBulkCreate(BaseModel):
    """Schema for bulk creating knowledge entries."""
    entries: List[BaseKnowledgeCreate]


@router.get("", response_model=List[BaseKnowledgeResponse])
async def list_base_knowledge(
    category: Optional[str] = Query(None, description="Filter by category"),
    applies_to: Optional[str] = Query(None, description="Filter by applies_to (guest/owner/both)"),
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    db: Session = Depends(get_db)
):
    """
    List all base knowledge entries with optional filters.

    Supports filtering by category, applies_to, and enabled status.
    Returns entries sorted by priority (descending).

    NOTE: This endpoint is public (no authentication required) to allow
    internal service-to-service calls from orchestrator.
    """
    # No authentication required for base knowledge read access
    # (allows orchestrator to fetch context for LLM prompts)

    try:
        query = db.query(BaseKnowledge)

        # Apply filters
        if category:
            query = query.filter(BaseKnowledge.category == category)
        if applies_to:
            query = query.filter(BaseKnowledge.applies_to == applies_to)
        if enabled is not None:
            query = query.filter(BaseKnowledge.enabled == enabled)

        # Order by priority (highest first)
        query = query.order_by(BaseKnowledge.priority.desc(), BaseKnowledge.created_at)

        entries = query.all()

        logger.info("base_knowledge_listed",
                   count=len(entries),
                   category=category,
                   applies_to=applies_to,
                   enabled=enabled)

        return [entry.to_dict() for entry in entries]

    except Exception as e:
        logger.error("failed_to_list_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve base knowledge entries")


@router.get("/{knowledge_id}", response_model=BaseKnowledgeResponse)
async def get_base_knowledge(
    knowledge_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific base knowledge entry by ID.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        entry = db.query(BaseKnowledge).filter(BaseKnowledge.id == knowledge_id).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Base knowledge entry not found")

        logger.info("base_knowledge_retrieved",
                   user=current_user.username,
                   knowledge_id=knowledge_id,
                   category=entry.category,
                   key=entry.key)

        return entry.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_base_knowledge", error=str(e), knowledge_id=knowledge_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve base knowledge entry")


@router.post("", response_model=BaseKnowledgeResponse, status_code=201)
async def create_base_knowledge(
    entry: BaseKnowledgeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new base knowledge entry.

    Requires write permission.
    Category and key combination must be unique.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Check if entry with same category and key already exists
        existing = db.query(BaseKnowledge).filter(
            BaseKnowledge.category == entry.category,
            BaseKnowledge.key == entry.key
        ).first()

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Base knowledge entry with category '{entry.category}' and key '{entry.key}' already exists"
            )

        # Create new entry
        new_entry = BaseKnowledge(
            category=entry.category,
            key=entry.key,
            value=entry.value,
            applies_to=entry.applies_to,
            priority=entry.priority,
            extra_metadata=entry.extra_metadata,
            enabled=entry.enabled,
            description=entry.description
        )

        db.add(new_entry)
        db.commit()
        db.refresh(new_entry)

        logger.info("base_knowledge_created",
                   user=current_user.username,
                   knowledge_id=new_entry.id,
                   category=new_entry.category,
                   key=new_entry.key)

        return new_entry.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create base knowledge entry")


@router.put("/{knowledge_id}", response_model=BaseKnowledgeResponse)
async def update_base_knowledge(
    knowledge_id: int,
    update_data: BaseKnowledgeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update an existing base knowledge entry.

    Requires write permission.
    Only provided fields will be updated.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        entry = db.query(BaseKnowledge).filter(BaseKnowledge.id == knowledge_id).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Base knowledge entry not found")

        # Update fields if provided
        if update_data.value is not None:
            entry.value = update_data.value
        if update_data.applies_to is not None:
            entry.applies_to = update_data.applies_to
        if update_data.priority is not None:
            entry.priority = update_data.priority
        if update_data.extra_metadata is not None:
            entry.extra_metadata = update_data.extra_metadata
        if update_data.enabled is not None:
            entry.enabled = update_data.enabled
        if update_data.description is not None:
            entry.description = update_data.description

        db.commit()
        db.refresh(entry)

        logger.info("base_knowledge_updated",
                   user=current_user.username,
                   knowledge_id=knowledge_id,
                   category=entry.category,
                   key=entry.key)

        return entry.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_base_knowledge", error=str(e), knowledge_id=knowledge_id)
        raise HTTPException(status_code=500, detail="Failed to update base knowledge entry")


@router.delete("/{knowledge_id}", status_code=204)
async def delete_base_knowledge(
    knowledge_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete a base knowledge entry.

    Requires write permission.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        entry = db.query(BaseKnowledge).filter(BaseKnowledge.id == knowledge_id).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Base knowledge entry not found")

        logger.info("base_knowledge_deleted",
                   user=current_user.username,
                   knowledge_id=knowledge_id,
                   category=entry.category,
                   key=entry.key)

        db.delete(entry)
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_base_knowledge", error=str(e), knowledge_id=knowledge_id)
        raise HTTPException(status_code=500, detail="Failed to delete base knowledge entry")


@router.post("/bulk", response_model=dict, status_code=201)
async def bulk_create_base_knowledge(
    data: BaseKnowledgeBulkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Bulk create base knowledge entries.

    Requires write permission.
    Creates multiple entries in a single transaction.
    Skips entries that already exist (by category + key).
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        created_count = 0
        skipped_count = 0
        created_ids = []

        for entry_data in data.entries:
            # Check if entry already exists
            existing = db.query(BaseKnowledge).filter(
                BaseKnowledge.category == entry_data.category,
                BaseKnowledge.key == entry_data.key
            ).first()

            if existing:
                skipped_count += 1
                continue

            # Create new entry
            new_entry = BaseKnowledge(
                category=entry_data.category,
                key=entry_data.key,
                value=entry_data.value,
                applies_to=entry_data.applies_to,
                priority=entry_data.priority,
                extra_metadata=entry_data.extra_metadata,
                enabled=entry_data.enabled,
                description=entry_data.description
            )

            db.add(new_entry)
            db.flush()  # Get the ID without committing
            created_ids.append(new_entry.id)
            created_count += 1

        db.commit()

        logger.info("base_knowledge_bulk_created",
                   user=current_user.username,
                   created_count=created_count,
                   skipped_count=skipped_count)

        return {
            "created_count": created_count,
            "skipped_count": skipped_count,
            "created_ids": created_ids
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_bulk_create_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to bulk create base knowledge entries")
