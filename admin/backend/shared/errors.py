"""
Unified Error Handling for Project Athena

Standard error response models and exception classes for consistent
error handling across all services.

Usage:
    from shared.errors import (
        register_exception_handlers,
        BadRequestError,
        RateLimitError,
        ServiceUnavailableError,
        UpstreamError
    )

    # In FastAPI app setup
    register_exception_handlers(app)

    # In route handlers
    if not valid:
        raise BadRequestError("Invalid input", detail="Field 'query' is required")

    if rate_limit_exceeded:
        raise RateLimitError()

    if service_down:
        raise ServiceUnavailableError("Orchestrator unavailable")
"""
from typing import Optional, Any, Dict, List
from pydantic import BaseModel
from fastapi import Request
from fastapi.responses import JSONResponse
from enum import Enum
from datetime import datetime
import structlog
import traceback

logger = structlog.get_logger()


class ErrorCode(str, Enum):
    """Standard error codes across all Project Athena services."""

    # Client errors (4xx)
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFLICT = "CONFLICT"

    # Server errors (5xx)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"

    # Domain-specific errors
    LLM_ERROR = "LLM_ERROR"
    RAG_ERROR = "RAG_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    CLASSIFICATION_ERROR = "CLASSIFICATION_ERROR"
    SYNTHESIS_ERROR = "SYNTHESIS_ERROR"


class ErrorResponse(BaseModel):
    """
    Standard error response format for all Project Athena APIs.

    Example response:
    {
        "error": true,
        "code": "RATE_LIMITED",
        "message": "Rate limit exceeded",
        "detail": "Maximum 60 requests per minute allowed",
        "request_id": "abc123",
        "timestamp": "2025-12-01T10:30:00Z"
    }
    """
    error: bool = True
    code: str
    message: str
    detail: Optional[str] = None
    request_id: Optional[str] = None
    timestamp: Optional[str] = None

    class Config:
        use_enum_values = True


class PartialSuccessResponse(BaseModel):
    """
    Response format for operations with partial success.

    Useful for batch operations where some items succeed and others fail.

    Example:
    {
        "success": true,
        "data": {"processed": 8},
        "errors": [
            {"item": "item_9", "code": "VALIDATION_ERROR", "message": "Invalid format"},
            {"item": "item_10", "code": "NOT_FOUND", "message": "Resource not found"}
        ],
        "request_id": "abc123"
    }
    """
    success: bool
    data: Optional[Any] = None
    errors: Optional[List[Dict[str, Any]]] = None
    request_id: Optional[str] = None


class AthenaException(Exception):
    """
    Base exception class for all Project Athena services.

    All custom exceptions should inherit from this class.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 500,
        detail: Optional[str] = None
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


# ==============================================================================
# Client Errors (4xx)
# ==============================================================================

class BadRequestError(AthenaException):
    """400 Bad Request - Invalid input or request format."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.BAD_REQUEST, message, 400, detail)


class UnauthorizedError(AthenaException):
    """401 Unauthorized - Authentication required."""
    def __init__(self, message: str = "Authentication required", detail: Optional[str] = None):
        super().__init__(ErrorCode.UNAUTHORIZED, message, 401, detail)


class ForbiddenError(AthenaException):
    """403 Forbidden - Insufficient permissions."""
    def __init__(self, message: str = "Access denied", detail: Optional[str] = None):
        super().__init__(ErrorCode.FORBIDDEN, message, 403, detail)


class NotFoundError(AthenaException):
    """404 Not Found - Resource doesn't exist."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.NOT_FOUND, message, 404, detail)


class RateLimitError(AthenaException):
    """429 Too Many Requests - Rate limit exceeded."""
    def __init__(self, message: str = "Rate limit exceeded", detail: Optional[str] = None):
        super().__init__(ErrorCode.RATE_LIMITED, message, 429, detail)


class ValidationError(AthenaException):
    """422 Validation Error - Request validation failed."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.VALIDATION_ERROR, message, 422, detail)


class ConflictError(AthenaException):
    """409 Conflict - Resource already exists or conflicting state."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.CONFLICT, message, 409, detail)


# ==============================================================================
# Server Errors (5xx)
# ==============================================================================

class InternalError(AthenaException):
    """500 Internal Server Error - Unexpected error occurred."""
    def __init__(self, message: str = "An unexpected error occurred", detail: Optional[str] = None):
        super().__init__(ErrorCode.INTERNAL_ERROR, message, 500, detail)


class ServiceUnavailableError(AthenaException):
    """503 Service Unavailable - Service temporarily unavailable."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.SERVICE_UNAVAILABLE, message, 503, detail)


