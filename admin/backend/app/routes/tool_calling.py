"""
Tool Calling Management API Routes.

Provides CRUD operations for tool registry, settings, triggers, and usage metrics.
Enables admin control of hybrid RAG system with LLM tool calling.
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, Integer, text
from pydantic import BaseModel, Field
import structlog
from datetime import datetime, timedelta

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ToolRegistry, ToolCallingSetting, ToolCallingTrigger, ToolUsageMetric, ToolApiKeyRequirement, ExternalAPIKey

logger = structlog.get_logger()

router = APIRouter(prefix="/api/tool-calling", tags=["tool-calling"])


# ============================================================================
# Pydantic Models for Request/Response
# ============================================================================

class ToolRegistryResponse(BaseModel):
    """Response model for tool registry data."""
    id: int
    tool_name: str
    display_name: str
    description: str
    category: str
    function_schema: Dict[str, Any]
    enabled: bool
    guest_mode_allowed: bool
    requires_auth: bool
    rate_limit_per_minute: Optional[int] = None
    timeout_seconds: int
    priority: int
    service_url: Optional[str] = None
    web_search_fallback_enabled: bool = True
    required_api_keys: Optional[List[str]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ToolRegistryCreate(BaseModel):
    """Request model for creating a new tool."""
    tool_name: str
    display_name: str
    description: str
    category: str
    function_schema: Dict[str, Any]
    enabled: bool = True
    guest_mode_allowed: bool = False
    requires_auth: bool = False
    rate_limit_per_minute: Optional[int] = None
    timeout_seconds: int = 30
    priority: int = 100
    service_url: Optional[str] = None
    web_search_fallback_enabled: bool = True


class ToolRegistryUpdate(BaseModel):
    """Request model for updating a tool."""
    display_name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    guest_mode_allowed: Optional[bool] = None
    timeout_seconds: Optional[int] = None
    priority: Optional[int] = None
    web_search_fallback_enabled: Optional[bool] = None


class ToolCallingSettingsResponse(BaseModel):
    """Response model for tool calling settings."""
    id: int
    enabled: bool
    llm_model: str
    llm_backend: str
    max_parallel_tools: int
    tool_call_timeout_seconds: int
    temperature: float
    max_tokens: int
    fallback_to_direct_llm: bool
    cache_results: bool
    cache_ttl_seconds: int
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ToolCallingSettingsUpdate(BaseModel):
    """Request model for updating tool calling settings."""
    enabled: Optional[bool] = None
    llm_model: Optional[str] = None
    llm_backend: Optional[str] = None
    max_parallel_tools: Optional[int] = None
    tool_call_timeout_seconds: Optional[int] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    fallback_to_direct_llm: Optional[bool] = None
    cache_results: Optional[bool] = None
    cache_ttl_seconds: Optional[int] = None


class ToolCallingTriggerResponse(BaseModel):
    """Response model for tool calling trigger data."""
    id: int
    trigger_name: str
    trigger_type: str
    enabled: bool
    priority: int
    config: Dict[str, Any]
    description: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ToolCallingTriggerUpdate(BaseModel):
    """Request model for updating a trigger."""
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    config: Optional[Dict[str, Any]] = None
    description: Optional[str] = None


class ToolUsageMetricResponse(BaseModel):
    """Response model for tool usage metric data."""
    id: int
    timestamp: str
    tool_name: str
    success: bool
    latency_ms: int
    error_message: Optional[str] = None
    trigger_reason: Optional[str] = None
    intent: Optional[str] = None
    confidence: Optional[float] = None
    guest_mode: bool
    request_id: Optional[str] = None
    session_id: Optional[str] = None

    class Config:
        from_attributes = True


class ToolMetricsAggregation(BaseModel):
    """Aggregated metrics for a tool."""
    tool_name: str
    total_calls: int
    success_count: int
    error_count: int
    success_rate: float
    avg_latency_ms: float
    last_called: Optional[datetime] = None
    p50_latency_ms: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    p99_latency_ms: Optional[float] = None


class ToolMetricRecord(BaseModel):
    """Request model for recording a tool usage metric."""
    tool_name: str
    success: bool
    latency_ms: int
    error_message: Optional[str] = None
    trigger_reason: Optional[str] = None
    intent: Optional[str] = None
    confidence: Optional[float] = None
    guest_mode: bool = False
    request_id: Optional[str] = None
    session_id: Optional[str] = None


# ============================================================================
# Tool API Key Requirement Models
# ============================================================================

class ToolApiKeyRequirementResponse(BaseModel):
    """Response model for tool API key requirement."""
    id: int
    tool_id: int
    api_key_service: str
    is_required: bool
    inject_as: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class ToolApiKeyRequirementCreate(BaseModel):
    """Request model for creating a tool API key requirement."""
    tool_id: int
    api_key_service: str
    is_required: bool = True
    inject_as: Optional[str] = None
    description: Optional[str] = None


class ToolApiKeyRequirementUpdate(BaseModel):
    """Request model for updating a tool API key requirement."""
    is_required: Optional[bool] = None
    inject_as: Optional[str] = None
    description: Optional[str] = None


class AvailableApiKeyService(BaseModel):
    """Response model for available API key service."""
    service_name: str
    api_name: str
    enabled: bool
    key_type: Optional[str] = None


class ToolWithApiKeys(BaseModel):
    """Response model for tool with its API key requirements."""
    tool_id: int
    tool_name: str
    display_name: str
    required_api_keys: List[str]
    api_key_requirements: List[ToolApiKeyRequirementResponse]


# ============================================================================
# Tool Registry Endpoints
# ============================================================================

@router.get("/tools/public", response_model=List[ToolRegistryResponse])
async def list_tools_public(
    enabled_only: bool = False,
    category: Optional[str] = None,
    guest_mode_only: bool = False,
    db: Session = Depends(get_db)
):
    """
    List all tools (public endpoint, no auth required).

    This endpoint is used by services (Orchestrator, etc.) to get
    available tools without requiring authentication.

    Query params:
    - enabled_only: If true, only return enabled tools
    - category: Filter by category (optional)
    - guest_mode_only: If true, only return guest-mode allowed tools

    Returns:
        List of tools with their configuration
    """
    logger.info("list_tools_public", enabled_only=enabled_only, category=category, guest_mode_only=guest_mode_only)

    query = db.query(ToolRegistry)

    if enabled_only:
        query = query.filter(ToolRegistry.enabled == True)
    if category:
        query = query.filter(ToolRegistry.category == category)
    if guest_mode_only:
        query = query.filter(ToolRegistry.guest_mode_allowed == True)

    tools = query.order_by(ToolRegistry.priority, ToolRegistry.tool_name).all()

    return [ToolRegistryResponse(**tool.to_dict()) for tool in tools]


@router.get("/tools", response_model=List[ToolRegistryResponse])
async def list_tools(
    enabled_only: bool = False,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all tools (authenticated endpoint).

    Query params:
    - enabled_only: If true, only return enabled tools
    - category: Filter by category (optional)

    Returns:
        List of tools with their configuration
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_tools", user=current_user.username, enabled_only=enabled_only, category=category)

    query = db.query(ToolRegistry)

    if enabled_only:
        query = query.filter(ToolRegistry.enabled == True)
    if category:
        query = query.filter(ToolRegistry.category == category)

    tools = query.order_by(ToolRegistry.priority, ToolRegistry.tool_name).all()

    return [ToolRegistryResponse(**tool.to_dict()) for tool in tools]


@router.get("/tools/{tool_id}", response_model=ToolRegistryResponse)
async def get_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get specific tool by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    logger.info("get_tool", tool_id=tool_id, user=current_user.username)

    return ToolRegistryResponse(**tool.to_dict())


@router.post("/tools", response_model=ToolRegistryResponse)
async def create_tool(
    tool_data: ToolRegistryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new tool."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if tool with same name already exists
    existing = db.query(ToolRegistry).filter(ToolRegistry.tool_name == tool_data.tool_name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Tool with name '{tool_data.tool_name}' already exists")

    tool = ToolRegistry(**tool_data.model_dump())
    db.add(tool)
    db.commit()
    db.refresh(tool)

    logger.info("create_tool", tool_name=tool.tool_name, user=current_user.username)

    return ToolRegistryResponse(**tool.to_dict())


@router.put("/tools/{tool_id}", response_model=ToolRegistryResponse)
async def update_tool(
    tool_id: int,
    tool_data: ToolRegistryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update tool configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    # Update fields
    update_data = tool_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(tool, field, value)

    db.commit()
    db.refresh(tool)

    logger.info("update_tool", tool_id=tool_id, tool_name=tool.tool_name, updated_fields=list(update_data.keys()), user=current_user.username)

    return ToolRegistryResponse(**tool.to_dict())


@router.put("/tools/{tool_id}/toggle", response_model=ToolRegistryResponse)
async def toggle_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle tool enabled/disabled status."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    tool.enabled = not tool.enabled
    db.commit()
    db.refresh(tool)

    logger.info("toggle_tool", tool_id=tool_id, tool_name=tool.tool_name, enabled=tool.enabled, user=current_user.username)

    return ToolRegistryResponse(**tool.to_dict())


@router.delete("/tools/{tool_id}")
async def delete_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a tool."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    tool_name = tool.tool_name
    db.delete(tool)
    db.commit()

    logger.info("delete_tool", tool_id=tool_id, tool_name=tool_name, user=current_user.username)

    return {"status": "deleted", "tool_name": tool_name}


@router.post("/tools/by-name/{tool_name}/toggle", response_model=ToolRegistryResponse)
async def toggle_tool_by_name(
    tool_name: str,
    body: dict = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Toggle or set tool enabled/disabled status by tool name.

    Body (optional):
    - enabled: bool - If provided, set to this value. Otherwise, toggle current state.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    tool = db.query(ToolRegistry).filter(ToolRegistry.tool_name == tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    if body and 'enabled' in body:
        tool.enabled = body['enabled']
    else:
        tool.enabled = not tool.enabled

    db.commit()
    db.refresh(tool)

    logger.info("toggle_tool_by_name", tool_name=tool_name, enabled=tool.enabled, user=current_user.username)

    return ToolRegistryResponse(**tool.to_dict())


@router.get("/tools/stats")
async def get_tool_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get statistics about registered tools.

    Returns counts of tools by source, enabled status, and category.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("get_tool_stats", user=current_user.username)

    # Get counts using raw SQL for efficiency
    stats_query = text("""
        SELECT
            COUNT(*) FILTER (WHERE source = 'static') as static_count,
            COUNT(*) FILTER (WHERE source = 'mcp') as mcp_count,
            COUNT(*) FILTER (WHERE source = 'legacy') as legacy_count,
            COUNT(*) FILTER (WHERE source IS NULL OR source NOT IN ('static', 'mcp', 'legacy')) as other_count,
            COUNT(*) FILTER (WHERE enabled = true) as enabled_count,
            COUNT(*) FILTER (WHERE enabled = false) as disabled_count,
            COUNT(*) as total_count
        FROM tool_registry
    """)

    result = db.execute(stats_query).fetchone()

    # Get category breakdown
    category_query = text("""
        SELECT category, COUNT(*) as count
        FROM tool_registry
        GROUP BY category
        ORDER BY count DESC
    """)
    categories = db.execute(category_query).fetchall()

    return {
        "by_source": {
            "static": result.static_count or 0,
            "mcp": result.mcp_count or 0,
            "legacy": result.legacy_count or 0,
            "other": result.other_count or 0,
        },
        "by_status": {
            "enabled": result.enabled_count or 0,
            "disabled": result.disabled_count or 0,
        },
        "by_category": {cat.category: cat.count for cat in categories if cat.category},
        "total": result.total_count or 0,
    }


@router.get("/tools/stats/public")
async def get_tool_stats_public(db: Session = Depends(get_db)):
    """
    Get statistics about registered tools (public endpoint).

    Used by services to get tool counts without authentication.
    """
    logger.info("get_tool_stats_public")

    stats_query = text("""
        SELECT
            COUNT(*) FILTER (WHERE source = 'static') as static_count,
            COUNT(*) FILTER (WHERE source = 'mcp') as mcp_count,
            COUNT(*) FILTER (WHERE source = 'legacy') as legacy_count,
            COUNT(*) FILTER (WHERE enabled = true) as enabled_count,
            COUNT(*) as total_count
        FROM tool_registry
    """)

    result = db.execute(stats_query).fetchone()

    return {
        "static_count": result.static_count or 0,
        "mcp_count": result.mcp_count or 0,
        "legacy_count": result.legacy_count or 0,
        "enabled_count": result.enabled_count or 0,
        "total_count": result.total_count or 0,
    }


# ============================================================================
# Tool Calling Settings Endpoints
# ============================================================================

@router.get("/settings/public", response_model=ToolCallingSettingsResponse)
async def get_settings_public(db: Session = Depends(get_db)):
    """
    Get tool calling settings (public endpoint, no auth required).

    This endpoint is used by services to get configuration
    without requiring authentication.

    Returns:
        Tool calling settings
    """
    logger.info("get_settings_public", source="public")

    settings = db.query(ToolCallingSetting).first()
    if not settings:
        # Return default settings if none exist
        return ToolCallingSettingsResponse(
            id=0,
            enabled=True,
            llm_model="gpt-4o-mini",
            llm_backend="openai",
            max_parallel_tools=3,
            tool_call_timeout_seconds=30,
            temperature=0.1,
            max_tokens=500,
            fallback_to_direct_llm=True,
            cache_results=True,
            cache_ttl_seconds=300,
            updated_at=None
        )

    return ToolCallingSettingsResponse(**settings.to_dict())


@router.get("/settings", response_model=ToolCallingSettingsResponse)
async def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get tool calling settings (authenticated endpoint)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("get_settings", user=current_user.username)

    settings = db.query(ToolCallingSetting).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found. Run database seed script.")

    return ToolCallingSettingsResponse(**settings.to_dict())


@router.put("/settings", response_model=ToolCallingSettingsResponse)
async def update_settings(
    settings_data: ToolCallingSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update tool calling settings."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    settings = db.query(ToolCallingSetting).first()
    if not settings:
        # Create default settings if none exist
        settings = ToolCallingSetting(
            enabled=True,
            llm_model="gpt-4o-mini",
            llm_backend="openai",
            max_parallel_tools=3,
            tool_call_timeout_seconds=30,
            temperature=0.1,
            max_tokens=500,
            fallback_to_direct_llm=True,
            cache_results=True,
            cache_ttl_seconds=300
        )
        db.add(settings)

    # Update fields
    update_data = settings_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(settings, field, value)

    db.commit()
    db.refresh(settings)

    logger.info("update_settings", updated_fields=list(update_data.keys()), user=current_user.username)

    return ToolCallingSettingsResponse(**settings.to_dict())


# ============================================================================
# Tool Calling Triggers Endpoints
# ============================================================================

@router.get("/triggers/public", response_model=List[ToolCallingTriggerResponse])
async def list_triggers_public(
    enabled_only: bool = False,
    db: Session = Depends(get_db)
):
    """
    List all triggers (public endpoint, no auth required).

    Query params:
    - enabled_only: If true, only return enabled triggers

    Returns:
        List of triggers with their configuration
    """
    logger.info("list_triggers_public", enabled_only=enabled_only)

    query = db.query(ToolCallingTrigger)

    if enabled_only:
        query = query.filter(ToolCallingTrigger.enabled == True)

    triggers = query.order_by(ToolCallingTrigger.priority, ToolCallingTrigger.trigger_name).all()

    return [ToolCallingTriggerResponse(**trigger.to_dict()) for trigger in triggers]


@router.get("/triggers", response_model=List[ToolCallingTriggerResponse])
async def list_triggers(
    enabled_only: bool = False,
    trigger_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all triggers (authenticated endpoint).

    Query params:
    - enabled_only: If true, only return enabled triggers
    - trigger_type: Filter by trigger type (optional)

    Returns:
        List of triggers with their configuration
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_triggers", user=current_user.username, enabled_only=enabled_only, trigger_type=trigger_type)

    query = db.query(ToolCallingTrigger)

    if enabled_only:
        query = query.filter(ToolCallingTrigger.enabled == True)
    if trigger_type:
        query = query.filter(ToolCallingTrigger.trigger_type == trigger_type)

    triggers = query.order_by(ToolCallingTrigger.priority, ToolCallingTrigger.trigger_name).all()

    return [ToolCallingTriggerResponse(**trigger.to_dict()) for trigger in triggers]


@router.put("/triggers/{trigger_id}", response_model=ToolCallingTriggerResponse)
async def update_trigger(
    trigger_id: int,
    trigger_data: ToolCallingTriggerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update trigger configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    trigger = db.query(ToolCallingTrigger).filter(ToolCallingTrigger.id == trigger_id).first()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")

    # Update fields
    update_data = trigger_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(trigger, field, value)

    db.commit()
    db.refresh(trigger)

    logger.info("update_trigger", trigger_id=trigger_id, trigger_name=trigger.trigger_name, updated_fields=list(update_data.keys()), user=current_user.username)

    return ToolCallingTriggerResponse(**trigger.to_dict())


@router.put("/triggers/{trigger_id}/toggle", response_model=ToolCallingTriggerResponse)
async def toggle_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle trigger enabled/disabled status."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    trigger = db.query(ToolCallingTrigger).filter(ToolCallingTrigger.id == trigger_id).first()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")

    trigger.enabled = not trigger.enabled
    db.commit()
    db.refresh(trigger)

    logger.info("toggle_trigger", trigger_id=trigger_id, trigger_name=trigger.trigger_name, enabled=trigger.enabled, user=current_user.username)

    return ToolCallingTriggerResponse(**trigger.to_dict())


# ============================================================================
# Tool Usage Metrics Endpoints
# ============================================================================

@router.post("/metrics/record")
async def record_metric(
    metric: ToolMetricRecord,
    db: Session = Depends(get_db)
):
    """
    Record a tool usage metric.

    This endpoint is PUBLIC (no auth required) so the orchestrator can call it.

    Args:
        metric: Tool usage metric data

    Returns:
        Confirmation with metric ID
    """
    logger.info("record_metric", tool_name=metric.tool_name, success=metric.success)

    db_metric = ToolUsageMetric(
        tool_name=metric.tool_name,
        success=metric.success,
        latency_ms=metric.latency_ms,
        error_message=metric.error_message,
        trigger_reason=metric.trigger_reason,
        intent=metric.intent,
        confidence=metric.confidence,
        guest_mode=metric.guest_mode,
        request_id=metric.request_id,
        session_id=metric.session_id,
        timestamp=datetime.utcnow()
    )

    db.add(db_metric)
    db.commit()
    db.refresh(db_metric)

    return {"success": True, "metric_id": db_metric.id}


@router.get("/metrics", response_model=List[ToolUsageMetricResponse])
async def list_metrics(
    tool_name: Optional[str] = None,
    success_only: bool = False,
    hours_ago: int = 24,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List tool usage metrics.

    Query params:
    - tool_name: Filter by tool name (optional)
    - success_only: If true, only return successful calls
    - hours_ago: Number of hours to look back (default 24)
    - limit: Maximum number of results (default 100)

    Returns:
        List of tool usage metrics
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_metrics", user=current_user.username, tool_name=tool_name, hours_ago=hours_ago)

    # Calculate time threshold
    time_threshold = datetime.utcnow() - timedelta(hours=hours_ago)

    query = db.query(ToolUsageMetric).filter(ToolUsageMetric.timestamp >= time_threshold)

    if tool_name:
        query = query.filter(ToolUsageMetric.tool_name == tool_name)
    if success_only:
        query = query.filter(ToolUsageMetric.success == True)

    metrics = query.order_by(desc(ToolUsageMetric.timestamp)).limit(limit).all()

    return [ToolUsageMetricResponse(**metric.to_dict()) for metric in metrics]


@router.get("/metrics/aggregated", response_model=List[ToolMetricsAggregation])
async def get_aggregated_metrics(
    hours_ago: int = 24,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get aggregated metrics for all tools.

    Query params:
    - hours_ago: Number of hours to look back (default 24)

    Returns:
        List of aggregated metrics per tool
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("get_aggregated_metrics", user=current_user.username, hours_ago=hours_ago)

    # Calculate time threshold
    time_threshold = datetime.utcnow() - timedelta(hours=hours_ago)

    # Aggregate metrics by tool_name with percentiles using PostgreSQL's percentile_cont
    # Note: percentile_cont is an ordered-set aggregate, requiring raw SQL
    percentile_query = text("""
        SELECT
            tool_name,
            COUNT(*) as total_calls,
            SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
            AVG(latency_ms) as avg_latency_ms,
            MAX(timestamp) as last_called,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) as p50_latency_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_latency_ms,
            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) as p99_latency_ms
        FROM tool_usage_metrics
        WHERE timestamp >= :time_threshold
        GROUP BY tool_name
    """)

    results = db.execute(percentile_query, {"time_threshold": time_threshold}).fetchall()

    aggregations = []
    for result in results:
        total_calls = result.total_calls
        success_count = result.success_count or 0
        error_count = total_calls - success_count
        success_rate = (success_count / total_calls * 100) if total_calls > 0 else 0.0

        aggregations.append(ToolMetricsAggregation(
            tool_name=result.tool_name,
            total_calls=total_calls,
            success_count=success_count,
            error_count=error_count,
            last_called=result.last_called,
            success_rate=success_rate,
            avg_latency_ms=round(result.avg_latency_ms, 2) if result.avg_latency_ms else 0.0,
            p50_latency_ms=round(result.p50_latency_ms, 2) if result.p50_latency_ms else None,
            p95_latency_ms=round(result.p95_latency_ms, 2) if result.p95_latency_ms else None,
            p99_latency_ms=round(result.p99_latency_ms, 2) if result.p99_latency_ms else None
        ))

    # Sort by total calls descending
    aggregations.sort(key=lambda x: x.total_calls, reverse=True)

    return aggregations


# ============================================================================
# Tool API Key Requirements Endpoints
# ============================================================================

@router.get("/api-keys/available", response_model=List[AvailableApiKeyService])
async def list_available_api_keys(
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List available API key services that can be linked to tools.

    Returns list of services from external_api_keys table.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_available_api_keys", user=current_user.username, enabled_only=enabled_only)

    query = db.query(ExternalAPIKey)
    if enabled_only:
        query = query.filter(ExternalAPIKey.enabled == True)

    api_keys = query.order_by(ExternalAPIKey.service_name).all()

    return [
        AvailableApiKeyService(
            service_name=key.service_name,
            api_name=key.api_name,
            enabled=key.enabled,
            key_type=key.key_type
        )
        for key in api_keys
    ]


@router.get("/tools/{tool_id}/api-keys", response_model=List[ToolApiKeyRequirementResponse])
async def get_tool_api_key_requirements(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get API key requirements for a specific tool."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    logger.info("get_tool_api_key_requirements", tool_id=tool_id, tool_name=tool.tool_name, user=current_user.username)

    requirements = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.tool_id == tool_id
    ).all()

    return [ToolApiKeyRequirementResponse(**req.to_dict()) for req in requirements]


@router.get("/tools/{tool_id}/api-keys/public", response_model=List[ToolApiKeyRequirementResponse])
async def get_tool_api_key_requirements_public(
    tool_id: int,
    db: Session = Depends(get_db)
):
    """
    Get API key requirements for a specific tool (public endpoint).

    Used by orchestrator to determine which keys to inject.
    """
    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    logger.info("get_tool_api_key_requirements_public", tool_id=tool_id, tool_name=tool.tool_name)

    requirements = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.tool_id == tool_id
    ).all()

    return [ToolApiKeyRequirementResponse(**req.to_dict()) for req in requirements]


@router.get("/tools/by-name/{tool_name}/api-keys/public", response_model=List[ToolApiKeyRequirementResponse])
async def get_tool_api_key_requirements_by_name(
    tool_name: str,
    db: Session = Depends(get_db)
):
    """
    Get API key requirements for a tool by name (public endpoint).

    Used by orchestrator to determine which keys to inject when tool name is known.
    """
    tool = db.query(ToolRegistry).filter(ToolRegistry.tool_name == tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    logger.info("get_tool_api_key_requirements_by_name", tool_name=tool_name)

    requirements = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.tool_id == tool.id
    ).all()

    return [ToolApiKeyRequirementResponse(**req.to_dict()) for req in requirements]


@router.post("/tools/{tool_id}/api-keys", response_model=ToolApiKeyRequirementResponse)
async def add_tool_api_key_requirement(
    tool_id: int,
    requirement: ToolApiKeyRequirementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add an API key requirement to a tool."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Verify tool exists
    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    # Verify API key service exists
    api_key = db.query(ExternalAPIKey).filter(
        ExternalAPIKey.service_name == requirement.api_key_service
    ).first()
    if not api_key:
        raise HTTPException(status_code=404, detail=f"API key service '{requirement.api_key_service}' not found")

    # Check if requirement already exists
    existing = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.tool_id == tool_id,
        ToolApiKeyRequirement.api_key_service == requirement.api_key_service
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Tool already has requirement for '{requirement.api_key_service}'")

    # Create requirement
    db_requirement = ToolApiKeyRequirement(
        tool_id=tool_id,
        api_key_service=requirement.api_key_service,
        is_required=requirement.is_required,
        inject_as=requirement.inject_as,
        description=requirement.description
    )
    db.add(db_requirement)

    # Update cache field on tool
    _update_tool_api_keys_cache(db, tool_id)

    db.commit()
    db.refresh(db_requirement)

    logger.info("add_tool_api_key_requirement",
                tool_id=tool_id,
                tool_name=tool.tool_name,
                api_key_service=requirement.api_key_service,
                user=current_user.username)

    return ToolApiKeyRequirementResponse(**db_requirement.to_dict())


@router.put("/api-key-requirements/{requirement_id}", response_model=ToolApiKeyRequirementResponse)
async def update_tool_api_key_requirement(
    requirement_id: int,
    update_data: ToolApiKeyRequirementUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a tool API key requirement."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    requirement = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.id == requirement_id
    ).first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for field, value in update_dict.items():
        setattr(requirement, field, value)

    db.commit()
    db.refresh(requirement)

    logger.info("update_tool_api_key_requirement",
                requirement_id=requirement_id,
                tool_id=requirement.tool_id,
                updated_fields=list(update_dict.keys()),
                user=current_user.username)

    return ToolApiKeyRequirementResponse(**requirement.to_dict())


@router.delete("/api-key-requirements/{requirement_id}")
async def delete_tool_api_key_requirement(
    requirement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a tool API key requirement."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    requirement = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.id == requirement_id
    ).first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    tool_id = requirement.tool_id
    api_key_service = requirement.api_key_service

    db.delete(requirement)

    # Update cache field on tool
    _update_tool_api_keys_cache(db, tool_id)

    db.commit()

    logger.info("delete_tool_api_key_requirement",
                requirement_id=requirement_id,
                tool_id=tool_id,
                api_key_service=api_key_service,
                user=current_user.username)

    return {"status": "deleted", "api_key_service": api_key_service}


def _update_tool_api_keys_cache(db: Session, tool_id: int):
    """Update the required_api_keys cache field on a tool."""
    requirements = db.query(ToolApiKeyRequirement).filter(
        ToolApiKeyRequirement.tool_id == tool_id
    ).all()

    api_keys = [req.api_key_service for req in requirements]

    tool = db.query(ToolRegistry).filter(ToolRegistry.id == tool_id).first()
    if tool:
        tool.required_api_keys = api_keys


@router.get("/tools-with-api-keys", response_model=List[ToolWithApiKeys])
async def list_tools_with_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all tools with their API key requirements."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_tools_with_api_keys", user=current_user.username)

    tools = db.query(ToolRegistry).order_by(ToolRegistry.tool_name).all()

    result = []
    for tool in tools:
        requirements = db.query(ToolApiKeyRequirement).filter(
            ToolApiKeyRequirement.tool_id == tool.id
        ).all()

        result.append(ToolWithApiKeys(
            tool_id=tool.id,
            tool_name=tool.tool_name,
            display_name=tool.display_name,
            required_api_keys=tool.required_api_keys or [],
            api_key_requirements=[
                ToolApiKeyRequirementResponse(**req.to_dict())
                for req in requirements
            ]
        ))

    return result


from sqlalchemy.types import Integer as IntegerType
# Fix import for Integer type casting
Integer = IntegerType


# ============================================================================
# MCP Discovery and Refresh Endpoints
# ============================================================================

class MCPDiscoveryRequest(BaseModel):
    """Request model for MCP discovery."""
    mcp_url: Optional[str] = None  # Override URL, or use from feature flags


class MCPDiscoveryResult(BaseModel):
    """Response model for MCP discovery."""
    success: bool
    discovered_count: int
    tools: List[Dict[str, Any]]
    mcp_url: Optional[str] = None
    error: Optional[str] = None


class ToolRefreshResult(BaseModel):
    """Response model for tool refresh."""
    success: bool
    static_count: int
    mcp_count: int
    legacy_count: int
    total_unique: int
    duration_ms: float


@router.post("/mcp/discover", response_model=MCPDiscoveryResult)
async def discover_mcp_tools(
    request: MCPDiscoveryRequest = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Trigger MCP tool discovery from n8n or other MCP-compatible endpoints.

    This endpoint:
    1. Connects to the configured MCP URL (or provided URL)
    2. Fetches available tools via MCP protocol
    3. Returns discovered tools (does NOT automatically add to registry)

    To add discovered tools to the registry, use POST /api/mcp-security/approval-queue
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    import httpx
    import os
    from app.models import FeatureFlag

    # Get MCP URL from request, environment, or feature flag config
    mcp_url = None
    if request and request.mcp_url:
        mcp_url = request.mcp_url
    else:
        mcp_url = os.getenv("N8N_MCP_URL")
        if not mcp_url:
            # Check feature flag config
            flag = db.query(FeatureFlag).filter(FeatureFlag.name == 'mcp_integration').first()
            if flag and flag.config:
                mcp_url = flag.config.get('mcp_url')

    if not mcp_url:
        return MCPDiscoveryResult(
            success=False,
            discovered_count=0,
            tools=[],
            error="MCP URL not configured. Set N8N_MCP_URL env var or configure mcp_integration feature flag."
        )

    logger.info("mcp_discovery_started", mcp_url=mcp_url, user=current_user.username)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{mcp_url}/mcp/tools/list",
                json={},
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 200:
                data = response.json()
                tools = data.get('tools', [])

                logger.info("mcp_discovery_success",
                           mcp_url=mcp_url,
                           discovered_count=len(tools),
                           user=current_user.username)

                return MCPDiscoveryResult(
                    success=True,
                    discovered_count=len(tools),
                    tools=tools,
                    mcp_url=mcp_url
                )
            else:
                error_msg = f"MCP endpoint returned status {response.status_code}"
                logger.warning("mcp_discovery_failed",
                              mcp_url=mcp_url,
                              status_code=response.status_code,
                              user=current_user.username)

                return MCPDiscoveryResult(
                    success=False,
                    discovered_count=0,
                    tools=[],
                    mcp_url=mcp_url,
                    error=error_msg
                )

    except Exception as e:
        error_msg = str(e)
        logger.error("mcp_discovery_error",
                    mcp_url=mcp_url,
                    error=error_msg,
                    user=current_user.username)

        return MCPDiscoveryResult(
            success=False,
            discovered_count=0,
            tools=[],
            mcp_url=mcp_url,
            error=error_msg
        )


@router.get("/mcp/status")
async def get_mcp_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get current MCP integration status.

    Returns:
    - MCP integration enabled/disabled
    - n8n integration enabled/disabled
    - Configured MCP URL
    - Last discovery timestamp (if tracked)
    - Number of MCP tools in registry
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    import os
    from app.models import FeatureFlag, MCPSecurity

    # Get feature flags
    mcp_flag = db.query(FeatureFlag).filter(FeatureFlag.name == 'mcp_integration').first()
    n8n_flag = db.query(FeatureFlag).filter(FeatureFlag.name == 'n8n_integration').first()

    # Get MCP URL
    mcp_url = os.getenv("N8N_MCP_URL")
    if not mcp_url and mcp_flag and mcp_flag.config:
        mcp_url = mcp_flag.config.get('mcp_url')

    # Get security config
    security = db.query(MCPSecurity).first()

    # Count MCP tools in registry
    mcp_tools_count = db.query(ToolRegistry).filter(
        ToolRegistry.source == 'mcp'
    ).count()

    logger.info("get_mcp_status", user=current_user.username)

    return {
        "mcp_integration": {
            "enabled": mcp_flag.enabled if mcp_flag else False,
            "config": mcp_flag.config if mcp_flag else {}
        },
        "n8n_integration": {
            "enabled": n8n_flag.enabled if n8n_flag else False,
            "config": n8n_flag.config if n8n_flag else {}
        },
        "mcp_url": mcp_url,
        "mcp_tools_count": mcp_tools_count,
        "security": {
            "allowed_domains": security.allowed_domains if security else ["localhost", "127.0.0.1"],
            "blocked_domains": security.blocked_domains if security else [],
            "require_owner_approval": security.require_owner_approval if security else True,
            "max_execution_time_ms": security.max_execution_time_ms if security else 30000,
        }
    }


@router.post("/refresh", response_model=ToolRefreshResult)
async def refresh_tool_registry(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Force a refresh of the orchestrator's tool registry.

    This sends a signal to the orchestrator to reload all tools from:
    1. Static tools (from admin database)
    2. MCP tools (from n8n if enabled)
    3. Legacy tools (from rag_tools.py if fallback enabled)

    Returns refresh statistics.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    import httpx
    import time
    import os

    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")

    logger.info("refresh_tool_registry_started",
               orchestrator_url=orchestrator_url,
               user=current_user.username)

    start_time = time.time()

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{orchestrator_url}/tools/refresh",
                json={}
            )

            if response.status_code == 200:
                data = response.json()
                duration_ms = (time.time() - start_time) * 1000

                logger.info("refresh_tool_registry_success",
                           static_count=data.get('static_count', 0),
                           mcp_count=data.get('mcp_count', 0),
                           legacy_count=data.get('legacy_count', 0),
                           duration_ms=duration_ms,
                           user=current_user.username)

                return ToolRefreshResult(
                    success=True,
                    static_count=data.get('static_count', 0),
                    mcp_count=data.get('mcp_count', 0),
                    legacy_count=data.get('legacy_count', 0),
                    total_unique=data.get('total_unique', 0),
                    duration_ms=duration_ms
                )
            else:
                duration_ms = (time.time() - start_time) * 1000
                logger.warning("refresh_tool_registry_failed",
                              status_code=response.status_code,
                              user=current_user.username)

                raise HTTPException(
                    status_code=502,
                    detail=f"Orchestrator returned {response.status_code}"
                )

    except httpx.HTTPError as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error("refresh_tool_registry_error",
                    error=str(e),
                    orchestrator_url=orchestrator_url,
                    user=current_user.username)

        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to orchestrator: {str(e)}"
        )
