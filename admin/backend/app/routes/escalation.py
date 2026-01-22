"""
Escalation Presets and Rules Management Routes.

Provides API endpoints for managing model escalation behavior:
- Presets: Named configurations with sets of rules
- Rules: Individual trigger conditions for escalation
- State: Current escalation state per session
- Overrides: Manual escalation overrides for testing
"""
import os
import json
import structlog
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_
from pydantic import BaseModel, Field

from ..database import get_db
from ..models import (
    EscalationPreset,
    EscalationRule,
    EscalationState,
    EscalationEvent,
    User
)
from ..auth.oidc import get_current_user, get_optional_user

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/escalation", tags=["escalation"])

# Redis configuration for instant cache invalidation
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
# Handle Kubernetes service discovery which may set REDIS_PORT to a URL like tcp://10.111.37.244:6379
_redis_port_env = os.getenv("REDIS_PORT", "6379")
if _redis_port_env.startswith("tcp://"):
    # Extract port from URL format
    REDIS_PORT = int(_redis_port_env.split(":")[-1])
else:
    REDIS_PORT = int(_redis_port_env)
REDIS_PRESET_VERSION_KEY = "escalation:preset_version"

# Try to import redis, but make it optional
try:
    import redis.asyncio as aioredis
    redis_available = True
except ImportError:
    redis_available = False
    logger.warning("redis not available - cache invalidation disabled")


# ============== Request/Response Models ==============

class PresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    auto_activate_conditions: Optional[Dict[str, Any]] = None


class PresetUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    auto_activate_conditions: Optional[Dict[str, Any]] = None


class RuleCreate(BaseModel):
    rule_name: str = Field(..., min_length=1, max_length=100)
    trigger_type: str = Field(..., min_length=1, max_length=50)
    trigger_patterns: Dict[str, Any]
    escalation_target: str = Field(..., pattern="^(complex|super_complex)$")
    escalation_duration: int = Field(5, ge=1, le=999)
    priority: int = Field(100, ge=1, le=1000)
    description: Optional[str] = None


class RuleUpdate(BaseModel):
    rule_name: Optional[str] = Field(None, min_length=1, max_length=100)
    trigger_type: Optional[str] = Field(None, min_length=1, max_length=50)
    trigger_patterns: Optional[Dict[str, Any]] = None
    escalation_target: Optional[str] = Field(None, pattern="^(complex|super_complex)$")
    escalation_duration: Optional[int] = Field(None, ge=1, le=999)
    priority: Optional[int] = Field(None, ge=1, le=1000)
    description: Optional[str] = None
    enabled: Optional[bool] = None


class OverrideCreate(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=255)
    target_model: str = Field(..., pattern="^(simple|complex|super_complex)$")
    duration_type: str = Field("turns", pattern="^(turns|time|indefinite)$")
    duration_turns: Optional[int] = Field(None, ge=1, le=999)
    duration_minutes: Optional[int] = Field(None, ge=1, le=1440)
    reason: Optional[str] = None


class InternalStateUpdate(BaseModel):
    session_id: str
    escalated_to: str
    turns_remaining: int
    triggered_by_rule_id: Optional[int] = None


class EscalationEventCreate(BaseModel):
    """Create an escalation event (internal endpoint for orchestrator)."""
    session_id: str
    event_type: str = Field("escalation", pattern="^(escalation|de-escalation)$")
    from_model: Optional[str] = None
    to_model: str
    rule_id: Optional[int] = None
    rule_name: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_context: Optional[Dict[str, Any]] = None


# ============== Cache Invalidation Helpers ==============

async def _get_redis_client():
    """Get async redis client."""
    if not redis_available:
        return None
    try:
        return aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    except Exception as e:
        logger.warning("redis_connection_failed", error=str(e))
        return None


async def _bust_cache_if_active(db: Session, preset_id: int):
    """Increment version counter if this preset is currently active."""
    preset = db.query(EscalationPreset).get(preset_id)
    if preset and preset.is_active:
        await _increment_preset_version()
        logger.info("cache_busted_active_preset", preset_id=preset_id, preset_name=preset.name)