class TimeoutError(AthenaException):
    """504 Gateway Timeout - Request timed out."""
    def __init__(self, message: str = "Request timed out", detail: Optional[str] = None):
        super().__init__(ErrorCode.TIMEOUT, message, 504, detail)


class UpstreamError(AthenaException):
    """502 Bad Gateway - Upstream service failed."""
    def __init__(self, service: str, detail: Optional[str] = None):
        super().__init__(
            ErrorCode.UPSTREAM_ERROR,
            f"Upstream service '{service}' failed",
            502,
            detail
        )
        self.service = service


# ==============================================================================
# Domain-Specific Errors
# ==============================================================================

class LLMError(AthenaException):
    """500 LLM Error - LLM processing failed."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.LLM_ERROR, message, 500, detail)


class RAGError(AthenaException):
    """500 RAG Error - RAG service processing failed."""
    def __init__(self, message: str, service: Optional[str] = None, detail: Optional[str] = None):
        super().__init__(ErrorCode.RAG_ERROR, message, 500, detail)
        self.service = service


class ToolError(AthenaException):
    """500 Tool Error - Tool execution failed."""
    def __init__(self, message: str, tool: Optional[str] = None, detail: Optional[str] = None):
        super().__init__(ErrorCode.TOOL_ERROR, message, 500, detail)
        self.tool = tool


class ConfigError(AthenaException):
    """500 Config Error - Configuration loading/parsing failed."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.CONFIG_ERROR, message, 500, detail)


class ClassificationError(AthenaException):
    """500 Classification Error - Intent classification failed."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.CLASSIFICATION_ERROR, message, 500, detail)


class SynthesisError(AthenaException):
    """500 Synthesis Error - Response synthesis failed."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(ErrorCode.SYNTHESIS_ERROR, message, 500, detail)


# ==============================================================================
# Exception Handlers
# ==============================================================================

async def athena_exception_handler(request: Request, exc: AthenaException) -> JSONResponse:
    """
    FastAPI exception handler for AthenaException and subclasses.

    Logs the error and returns a standardized ErrorResponse.
    """
    request_id = getattr(request.state, "request_id", None)

    logger.error(
        "athena_exception",
        code=exc.code.value if isinstance(exc.code, ErrorCode) else exc.code,
        message=exc.message,
        status_code=exc.status_code,
        detail=exc.detail,
        request_id=request_id,
        path=str(request.url.path)
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            code=exc.code.value if isinstance(exc.code, ErrorCode) else exc.code,
            message=exc.message,
            detail=exc.detail,
            request_id=request_id,
            timestamp=datetime.utcnow().isoformat() + "Z"
        ).dict()
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all exception handler for unhandled exceptions.

    Logs the full traceback and returns a generic error response.
    Does not expose internal details to clients for security.
    """
    request_id = getattr(request.state, "request_id", None)

    logger.error(
        "unhandled_exception",
        error=str(exc),
        error_type=type(exc).__name__,
        request_id=request_id,
        path=str(request.url.path),
        traceback=traceback.format_exc()
    )

    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            code=ErrorCode.INTERNAL_ERROR.value,
            message="An unexpected error occurred",
            detail=None,  # Don't expose internal details
            request_id=request_id,
            timestamp=datetime.utcnow().isoformat() + "Z"
        ).dict()
    )


def register_exception_handlers(app) -> None:
    """
    Register exception handlers with a FastAPI application.

    Usage:
        from shared.errors import register_exception_handlers

        app = FastAPI()
        register_exception_handlers(app)
    """
    app.add_exception_handler(AthenaException, athena_exception_handler)
    # Note: Registering a generic Exception handler can interfere with FastAPI's
    # built-in validation error handling. Use with caution.
    # app.add_exception_handler(Exception, generic_exception_handler)

    logger.info("athena_exception_handlers_registered")


def create_error_response(
    code: ErrorCode,
    message: str,
    detail: Optional[str] = None,
    request_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create an error response dictionary.

    Useful for cases where you need to return an error response
    without raising an exception.

    Usage:
        return create_error_response(
            ErrorCode.BAD_REQUEST,
            "Invalid query",
            detail="Query cannot be empty"
        )
    """
    return ErrorResponse(
        code=code.value if isinstance(code, ErrorCode) else code,
        message=message,
        detail=detail,
        request_id=request_id,
        timestamp=datetime.utcnow().isoformat() + "Z"
    ).dict()
