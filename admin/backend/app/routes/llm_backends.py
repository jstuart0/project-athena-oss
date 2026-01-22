"""
LLM Backend Management API Routes.

Provides CRUD operations for LLM backend configuration to enable
per-model backend selection (Ollama, MLX, Auto) with performance tracking.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, LLMBackend, LLMPerformanceMetric
from datetime import datetime

logger = structlog.get_logger()

router = APIRouter(prefix="/api/llm-backends", tags=["llm-backends"])


# Pydantic models for request/response
class LLMBackendCreate(BaseModel):
    """Request model for creating LLM backend config."""
    model_name: str = Field(..., description="Model identifier (e.g., 'phi3:mini', 'llama3.1:8b')")
    backend_type: str = Field(..., description="Backend type: 'ollama', 'mlx', or 'auto'")
    endpoint_url: str = Field(..., description="Backend endpoint URL (e.g., 'http://localhost:11434')")
    enabled: bool = Field(default=True, description="Whether this backend is enabled")
    priority: int = Field(default=100, description="Priority for 'auto' mode (lower = higher priority)")
    max_tokens: int = Field(default=2048, description="Maximum tokens to generate")
    temperature_default: float = Field(default=0.7, description="Default temperature for generation")
    timeout_seconds: int = Field(default=60, description="Request timeout in seconds")
    keep_alive_seconds: int = Field(default=-1, description="How long to keep model loaded. -1 = forever, 0 = unload immediately, >0 = seconds")
    description: Optional[str] = Field(None, description="Optional description of this backend configuration")

    class Config:
        json_schema_extra = {
            "example": {
                "model_name": "phi3:mini",
                "backend_type": "ollama",
                "endpoint_url": "http://localhost:11434",
                "enabled": True,
                "priority": 100,
                "max_tokens": 2048,
                "temperature_default": 0.7,
                "timeout_seconds": 60,
                "keep_alive_seconds": -1,
                "description": "Phi-3 Mini via Ollama for fast classification"
            }
        }


class LLMBackendUpdate(BaseModel):
    """Request model for updating LLM backend config."""
    backend_type: Optional[str] = None
    endpoint_url: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    max_tokens: Optional[int] = None
    temperature_default: Optional[float] = None
    timeout_seconds: Optional[int] = None
    keep_alive_seconds: Optional[int] = None
    description: Optional[str] = None


class LLMBackendResponse(BaseModel):
    """Response model for LLM backend config."""
    id: int
    model_name: str
    backend_type: str
    endpoint_url: str
    enabled: bool
    priority: int
    avg_tokens_per_sec: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    total_requests: Optional[int] = 0
    total_errors: Optional[int] = 0
    max_tokens: Optional[int] = None
    temperature_default: Optional[float] = None
    timeout_seconds: Optional[int] = None
    keep_alive_seconds: Optional[int] = -1
    description: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class LLMMetricCreate(BaseModel):
    """Request model for creating LLM performance metric."""
    timestamp: float = Field(..., description="Unix timestamp of request start")
    model: str = Field(..., description="Model name used for generation")
    backend: str = Field(..., description="Backend type (ollama, mlx, auto)")
    latency_seconds: float = Field(..., description="Total request latency in seconds")
    tokens: int = Field(..., description="Number of tokens generated")
    tokens_per_second: float = Field(..., description="Token generation speed")
    request_id: Optional[str] = Field(None, description="Optional request ID for tracking")
    session_id: Optional[str] = Field(None, description="Optional session ID for conversation tracking")
    user_id: Optional[str] = Field(None, description="Optional user ID")
    zone: Optional[str] = Field(None, description="Optional zone/location")
    intent: Optional[str] = Field(None, description="Optional intent classification")
    source: Optional[str] = Field(None, description="Source of the request (admin_voice_test, gateway, orchestrator, rag_*)")
    stage: Optional[str] = Field(None, description="Pipeline stage (classify, summarize, tool_selection, validation, synthesize, etc.)")

    class Config:
        json_schema_extra = {
            "example": {
                "timestamp": 1700000000.123,
                "model": "phi3:mini",
                "backend": "ollama",
                "latency_seconds": 2.5,
                "tokens": 150,
                "tokens_per_second": 60.0,
                "request_id": "req_123abc",
                "session_id": "sess_xyz789",
                "intent": "weather_query",
                "source": "gateway",
                "stage": "synthesize"
            }
        }


# API Routes

# Public endpoint (no auth) for services to query LLM backends
@router.get("/public", response_model=List[LLMBackendResponse])
async def list_backends_public(
    enabled_only: bool = False,
    db: Session = Depends(get_db)
):
    """
    List all LLM backend configurations (public endpoint, no auth required).

    This endpoint is used by services (Gateway, Orchestrator, etc.) to check
    LLM backend configuration without requiring authentication.

    Query params:
    - enabled_only: If true, only return enabled backends

    Returns:
        List of LLM backends with their configuration sorted by priority
    """
    logger.info("list_llm_backends_public", enabled_only=enabled_only, source="public")

    query = db.query(LLMBackend)
    if enabled_only:
        query = query.filter(LLMBackend.enabled == True)

    backends = query.order_by(LLMBackend.priority, LLMBackend.model_name).all()

    return [
        LLMBackendResponse(
            **backend.to_dict()
        ) for backend in backends
    ]


# Public endpoint for MLX applicability data
@router.get("/public/mlx-applicability")
async def get_mlx_applicability(db: Session = Depends(get_db)):
    """
    Get MLX applicability data for components and models.

    Returns information about:
    - Which models have MLX backend configured
    - Which components are currently using each model
    - Whether the mlx_backend feature flag is enabled
    - Summary of MLX usage across the system

    This is a public endpoint for use by frontend and services.
    """
    from app.models import Feature, ComponentModelAssignment, GatewayConfig

    # Get mlx_backend feature flag status
    mlx_feature = db.query(Feature).filter(Feature.name == "mlx_backend").first()
    mlx_enabled = mlx_feature.enabled if mlx_feature else False
    mlx_latency_impact = mlx_feature.avg_latency_ms if mlx_feature else 0

    # Get all backends with their backend_type
    all_backends = db.query(LLMBackend).all()

    # Identify MLX-capable models (backend_type is 'mlx' or 'auto', or model name contains 'mlx')
    mlx_models = []
    ollama_models = []
    for backend in all_backends:
        model_info = {
            "model_name": backend.model_name,
            "backend_type": backend.backend_type,
            "enabled": backend.enabled,
            "endpoint_url": backend.endpoint_url
        }
        if backend.backend_type in ('mlx', 'auto') or 'mlx' in backend.model_name.lower():
            mlx_models.append(model_info)
        else:
            ollama_models.append(model_info)

    # Get component model assignments
    component_assignments = db.query(ComponentModelAssignment).filter(
        ComponentModelAssignment.enabled == True
    ).all()

    # Get gateway config for intent model
    gateway_config = db.query(GatewayConfig).first()
    gateway_intent_model = gateway_config.intent_model if gateway_config else None

    # Build component-to-model mapping
    components_using_models = {}

    # Add gateway intent
    if gateway_intent_model:
        if gateway_intent_model not in components_using_models:
            components_using_models[gateway_intent_model] = []
        components_using_models[gateway_intent_model].append({
            "component_name": "gateway_intent",
            "display_name": "Gateway Intent Classification",
            "category": "gateway"
        })

    # Add component assignments
    for comp in component_assignments:
        if comp.model_name not in components_using_models:
            components_using_models[comp.model_name] = []
        components_using_models[comp.model_name].append({
            "component_name": comp.component_name,
            "display_name": comp.display_name,
            "category": comp.category
        })

    # Check which components could use MLX (have MLX alternatives)
    mlx_model_names = {m["model_name"] for m in mlx_models}
    components_with_mlx_alternative = []
    components_without_mlx = []

    all_component_models = set(components_using_models.keys())

    for model_name, components in components_using_models.items():
        has_mlx = model_name in mlx_model_names or 'mlx' in model_name.lower()
        for comp in components:
            comp_info = {
                **comp,
                "current_model": model_name,
                "has_mlx_alternative": has_mlx
            }
            if has_mlx:
                components_with_mlx_alternative.append(comp_info)
            else:
                components_without_mlx.append(comp_info)

    # Calculate summary stats
    total_components = len(components_with_mlx_alternative) + len(components_without_mlx)
    components_using_mlx = len(components_with_mlx_alternative)

    return {
        "mlx_feature_enabled": mlx_enabled,
        "mlx_latency_impact_ms": mlx_latency_impact,
        "summary": {
            "total_components": total_components,
            "components_using_mlx": components_using_mlx,
            "components_not_using_mlx": len(components_without_mlx),
            "mlx_models_available": len(mlx_models),
            "ollama_models_configured": len(ollama_models),
            "mlx_utilization_percent": round(components_using_mlx / total_components * 100, 1) if total_components > 0 else 0
        },
        "mlx_models": mlx_models,
        "ollama_models": ollama_models,
        "components_with_mlx": components_with_mlx_alternative,
        "components_without_mlx": components_without_mlx,
        "model_usage": components_using_models
    }


@router.get("", response_model=List[LLMBackendResponse])
async def list_backends(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all LLM backend configurations (authenticated endpoint).

    Query params:
    - enabled_only: If true, only return enabled backends
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_llm_backends", user=current_user.username, enabled_only=enabled_only)

    query = db.query(LLMBackend)
    if enabled_only:
        query = query.filter(LLMBackend.enabled == True)

    backends = query.order_by(LLMBackend.model_name).all()

    return [
        LLMBackendResponse(
            **backend.to_dict()
        ) for backend in backends
    ]


class LLMMetricResponse(BaseModel):
    """Response model for LLM performance metric."""
    id: int
    timestamp: str
    model: str
    backend: str
    latency_seconds: float
    tokens_generated: int
    tokens_per_second: float
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    zone: Optional[str] = None
    intent: Optional[str] = None
    source: Optional[str] = None
    stage: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/metrics", response_model=List[LLMMetricResponse])
async def get_metrics(
    model: Optional[str] = None,
    backend: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve LLM performance metrics.

    Query Parameters:
    - model: Filter by model name (optional)
    - backend: Filter by backend type (optional)
    - limit: Maximum number of metrics to return (default: 100, max: 1000)

    Returns:
        List of performance metrics ordered by timestamp (newest first)
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Limit maximum to 1000 records
    if limit > 1000:
        limit = 1000

    query = db.query(LLMPerformanceMetric)

    if model:
        query = query.filter(LLMPerformanceMetric.model == model)
    if backend:
        query = query.filter(LLMPerformanceMetric.backend == backend)

    metrics = query.order_by(
        LLMPerformanceMetric.timestamp.desc()
    ).limit(limit).all()

    logger.info(
        "llm_metrics_retrieved",
        user=current_user.username,
        count=len(metrics),
        filters={"model": model, "backend": backend, "limit": limit}
    )

    return [
        LLMMetricResponse(
            id=m.id,
            timestamp=m.timestamp.isoformat(),
            model=m.model,
            backend=m.backend,
            latency_seconds=m.latency_seconds,
            tokens_generated=m.tokens_generated,
            tokens_per_second=m.tokens_per_second,
            request_id=m.request_id,
            session_id=m.session_id,
            user_id=m.user_id,
            zone=m.zone,
            intent=m.intent,
            source=m.source,
            stage=m.stage
        ) for m in metrics
    ]


@router.post("/metrics", status_code=201)
async def create_metric(
    metric: LLMMetricCreate,
    db: Session = Depends(get_db)
):
    """
    Store LLM performance metric in database.

    This endpoint is called internally by the LLM Router to persist metrics.
    No authentication required for internal service-to-service calls.

    Returns:
        201: Metric created successfully
        500: Database error
    """
    try:
        db_metric = LLMPerformanceMetric(
            timestamp=datetime.fromtimestamp(metric.timestamp),
            model=metric.model,
            backend=metric.backend,
            latency_seconds=metric.latency_seconds,
            tokens_generated=metric.tokens,
            tokens_per_second=metric.tokens_per_second,
            request_id=metric.request_id,
            session_id=metric.session_id,
            user_id=metric.user_id,
            zone=metric.zone,
            intent=metric.intent,
            source=metric.source,
            stage=metric.stage
        )

        db.add(db_metric)
        db.commit()
        db.refresh(db_metric)

        logger.info(
            "llm_metric_persisted",
            metric_id=db_metric.id,
            model=metric.model,
            backend=metric.backend,
            tokens_per_sec=metric.tokens_per_second
        )

        return {"id": db_metric.id, "status": "created"}

    except Exception as e:
        db.rollback()
        logger.error(
            "failed_to_persist_metric",
            error=str(e),
            model=metric.model
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist metric: {str(e)}"
        )

@router.get("/{backend_id}", response_model=LLMBackendResponse)
async def get_backend(
    backend_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get specific LLM backend configuration by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    backend = db.query(LLMBackend).filter(LLMBackend.id == backend_id).first()
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")

    logger.info("get_llm_backend", backend_id=backend_id, user=current_user.username)

    return LLMBackendResponse(**backend.to_dict())


@router.get("/model/{model_name}", response_model=LLMBackendResponse)
async def get_backend_by_model(
    model_name: str,
    db: Session = Depends(get_db)
):
    """
    Get LLM backend configuration for a specific model.

    This endpoint is called by services and does not require authentication
    (uses service-to-service communication).
    """
    backend = db.query(LLMBackend).filter(
        LLMBackend.model_name == model_name,
        LLMBackend.enabled == True
    ).first()

    if not backend:
        logger.warning("backend_not_found", model_name=model_name)
        raise HTTPException(
            status_code=404,
            detail=f"No enabled backend configured for model '{model_name}'"
        )

    logger.debug("get_backend_by_model", model_name=model_name, backend_type=backend.backend_type)

    return LLMBackendResponse(**backend.to_dict())


@router.post("", response_model=LLMBackendResponse, status_code=201)
async def create_backend(
    backend_data: LLMBackendCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create new LLM backend configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate backend_type
    valid_types = ['ollama', 'mlx', 'auto']
    if backend_data.backend_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid backend_type. Must be one of: {', '.join(valid_types)}"
        )

    # Check if model already configured
    existing = db.query(LLMBackend).filter(
        LLMBackend.model_name == backend_data.model_name
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Backend for model '{backend_data.model_name}' already exists"
        )

    backend = LLMBackend(
        model_name=backend_data.model_name,
        backend_type=backend_data.backend_type,
        endpoint_url=backend_data.endpoint_url,
        enabled=backend_data.enabled,
        priority=backend_data.priority,
        max_tokens=backend_data.max_tokens,
        temperature_default=backend_data.temperature_default,
        timeout_seconds=backend_data.timeout_seconds,
        keep_alive_seconds=backend_data.keep_alive_seconds,
        description=backend_data.description,
        created_by_id=current_user.id
    )

    db.add(backend)
    db.commit()
    db.refresh(backend)

    logger.info(
        "created_llm_backend",
        backend_id=backend.id,
        model_name=backend.model_name,
        backend_type=backend.backend_type,
        user=current_user.username
    )

    return LLMBackendResponse(**backend.to_dict())


@router.put("/{backend_id}", response_model=LLMBackendResponse)
async def update_backend(
    backend_id: int,
    backend_data: LLMBackendUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update LLM backend configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    backend = db.query(LLMBackend).filter(LLMBackend.id == backend_id).first()
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")

    # Validate backend_type if provided
    if backend_data.backend_type is not None:
        valid_types = ['ollama', 'mlx', 'auto']
        if backend_data.backend_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid backend_type. Must be one of: {', '.join(valid_types)}"
            )

    # Update fields
    update_data = backend_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(backend, field, value)

    db.commit()
    db.refresh(backend)

    logger.info(
        "updated_llm_backend",
        backend_id=backend_id,
        model_name=backend.model_name,
        updated_fields=list(update_data.keys()),
        user=current_user.username
    )

    return LLMBackendResponse(**backend.to_dict())


@router.delete("/{backend_id}", status_code=204)
async def delete_backend(
    backend_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete LLM backend configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    backend = db.query(LLMBackend).filter(LLMBackend.id == backend_id).first()
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")

    model_name = backend.model_name
    db.delete(backend)
    db.commit()

    logger.info(
        "deleted_llm_backend",
        backend_id=backend_id,
        model_name=model_name,
        user=current_user.username
    )

    return None


@router.post("/{backend_id}/toggle", response_model=LLMBackendResponse)
async def toggle_backend(
    backend_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle enabled/disabled status of an LLM backend."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    backend = db.query(LLMBackend).filter(LLMBackend.id == backend_id).first()
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")

    backend.enabled = not backend.enabled
    db.commit()
    db.refresh(backend)

    logger.info(
        "toggled_llm_backend",
        backend_id=backend_id,
        model_name=backend.model_name,
        enabled=backend.enabled,
        user=current_user.username
    )

    return LLMBackendResponse(**backend.to_dict())