async def _increment_preset_version():
    """Increment the preset version in Redis for instant cache invalidation."""
    client = await _get_redis_client()
    if client:
        try:
            await client.incr(REDIS_PRESET_VERSION_KEY)
            await client.close()
        except Exception as e:
            logger.warning("cache_version_increment_failed", error=str(e))


# ============== Public Endpoints (No Auth Required) ==============

@router.get("/presets/public")
async def list_presets_public(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """
    List all escalation presets (public endpoint for orchestrator).
    Returns presets without rules for quick listing.
    """
    presets = db.query(EscalationPreset).order_by(EscalationPreset.id).all()
    return [p.to_dict() for p in presets]


@router.get("/presets/active/public")
async def get_active_preset_public(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get active preset with all rules (public endpoint for orchestrator).
    This is the main endpoint used by the orchestrator for rule evaluation.
    """
    preset = db.query(EscalationPreset).options(
        joinedload(EscalationPreset.rules)
    ).filter(EscalationPreset.is_active == True).first()

    if not preset:
        raise HTTPException(status_code=404, detail="No active preset found")

    result = preset.to_dict()
    result['rules'] = sorted(
        [r.to_dict() for r in preset.rules if r.enabled],
        key=lambda x: x['priority'],
        reverse=True
    )
    return result


@router.get("/state/{session_id}/public")
async def get_escalation_state_public(
    session_id: str,
    db: Session = Depends(get_db)
) -> Optional[Dict[str, Any]]:
    """Get current escalation state for a session (public endpoint)."""
    state = db.query(EscalationState).filter(
        EscalationState.session_id == session_id
    ).first()

    if not state:
        return None

    # Check if expired
    if state.is_expired():
        db.delete(state)
        db.commit()
        return None

    return state.to_dict()


# ============== Preset Management (Auth Required) ==============

@router.get("/presets")
async def list_presets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all escalation presets with rule counts."""
    presets = db.query(EscalationPreset).options(
        joinedload(EscalationPreset.rules)
    ).order_by(EscalationPreset.id).all()

    result = []
    for p in presets:
        preset_dict = p.to_dict()
        preset_dict['rules'] = sorted(
            [r.to_dict() for r in p.rules],
            key=lambda x: x['priority'],
            reverse=True
        )
        result.append(preset_dict)
    return result


@router.get("/presets/{preset_id}")
async def get_preset(
    preset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get a specific preset with all rules."""
    preset = db.query(EscalationPreset).options(
        joinedload(EscalationPreset.rules)
    ).filter(EscalationPreset.id == preset_id).first()

    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    result = preset.to_dict()
    result['rules'] = sorted(
        [r.to_dict() for r in preset.rules],
        key=lambda x: x['priority'],
        reverse=True
    )
    return result


@router.post("/presets")
async def create_preset(
    data: PresetCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Create a new escalation preset."""
    # Check for duplicate name
    existing = db.query(EscalationPreset).filter(
        EscalationPreset.name == data.name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Preset with this name already exists")

    preset = EscalationPreset(
        name=data.name,
        description=data.description,
        is_active=False,
        auto_activate_conditions=data.auto_activate_conditions
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)

    logger.info("preset_created", preset_id=preset.id, name=preset.name, user=current_user.email)
    return preset.to_dict()


@router.put("/presets/{preset_id}")
async def update_preset(
    preset_id: int,
    data: PresetUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Update a preset's metadata."""
    preset = db.query(EscalationPreset).get(preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Check for duplicate name
    if data.name and data.name != preset.name:
        existing = db.query(EscalationPreset).filter(
            EscalationPreset.name == data.name,
            EscalationPreset.id != preset_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Preset with this name already exists")
        preset.name = data.name

    if data.description is not None:
        preset.description = data.description
    if data.auto_activate_conditions is not None:
        preset.auto_activate_conditions = data.auto_activate_conditions

    db.commit()
    db.refresh(preset)

    # Bust cache if this was the active preset
    await _bust_cache_if_active(db, preset_id)

    logger.info("preset_updated", preset_id=preset_id, user=current_user.email)
    return preset.to_dict()


@router.put("/presets/{preset_id}/activate")
async def activate_preset(
    preset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Activate a preset (deactivates all others)."""
    preset = db.query(EscalationPreset).get(preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Deactivate all presets
    db.query(EscalationPreset).update({EscalationPreset.is_active: False})

    # Activate this one
    preset.is_active = True
    db.commit()
    db.refresh(preset)

    # Always bust cache on activation
    await _increment_preset_version()

    # Log event
    event = EscalationEvent(
        session_id="admin",
        event_type="preset_activated",
        from_model=None,
        to_model="preset_change",
        preset_id=preset_id,
        preset_name=preset.name,
        triggered_by_user=current_user.email
    )
    db.add(event)
    db.commit()

    logger.info("preset_activated", preset_id=preset_id, name=preset.name, user=current_user.email)
    return preset.to_dict()


@router.post("/presets/{preset_id}/clone")
async def clone_preset(
    preset_id: int,
    new_name: str = Query(..., min_length=1, max_length=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Clone a preset with all its rules."""
    preset = db.query(EscalationPreset).options(
        joinedload(EscalationPreset.rules)
    ).filter(EscalationPreset.id == preset_id).first()

    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Check for duplicate name
    existing = db.query(EscalationPreset).filter(
        EscalationPreset.name == new_name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Preset with this name already exists")

    # Create new preset
    new_preset = EscalationPreset(
        name=new_name,
        description=f"Clone of {preset.name}",
        is_active=False,
        auto_activate_conditions=preset.auto_activate_conditions
    )
    db.add(new_preset)
    db.flush()

    # Clone all rules
    for rule in preset.rules:
        new_rule = EscalationRule(
            preset_id=new_preset.id,
            rule_name=rule.rule_name,
            trigger_type=rule.trigger_type,
            trigger_patterns=rule.trigger_patterns,
            escalation_target=rule.escalation_target,
            escalation_duration=rule.escalation_duration,
            priority=rule.priority,
            enabled=rule.enabled,
            description=rule.description
        )
        db.add(new_rule)

    db.commit()
    db.refresh(new_preset)

    logger.info("preset_cloned", original_id=preset_id, new_id=new_preset.id, new_name=new_name, user=current_user.email)
    return new_preset.to_dict()


@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    """Delete a preset (cannot delete active preset)."""
    preset = db.query(EscalationPreset).get(preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    if preset.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete active preset. Activate another preset first.")

    preset_name = preset.name
    db.delete(preset)
    db.commit()

    logger.info("preset_deleted", preset_id=preset_id, name=preset_name, user=current_user.email)
    return {"status": "deleted", "name": preset_name}


# ============== Rule Management (Auth Required) ==============

@router.get("/presets/{preset_id}/rules")
async def list_rules(
    preset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all rules for a preset."""
    preset = db.query(EscalationPreset).get(preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    rules = db.query(EscalationRule).filter(
        EscalationRule.preset_id == preset_id
    ).order_by(EscalationRule.priority.desc()).all()

    return [r.to_dict() for r in rules]


@router.post("/presets/{preset_id}/rules")
async def create_rule(
    preset_id: int,
    data: RuleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Create a new rule for a preset."""
    preset = db.query(EscalationPreset).get(preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    rule = EscalationRule(
        preset_id=preset_id,
        rule_name=data.rule_name,
        trigger_type=data.trigger_type,
        trigger_patterns=data.trigger_patterns,
        escalation_target=data.escalation_target,
        escalation_duration=data.escalation_duration,
        priority=data.priority,
        description=data.description,
        enabled=True
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    # Bust cache if this preset is active
    await _bust_cache_if_active(db, preset_id)

    logger.info("rule_created", rule_id=rule.id, preset_id=preset_id, rule_name=rule.rule_name, user=current_user.email)
    return rule.to_dict()


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get a specific rule."""
    rule = db.query(EscalationRule).get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule.to_dict()


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    data: RuleUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Update a rule."""
    rule = db.query(EscalationRule).get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    if data.rule_name is not None:
        rule.rule_name = data.rule_name
    if data.trigger_type is not None:
        rule.trigger_type = data.trigger_type
    if data.trigger_patterns is not None:
        rule.trigger_patterns = data.trigger_patterns
    if data.escalation_target is not None:
        rule.escalation_target = data.escalation_target
    if data.escalation_duration is not None:
        rule.escalation_duration = data.escalation_duration
    if data.priority is not None:
        rule.priority = data.priority
    if data.description is not None:
        rule.description = data.description
    if data.enabled is not None:
        rule.enabled = data.enabled

    db.commit()
    db.refresh(rule)

    # Bust cache if parent preset is active
    await _bust_cache_if_active(db, rule.preset_id)

    logger.info("rule_updated", rule_id=rule_id, user=current_user.email)
    return rule.to_dict()


@router.put("/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Toggle a rule's enabled status."""
    rule = db.query(EscalationRule).get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.enabled = not rule.enabled
    db.commit()
    db.refresh(rule)

    # Bust cache if parent preset is active
    await _bust_cache_if_active(db, rule.preset_id)

    logger.info("rule_toggled", rule_id=rule_id, enabled=rule.enabled, user=current_user.email)
    return rule.to_dict()


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    """Delete a rule."""
    rule = db.query(EscalationRule).get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    preset_id = rule.preset_id
    rule_name = rule.rule_name
    db.delete(rule)
    db.commit()

    # Bust cache if parent preset is active
    await _bust_cache_if_active(db, preset_id)

    logger.info("rule_deleted", rule_id=rule_id, rule_name=rule_name, user=current_user.email)
    return {"status": "deleted", "rule_name": rule_name}


# ============== Manual Override Management ==============

@router.get("/overrides/active")
async def list_active_overrides(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all active manual overrides."""
    overrides = db.query(EscalationState).filter(
        EscalationState.is_manual_override == True
    ).all()

    # Filter out expired ones
    result = []
    for o in overrides:
        if not o.is_expired():
            result.append(o.to_dict())
        else:
            # Clean up expired override
            db.delete(o)
    db.commit()

    return result


@router.post("/override")
async def create_override(
    data: OverrideCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Create a manual override for a session."""
    # Check if override already exists for this session
    existing = db.query(EscalationState).filter(
        EscalationState.session_id == data.session_id
    ).first()

    if existing:
        db.delete(existing)

    # Calculate expiration
    turns_remaining = None
    expires_at = None

    if data.duration_type == "turns":
        turns_remaining = data.duration_turns or 10
    elif data.duration_type == "time":
        minutes = data.duration_minutes or 30
        expires_at = datetime.utcnow() + timedelta(minutes=minutes)
    # indefinite: both remain None

    state = EscalationState(
        session_id=data.session_id,
        escalated_to=data.target_model,
        turns_remaining=turns_remaining,
        expires_at=expires_at,
        is_manual_override=True,
        override_reason=data.reason
    )
    db.add(state)

    # Log event
    event = EscalationEvent(
        session_id=data.session_id,
        event_type="manual_override",
        from_model=None,
        to_model=data.target_model,
        triggered_by_user=current_user.email,
        reason=data.reason
    )
    db.add(event)

    db.commit()
    db.refresh(state)

    logger.info("override_created", session_id=data.session_id, target=data.target_model, user=current_user.email)
    return state.to_dict()


@router.get("/override/{session_id}")
async def get_override(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get override for a specific session."""
    state = db.query(EscalationState).filter(
        EscalationState.session_id == session_id
    ).first()

    if not state:
        raise HTTPException(status_code=404, detail="No override found for this session")

    if state.is_expired():
        db.delete(state)
        db.commit()
        raise HTTPException(status_code=404, detail="Override has expired")

    return state.to_dict()


@router.delete("/override/{session_id}")
async def cancel_override(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    """Cancel a manual override."""
    state = db.query(EscalationState).filter(
        EscalationState.session_id == session_id
    ).first()

    if not state:
        raise HTTPException(status_code=404, detail="No override found for this session")

    db.delete(state)

    # Log event
    event = EscalationEvent(
        session_id=session_id,
        event_type="override_cancelled",
        from_model=state.escalated_to,
        to_model="normal",
        triggered_by_user=current_user.email
    )
    db.add(event)

    db.commit()

    logger.info("override_cancelled", session_id=session_id, user=current_user.email)
    return {"status": "cancelled", "session_id": session_id}


# ============== Internal Endpoints (for Orchestrator) ==============

@router.post("/state/internal")
async def update_state_internal(
    data: InternalStateUpdate,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Update escalation state (internal endpoint for orchestrator)."""
    # Check if state exists
    state = db.query(EscalationState).filter(
        EscalationState.session_id == data.session_id
    ).first()

    if state:
        state.escalated_to = data.escalated_to
        state.turns_remaining = data.turns_remaining
        state.triggered_by_rule_id = data.triggered_by_rule_id
        state.escalated_at = datetime.utcnow()
    else:
        state = EscalationState(
            session_id=data.session_id,
            escalated_to=data.escalated_to,
            turns_remaining=data.turns_remaining,
            triggered_by_rule_id=data.triggered_by_rule_id,
            is_manual_override=False
        )
        db.add(state)

    db.commit()
    db.refresh(state)
    return state.to_dict()


@router.put("/state/{session_id}/decrement")
async def decrement_turns(
    session_id: str,
    db: Session = Depends(get_db)
) -> Optional[Dict[str, Any]]:
    """Decrement turns remaining for a session (called by orchestrator)."""
    state = db.query(EscalationState).filter(
        EscalationState.session_id == session_id
    ).first()

    if not state:
        return None

    if state.turns_remaining is not None:
        state.turns_remaining -= 1

        if state.turns_remaining <= 0:
            # Log de-escalation event
            event = EscalationEvent(
                session_id=session_id,
                event_type="de-escalation",
                from_model=state.escalated_to,
                to_model="normal"
            )
            db.add(event)
            db.delete(state)
            db.commit()
            return None

    db.commit()
    db.refresh(state)
    return state.to_dict()


@router.post("/events/internal")
async def create_event_internal(
    data: EscalationEventCreate,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Log an escalation event (internal endpoint for orchestrator).

    This endpoint is used by the orchestrator to record escalation events
    so they appear in the escalation metrics and analytics dashboard.
    """
    # Look up rule if rule_name provided but not rule_id
    rule_id = data.rule_id
    if not rule_id and data.rule_name:
        rule = db.query(EscalationRule).filter(
            EscalationRule.rule_name == data.rule_name
        ).first()
        if rule:
            rule_id = rule.id

    # Get active preset info
    active_preset = db.query(EscalationPreset).filter(
        EscalationPreset.is_active == True
    ).first()

    event = EscalationEvent(
        session_id=data.session_id,
        event_type=data.event_type,
        from_model=data.from_model,
        to_model=data.to_model,
        triggered_by_rule_id=rule_id,
        preset_id=active_preset.id if active_preset else None,
        preset_name=active_preset.name if active_preset else None,
        trigger_context=data.trigger_context
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    logger.info(
        "escalation_event_logged",
        session_id=data.session_id[:8] if data.session_id else "unknown",
        event_type=data.event_type,
        to_model=data.to_model,
        rule_name=data.rule_name
    )

    return event.to_dict()


# ============== Events / Analytics ==============

@router.get("/events")
async def list_events(
    session_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List escalation events with optional filtering."""
    query = db.query(EscalationEvent)

    if session_id:
        query = query.filter(EscalationEvent.session_id == session_id)
    if event_type:
        query = query.filter(EscalationEvent.event_type == event_type)

    events = query.order_by(
        EscalationEvent.created_at.desc()
    ).offset(offset).limit(limit).all()

    return [e.to_dict() for e in events]


@router.get("/events/stats")
async def get_event_stats(
    hours: int = Query(24, ge=1, le=168),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get escalation event statistics."""
    from sqlalchemy import func

    since = datetime.utcnow() - timedelta(hours=hours)

    # Count by event type
    type_counts = dict(
        db.query(
            EscalationEvent.event_type,
            func.count(EscalationEvent.id)
        ).filter(
            EscalationEvent.created_at >= since
        ).group_by(EscalationEvent.event_type).all()
    )

    # Count by target model
    model_counts = dict(
        db.query(
            EscalationEvent.to_model,
            func.count(EscalationEvent.id)
        ).filter(
            EscalationEvent.created_at >= since,
            EscalationEvent.event_type == "escalation"
        ).group_by(EscalationEvent.to_model).all()
    )

    # Count by trigger type (join with rules to get trigger_type)
    trigger_counts = dict(
        db.query(
            EscalationRule.trigger_type,
            func.count(EscalationEvent.id)
        ).join(
            EscalationRule, EscalationEvent.triggered_by_rule_id == EscalationRule.id
        ).filter(
            EscalationEvent.created_at >= since,
            EscalationEvent.event_type == "escalation"
        ).group_by(EscalationRule.trigger_type).all()
    )

    # Count by rule name (top 10 most triggered rules)
    rule_counts = db.query(
        EscalationRule.rule_name,
        EscalationRule.trigger_type,
        func.count(EscalationEvent.id).label('count')
    ).join(
        EscalationRule, EscalationEvent.triggered_by_rule_id == EscalationRule.id
    ).filter(
        EscalationEvent.created_at >= since,
        EscalationEvent.event_type == "escalation"
    ).group_by(
        EscalationRule.rule_name, EscalationRule.trigger_type
    ).order_by(
        func.count(EscalationEvent.id).desc()
    ).limit(10).all()

    top_rules = [
        {"rule_name": r[0], "trigger_type": r[1], "count": r[2]}
        for r in rule_counts
    ]

    # Total escalations
    total_escalations = type_counts.get("escalation", 0)

    # Unique sessions that escalated
    unique_sessions = db.query(
        func.count(func.distinct(EscalationEvent.session_id))
    ).filter(
        EscalationEvent.created_at >= since,
        EscalationEvent.event_type == "escalation"
    ).scalar() or 0

    return {
        "period_hours": hours,
        "total_escalations": total_escalations,
        "unique_sessions": unique_sessions,
        "by_event_type": type_counts,
        "by_target_model": model_counts,
        "by_trigger_type": trigger_counts,
        "top_rules": top_rules
    }


@router.get("/events/recent")
async def get_recent_events(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """Get recent escalation events with rule details."""
    events = db.query(EscalationEvent).options(
        joinedload(EscalationEvent.triggered_by_rule)
    ).order_by(
        EscalationEvent.created_at.desc()
    ).limit(limit).all()

    result = []
    for e in events:
        event_dict = e.to_dict()
        if e.triggered_by_rule:
            event_dict['rule_name'] = e.triggered_by_rule.rule_name
            event_dict['trigger_type'] = e.triggered_by_rule.trigger_type
        result.append(event_dict)

    return result


# ============== Prometheus Metrics ==============

@router.get("/metrics/prometheus", response_class=PlainTextResponse)
async def get_prometheus_metrics(
    db: Session = Depends(get_db)
) -> str:
    """
    Prometheus-compatible metrics endpoint for escalation stats.
    Scrape this endpoint with Prometheus at /api/escalation/metrics/prometheus
    """
    from sqlalchemy import func

    lines = []
    lines.append("# HELP athena_escalation_total Total number of escalation events")
    lines.append("# TYPE athena_escalation_total counter")

    # Escalations in last 24h by target model
    since_24h = datetime.utcnow() - timedelta(hours=24)

    model_counts = db.query(
        EscalationEvent.to_model,
        func.count(EscalationEvent.id)
    ).filter(
        EscalationEvent.created_at >= since_24h,
        EscalationEvent.event_type == "escalation"
    ).group_by(EscalationEvent.to_model).all()

    for model, count in model_counts:
        lines.append(f'athena_escalation_total{{target_model="{model}"}} {count}')

    # Escalations by trigger type
    lines.append("")
    lines.append("# HELP athena_escalation_by_trigger Escalations by trigger type (24h)")
    lines.append("# TYPE athena_escalation_by_trigger counter")

    trigger_counts = db.query(
        EscalationRule.trigger_type,
        func.count(EscalationEvent.id)
    ).join(
        EscalationRule, EscalationEvent.triggered_by_rule_id == EscalationRule.id
    ).filter(
        EscalationEvent.created_at >= since_24h,
        EscalationEvent.event_type == "escalation"
    ).group_by(EscalationRule.trigger_type).all()

    for trigger_type, count in trigger_counts:
        lines.append(f'athena_escalation_by_trigger{{trigger_type="{trigger_type}"}} {count}')

    # Unique sessions with escalations
    lines.append("")
    lines.append("# HELP athena_escalation_unique_sessions Unique sessions with escalations (24h)")
    lines.append("# TYPE athena_escalation_unique_sessions gauge")

    unique_sessions = db.query(
        func.count(func.distinct(EscalationEvent.session_id))
    ).filter(
        EscalationEvent.created_at >= since_24h,
        EscalationEvent.event_type == "escalation"
    ).scalar() or 0

    lines.append(f"athena_escalation_unique_sessions {unique_sessions}")

    # Active escalation states (currently escalated sessions)
    lines.append("")
    lines.append("# HELP athena_escalation_active_sessions Currently escalated sessions")
    lines.append("# TYPE athena_escalation_active_sessions gauge")

    active_escalations = db.query(func.count(EscalationState.id)).filter(
        or_(
            EscalationState.turns_remaining > 0,
            EscalationState.turns_remaining.is_(None)  # Indefinite overrides
        )
    ).scalar() or 0

    lines.append(f"athena_escalation_active_sessions {active_escalations}")

    # Manual overrides
    lines.append("")
    lines.append("# HELP athena_escalation_manual_overrides Active manual overrides")
    lines.append("# TYPE athena_escalation_manual_overrides gauge")

    manual_overrides = db.query(func.count(EscalationState.id)).filter(
        EscalationState.is_manual_override == True
    ).scalar() or 0

    lines.append(f"athena_escalation_manual_overrides {manual_overrides}")

    # Escalations by time buckets (1h, 6h, 24h)
    lines.append("")
    lines.append("# HELP athena_escalation_count_1h Escalations in last 1 hour")
    lines.append("# TYPE athena_escalation_count_1h gauge")

    since_1h = datetime.utcnow() - timedelta(hours=1)
    count_1h = db.query(func.count(EscalationEvent.id)).filter(
        EscalationEvent.created_at >= since_1h,
        EscalationEvent.event_type == "escalation"
    ).scalar() or 0
    lines.append(f"athena_escalation_count_1h {count_1h}")

    lines.append("")
    lines.append("# HELP athena_escalation_count_6h Escalations in last 6 hours")
    lines.append("# TYPE athena_escalation_count_6h gauge")

    since_6h = datetime.utcnow() - timedelta(hours=6)
    count_6h = db.query(func.count(EscalationEvent.id)).filter(
        EscalationEvent.created_at >= since_6h,
        EscalationEvent.event_type == "escalation"
    ).scalar() or 0
    lines.append(f"athena_escalation_count_6h {count_6h}")

    lines.append("")
    lines.append("# HELP athena_escalation_count_24h Escalations in last 24 hours")
    lines.append("# TYPE athena_escalation_count_24h gauge")

    count_24h = db.query(func.count(EscalationEvent.id)).filter(
        EscalationEvent.created_at >= since_24h,
        EscalationEvent.event_type == "escalation"
    ).scalar() or 0
    lines.append(f"athena_escalation_count_24h {count_24h}")

    # Top triggered rules
    lines.append("")
    lines.append("# HELP athena_escalation_rule_triggers Rule trigger counts (24h)")
    lines.append("# TYPE athena_escalation_rule_triggers counter")

    rule_counts = db.query(
        EscalationRule.rule_name,
        EscalationRule.trigger_type,
        func.count(EscalationEvent.id).label('count')
    ).join(
        EscalationRule, EscalationEvent.triggered_by_rule_id == EscalationRule.id
    ).filter(
        EscalationEvent.created_at >= since_24h,
        EscalationEvent.event_type == "escalation"
    ).group_by(
        EscalationRule.rule_name, EscalationRule.trigger_type
    ).all()

    for rule_name, trigger_type, count in rule_counts:
        # Escape quotes in rule name for Prometheus label
        safe_rule_name = rule_name.replace('"', '\\"')
        lines.append(f'athena_escalation_rule_triggers{{rule_name="{safe_rule_name}",trigger_type="{trigger_type}"}} {count}')

    return "\n".join(lines) + "\n"
