"""
RAG Service Runner
Starts a FastAPI service that loads its identity from environment
and serves RAG queries using database configuration
"""

import os
import sys
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import structlog
from contextlib import asynccontextmanager

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag.base_rag_service import BaseRAGService

logger = structlog.get_logger()


class RAGQuery(BaseModel):
    query: str
    intent: Optional[str] = None
    user_mode: Optional[str] = "owner"
    context: Optional[Dict[str, Any]] = None


class RAGResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    formatted: Optional[str] = None
    error: Optional[str] = None
    cached: bool = False
    service: str


# Get service name from environment or command line
SERVICE_NAME = os.getenv('RAG_SERVICE_NAME')
if not SERVICE_NAME and len(sys.argv) > 1:
    SERVICE_NAME = sys.argv[1]

if not SERVICE_NAME:
    print("Error: RAG_SERVICE_NAME environment variable or command line argument required")
    print("Usage: RAG_SERVICE_NAME=weather python -m uvicorn service:app")
    print("   or: python service.py weather")
    sys.exit(1)

# Create service instance
rag_service = BaseRAGService(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage service lifecycle"""
    # Startup
    logger.info(f"Starting RAG service: {SERVICE_NAME}")
    await rag_service.initialize()
    yield
    # Shutdown
    logger.info(f"Shutting down RAG service: {SERVICE_NAME}")
    await rag_service.close()


# Create FastAPI app
app = FastAPI(
    title=f"RAG Service - {SERVICE_NAME}",
    description=f"Retrieval-Augmented Generation service for {SERVICE_NAME}",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    is_healthy = await rag_service.health_check()
    status = "healthy" if is_healthy else "unhealthy"

    return {
        "status": status,
        "service": SERVICE_NAME,
        "configured": bool(rag_service.config),
        "templates": len(rag_service.templates),
        "cache_ttl": rag_service.config.get('cache_ttl', 0)
    }


@app.post("/query", response_model=RAGResponse)
async def query_service(request: RAGQuery):
    """Query the RAG service"""
    try:
        result = await rag_service.query(
            query=request.query,
            intent=request.intent,
            user_mode=request.user_mode,
            context=request.context or {}
        )

        return RAGResponse(**result)

    except Exception as e:
        logger.error(f"Query error in {SERVICE_NAME}: {e}")
        return RAGResponse(
            success=False,
            error=str(e),
            service=SERVICE_NAME
        )


@app.get("/config")
async def get_configuration():
    """Get current service configuration (for debugging)"""
    return {
        "service": SERVICE_NAME,
        "config": {
            "endpoint": rag_service.config.get('endpoint_url'),
            "cache_ttl": rag_service.config.get('cache_ttl'),
            "timeout": rag_service.config.get('timeout'),
            "enabled": rag_service.config.get('enabled')
        },
        "templates": len(rag_service.templates),
        "parameters": [
            {
                "name": p.get('param_name'),
                "type": p.get('param_type'),
                "required": p.get('required')
            }
            for p in rag_service.parameters
        ]
    }


@app.post("/refresh")
async def refresh_configuration():
    """Force refresh configuration from database"""
    try:
        await rag_service.load_configuration()
        return {
            "success": True,
            "message": f"Configuration refreshed for {SERVICE_NAME}",
            "templates": len(rag_service.templates)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """Root endpoint with service info"""
    return {
        "service": f"RAG Service - {SERVICE_NAME}",
        "version": "1.0.0",
        "endpoints": [
            "/health - Health check",
            "/query - Query the service (POST)",
            "/config - View configuration",
            "/refresh - Refresh configuration (POST)",
            "/docs - API documentation"
        ]
    }


# Error handlers
@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Unhandled exception in {SERVICE_NAME}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "service": SERVICE_NAME
        }
    )


if __name__ == "__main__":
    import uvicorn

    # Get port from environment or use service-specific default
    port_map = {
        "weather": 8010,
        "airports": 8011,
        "sports": 8012,
        "events": 8013,
        "streaming": 8014,
        "news": 8015,
        "stocks": 8016,
        "flights": 8017
    }

    port = int(os.getenv('PORT', port_map.get(SERVICE_NAME, 8010)))

    uvicorn.run(
        "service:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv('DEBUG', 'false').lower() == 'true'
    )