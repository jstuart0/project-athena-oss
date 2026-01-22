"""
Multi-intent configuration API routes.

Provides configuration and management for multi-intent query processing.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, MultiIntentConfig, IntentChainRule

logger = structlog.get_logger()

router = APIRouter(prefix="/api/multi-intent", tags=["multi-intent"])


class MultiIntentConfigUpdate(BaseModel):
    """Request model for updating multi-intent configuration."""
    enabled: bool = None
    max_intents_per_query: int = None
    separators: List[str] = None
    context_preservation: bool = None
    parallel_processing: bool = None
    combination_strategy: str = None
    min_words_per_intent: int = None
    context_words_to_preserve: List[str] = None


class IntentChainRuleCreate(BaseModel):
    """Request model for creating intent chain rule."""
    name: str
    trigger_pattern: str = None
    intent_sequence: List[str]
    enabled: bool = True
    description: str = None
    examples: List[str] = None
    require_all: bool = False
    stop_on_error: bool = True


class IntentChainRuleUpdate(BaseModel):
    """Request model for updating intent chain rule."""
    trigger_pattern: str = None
    intent_sequence: List[str] = None
    enabled: bool = None
    description: str = None
    examples: List[str] = None
    require_all: bool = None
    stop_on_error: bool = None


@router.get("/config")
async def get_multi_intent_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get multi-intent configuration."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(MultiIntentConfig).first()
    if not config:
        # Create default config if none exists
        config = MultiIntentConfig()
        db.add(config)
        db.commit()
        db.refresh(config)

    return config.to_dict()


@router.put("/config")
async def update_multi_intent_config(
    config_data: MultiIntentConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update multi-intent configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(MultiIntentConfig).first()
    if not config:
        config = MultiIntentConfig()
        db.add(config)

    # Update fields
    if config_data.enabled is not None:
        config.enabled = config_data.enabled
    if config_data.max_intents_per_query is not None:
        config.max_intents_per_query = config_data.max_intents_per_query
    if config_data.separators is not None:
        config.separators = config_data.separators
    if config_data.context_preservation is not None:
        config.context_preservation = config_data.context_preservation
    if config_data.parallel_processing is not None:
        config.parallel_processing = config_data.parallel_processing
    if config_data.combination_strategy is not None:
        config.combination_strategy = config_data.combination_strategy
    if config_data.min_words_per_intent is not None:
        config.min_words_per_intent = config_data.min_words_per_intent
    if config_data.context_words_to_preserve is not None:
        config.context_words_to_preserve = config_data.context_words_to_preserve

    db.commit()
    db.refresh(config)

    logger.info("multi_intent_config_updated", user=current_user.username)

    return config.to_dict()


@router.get("/chains")
async def list_intent_chains(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all intent chain rules."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(IntentChainRule)

    if enabled_only:
        query = query.filter(IntentChainRule.enabled == True)

    chains = query.order_by(IntentChainRule.name).all()
    return {"intent_chains": [c.to_dict() for c in chains]}


@router.get("/chains/{chain_id}")
async def get_intent_chain(
    chain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific intent chain by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    chain = db.query(IntentChainRule).filter(IntentChainRule.id == chain_id).first()
    if not chain:
        raise HTTPException(status_code=404, detail="Intent chain not found")

    return chain.to_dict()


@router.post("/chains", status_code=201)
async def create_intent_chain(
    chain_data: IntentChainRuleCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new intent chain rule."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Create chain
    chain = IntentChainRule(
        name=chain_data.name,
        trigger_pattern=chain_data.trigger_pattern,
        intent_sequence=chain_data.intent_sequence,
        enabled=chain_data.enabled,
        description=chain_data.description,
        examples=chain_data.examples,
        require_all=chain_data.require_all,
        stop_on_error=chain_data.stop_on_error
    )
    db.add(chain)
    db.commit()
    db.refresh(chain)

    logger.info("intent_chain_created", chain_id=chain.id, name=chain.name, user=current_user.username)

    return chain.to_dict()


@router.put("/chains/{chain_id}")
async def update_intent_chain(
    chain_id: int,
    chain_data: IntentChainRuleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing intent chain rule."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    chain = db.query(IntentChainRule).filter(IntentChainRule.id == chain_id).first()
    if not chain:
        raise HTTPException(status_code=404, detail="Intent chain not found")

    # Update fields
    if chain_data.trigger_pattern is not None:
        chain.trigger_pattern = chain_data.trigger_pattern
    if chain_data.intent_sequence is not None:
        chain.intent_sequence = chain_data.intent_sequence
    if chain_data.enabled is not None:
        chain.enabled = chain_data.enabled
    if chain_data.description is not None:
        chain.description = chain_data.description
    if chain_data.examples is not None:
        chain.examples = chain_data.examples
    if chain_data.require_all is not None:
        chain.require_all = chain_data.require_all
    if chain_data.stop_on_error is not None:
        chain.stop_on_error = chain_data.stop_on_error

    db.commit()
    db.refresh(chain)

    logger.info("intent_chain_updated", chain_id=chain.id, name=chain.name, user=current_user.username)

    return chain.to_dict()


@router.delete("/chains/{chain_id}", status_code=204)
async def delete_intent_chain(
    chain_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an intent chain rule."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    chain = db.query(IntentChainRule).filter(IntentChainRule.id == chain_id).first()
    if not chain:
        raise HTTPException(status_code=404, detail="Intent chain not found")

    chain_name = chain.name

    db.delete(chain)
    db.commit()

    logger.info("intent_chain_deleted", chain_id=chain_id, name=chain_name, user=current_user.username)

    return None
