"""
Room Groups management API routes.

Provides endpoints for managing logical room groupings and aliases.
Supports commands like "turn on the first floor lights" where "first floor"
maps to living room, dining room, and kitchen.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, RoomGroup, RoomGroupAlias, RoomGroupMember

logger = structlog.get_logger()

router = APIRouter(prefix="/api/room-groups", tags=["room-groups"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class RoomGroupMemberCreate(BaseModel):
    """Schema for adding a room to a group."""
    room_name: str
    display_name: Optional[str] = None
    ha_entity_pattern: Optional[str] = None


class RoomGroupMemberResponse(BaseModel):
    """Schema for room member response."""
    id: int
    room_group_id: int
    room_name: str
    display_name: Optional[str]
    ha_entity_pattern: Optional[str]
    created_at: Optional[str]

    class Config:
        from_attributes = True


class RoomGroupCreate(BaseModel):
    """Schema for creating a new room group."""
    name: str  # Canonical name: "first_floor"
    display_name: str  # User-friendly: "First Floor"
    description: Optional[str] = None
    enabled: bool = True
    aliases: Optional[List[str]] = []  # Initial aliases to add
    members: Optional[List[RoomGroupMemberCreate]] = []  # Initial members


class RoomGroupUpdate(BaseModel):
    """Schema for updating an existing room group."""
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


class RoomGroupResponse(BaseModel):
    """Schema for room group response."""
    id: int
    name: str
    display_name: str
    description: Optional[str]
    enabled: bool
    aliases: List[str]
    members: List[RoomGroupMemberResponse]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class AliasCreate(BaseModel):
    """Schema for adding an alias."""
    alias: str


class ResolveResponse(BaseModel):
    """Schema for resolve endpoint response."""
    found: bool
    room_group: Optional[RoomGroupResponse] = None
    matched_alias: Optional[str] = None
    room_names: List[str] = []


# ============================================================================
# Room Group CRUD Endpoints
# ============================================================================

@router.get("", response_model=List[RoomGroupResponse])
async def list_room_groups(
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    db: Session = Depends(get_db)
):
    """
    List all room groups with their aliases and members.

    NOTE: This endpoint is public (no authentication required) to allow
    internal service-to-service calls from orchestrator.
    """
    try:
        query = db.query(RoomGroup).options(
            joinedload(RoomGroup.aliases),
            joinedload(RoomGroup.members)
        )

        if enabled is not None:
            query = query.filter(RoomGroup.enabled == enabled)

        query = query.order_by(RoomGroup.display_name)
        groups = query.all()

        logger.info("room_groups_listed", count=len(groups), enabled=enabled)

        return [group.to_dict() for group in groups]

    except Exception as e:
        logger.error("failed_to_list_room_groups", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve room groups")


@router.get("/resolve/{query_term}", response_model=ResolveResponse)
async def resolve_room_group(
    query_term: str,
    db: Session = Depends(get_db)
):
    """
    Resolve a query term to a room group.

    Checks both room group names and aliases. Case-insensitive.
    Returns the room group with all its member rooms.

    This is the primary endpoint used by the orchestrator to expand
    commands like "first floor" into individual rooms.

    NOTE: This endpoint is public for orchestrator access.
    """
    try:
        query_lower = query_term.lower().strip()

        # First, try to find by group name (exact match, case-insensitive)
        group = db.query(RoomGroup).options(
            joinedload(RoomGroup.aliases),
            joinedload(RoomGroup.members)
        ).filter(
            RoomGroup.enabled == True,
            RoomGroup.name.ilike(query_lower.replace(' ', '_'))
        ).first()

        matched_alias = None

        # If not found by name, try aliases
        if not group:
            alias = db.query(RoomGroupAlias).join(RoomGroup).filter(
                RoomGroup.enabled == True,
                RoomGroupAlias.alias.ilike(query_lower)
            ).first()

            if alias:
                matched_alias = alias.alias
                group = db.query(RoomGroup).options(
                    joinedload(RoomGroup.aliases),
                    joinedload(RoomGroup.members)
                ).filter(RoomGroup.id == alias.room_group_id).first()

        if group:
            room_names = [m.room_name for m in group.members]
            logger.info("room_group_resolved",
                       query_term=query_term,
                       group_name=group.name,
                       matched_alias=matched_alias,
                       room_count=len(room_names))

            return {
                "found": True,
                "room_group": group.to_dict(),
                "matched_alias": matched_alias,
                "room_names": room_names
            }

        logger.debug("room_group_not_found", query_term=query_term)
        return {
            "found": False,
            "room_group": None,
            "matched_alias": None,
            "room_names": []
        }

    except Exception as e:
        logger.error("failed_to_resolve_room_group", error=str(e), query_term=query_term)
        raise HTTPException(status_code=500, detail="Failed to resolve room group")


@router.get("/available-rooms", response_model=List[str])
async def get_available_rooms(
    db: Session = Depends(get_db)
):
    """
    Get a list of known room names for autocomplete in the UI.

    Returns the commonly used room names from the orchestrator's known list
    plus any custom rooms already defined in groups.

    NOTE: This endpoint is public.
    """
    # Standard rooms from orchestrator
    standard_rooms = [
        "living_room", "dining_room", "kitchen", "bedroom", "master_bedroom",
        "guest_bedroom", "bathroom", "master_bathroom", "office", "study",
        "den", "family_room", "hallway", "hall", "foyer", "entryway",
        "basement", "attic", "garage", "laundry_room", "mudroom",
        "porch", "patio", "deck", "sunroom", "nursery", "playroom",
        "media_room", "theater", "gym", "workshop", "closet"
    ]

    try:
        # Get custom rooms from existing members
        custom_rooms = db.query(RoomGroupMember.room_name).distinct().all()
        custom_room_names = [r[0] for r in custom_rooms]

        # Combine and dedupe
        all_rooms = sorted(set(standard_rooms + custom_room_names))

        return all_rooms

    except Exception as e:
        logger.error("failed_to_get_available_rooms", error=str(e))
        return standard_rooms


@router.get("/{group_id}", response_model=RoomGroupResponse)
async def get_room_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific room group by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        group = db.query(RoomGroup).options(
            joinedload(RoomGroup.aliases),
            joinedload(RoomGroup.members)
        ).filter(RoomGroup.id == group_id).first()

        if not group:
            raise HTTPException(status_code=404, detail="Room group not found")

        logger.info("room_group_retrieved",
                   user=current_user.username,
                   group_id=group_id,
                   name=group.name)

        return group.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_room_group", error=str(e), group_id=group_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve room group")


@router.post("", response_model=RoomGroupResponse, status_code=201)
async def create_room_group(
    group_data: RoomGroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new room group.

    Optionally include initial aliases and members.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Check if group with same name exists
        canonical_name = group_data.name.lower().replace(' ', '_')
        existing = db.query(RoomGroup).filter(RoomGroup.name == canonical_name).first()

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Room group with name '{canonical_name}' already exists"
            )

        # Create the group
        new_group = RoomGroup(
            name=canonical_name,
            display_name=group_data.display_name,
            description=group_data.description,
            enabled=group_data.enabled
        )
        db.add(new_group)
        db.flush()  # Get the ID

        # Add aliases
        for alias in group_data.aliases or []:
            alias_lower = alias.lower().strip()
            # Check if alias already exists globally
            existing_alias = db.query(RoomGroupAlias).filter(
                RoomGroupAlias.alias.ilike(alias_lower)
            ).first()
            if existing_alias:
                raise HTTPException(
                    status_code=409,
                    detail=f"Alias '{alias}' is already in use by another group"
                )
            new_alias = RoomGroupAlias(room_group_id=new_group.id, alias=alias_lower)
            db.add(new_alias)

        # Add members
        for member in group_data.members or []:
            room_name = member.room_name.lower().replace(' ', '_')
            new_member = RoomGroupMember(
                room_group_id=new_group.id,
                room_name=room_name,
                display_name=member.display_name,
                ha_entity_pattern=member.ha_entity_pattern
            )
            db.add(new_member)

        db.commit()
        db.refresh(new_group)

        # Reload with relationships
        new_group = db.query(RoomGroup).options(
            joinedload(RoomGroup.aliases),
            joinedload(RoomGroup.members)
        ).filter(RoomGroup.id == new_group.id).first()

        logger.info("room_group_created",
                   user=current_user.username,
                   group_id=new_group.id,
                   name=new_group.name,
                   alias_count=len(group_data.aliases or []),
                   member_count=len(group_data.members or []))

        return new_group.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_room_group", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create room group")


@router.put("/{group_id}", response_model=RoomGroupResponse)
async def update_room_group(
    group_id: int,
    update_data: RoomGroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing room group."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        group = db.query(RoomGroup).filter(RoomGroup.id == group_id).first()

        if not group:
            raise HTTPException(status_code=404, detail="Room group not found")

        # Update fields
        if update_data.name is not None:
            canonical_name = update_data.name.lower().replace(' ', '_')
            # Check for duplicate name
            existing = db.query(RoomGroup).filter(
                RoomGroup.name == canonical_name,
                RoomGroup.id != group_id
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail=f"Name '{canonical_name}' already in use")
            group.name = canonical_name

        if update_data.display_name is not None:
            group.display_name = update_data.display_name
        if update_data.description is not None:
            group.description = update_data.description
        if update_data.enabled is not None:
            group.enabled = update_data.enabled

        db.commit()
        db.refresh(group)

        # Reload with relationships
        group = db.query(RoomGroup).options(
            joinedload(RoomGroup.aliases),
            joinedload(RoomGroup.members)
        ).filter(RoomGroup.id == group_id).first()

        logger.info("room_group_updated",
                   user=current_user.username,
                   group_id=group_id,
                   name=group.name)

        return group.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_room_group", error=str(e), group_id=group_id)
        raise HTTPException(status_code=500, detail="Failed to update room group")


@router.delete("/{group_id}", status_code=204)
async def delete_room_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a room group and all its aliases and members."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        group = db.query(RoomGroup).filter(RoomGroup.id == group_id).first()

        if not group:
            raise HTTPException(status_code=404, detail="Room group not found")

        logger.info("room_group_deleted",
                   user=current_user.username,
                   group_id=group_id,
                   name=group.name)

        db.delete(group)  # Cascades to aliases and members
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_room_group", error=str(e), group_id=group_id)
        raise HTTPException(status_code=500, detail="Failed to delete room group")


# ============================================================================
# Alias Management Endpoints
# ============================================================================

@router.post("/{group_id}/aliases", response_model=dict, status_code=201)
async def add_alias(
    group_id: int,
    alias_data: AliasCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add an alias to a room group."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        group = db.query(RoomGroup).filter(RoomGroup.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Room group not found")

        alias_lower = alias_data.alias.lower().strip()

        # Check if alias already exists
        existing = db.query(RoomGroupAlias).filter(
            RoomGroupAlias.alias.ilike(alias_lower)
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Alias '{alias_data.alias}' already in use")

        new_alias = RoomGroupAlias(room_group_id=group_id, alias=alias_lower)
        db.add(new_alias)
        db.commit()
        db.refresh(new_alias)

        logger.info("room_group_alias_added",
                   user=current_user.username,
                   group_id=group_id,
                   alias=alias_lower)

        return {"id": new_alias.id, "alias": new_alias.alias}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_add_alias", error=str(e), group_id=group_id)
        raise HTTPException(status_code=500, detail="Failed to add alias")


@router.delete("/{group_id}/aliases/{alias_id}", status_code=204)
async def remove_alias(
    group_id: int,
    alias_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove an alias from a room group."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        alias = db.query(RoomGroupAlias).filter(
            RoomGroupAlias.id == alias_id,
            RoomGroupAlias.room_group_id == group_id
        ).first()

        if not alias:
            raise HTTPException(status_code=404, detail="Alias not found")

        logger.info("room_group_alias_removed",
                   user=current_user.username,
                   group_id=group_id,
                   alias=alias.alias)

        db.delete(alias)
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_remove_alias", error=str(e), group_id=group_id, alias_id=alias_id)
        raise HTTPException(status_code=500, detail="Failed to remove alias")


# ============================================================================
# Member Management Endpoints
# ============================================================================

@router.post("/{group_id}/members", response_model=RoomGroupMemberResponse, status_code=201)
async def add_member(
    group_id: int,
    member_data: RoomGroupMemberCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add a room to a group."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        group = db.query(RoomGroup).filter(RoomGroup.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Room group not found")

        room_name = member_data.room_name.lower().replace(' ', '_')

        # Check if member already exists in this group
        existing = db.query(RoomGroupMember).filter(
            RoomGroupMember.room_group_id == group_id,
            RoomGroupMember.room_name == room_name
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Room '{room_name}' is already in this group"
            )

        new_member = RoomGroupMember(
            room_group_id=group_id,
            room_name=room_name,
            display_name=member_data.display_name,
            ha_entity_pattern=member_data.ha_entity_pattern
        )
        db.add(new_member)
        db.commit()
        db.refresh(new_member)

        logger.info("room_group_member_added",
                   user=current_user.username,
                   group_id=group_id,
                   room_name=room_name)

        return new_member.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_add_member", error=str(e), group_id=group_id)
        raise HTTPException(status_code=500, detail="Failed to add room member")


@router.delete("/{group_id}/members/{member_id}", status_code=204)
async def remove_member(
    group_id: int,
    member_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a room from a group."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        member = db.query(RoomGroupMember).filter(
            RoomGroupMember.id == member_id,
            RoomGroupMember.room_group_id == group_id
        ).first()

        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        logger.info("room_group_member_removed",
                   user=current_user.username,
                   group_id=group_id,
                   room_name=member.room_name)

        db.delete(member)
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_remove_member", error=str(e), group_id=group_id, member_id=member_id)
        raise HTTPException(status_code=500, detail="Failed to remove room member")
