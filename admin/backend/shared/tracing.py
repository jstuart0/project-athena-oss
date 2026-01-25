"""
Request Tracing Middleware for Project Athena

Adds correlation IDs to all requests for distributed tracing across services.
Enables tracking a single request as it flows through Gateway -> Orchestrator -> RAG services.

Usage:
    from shared.tracing import RequestTracingMiddleware, get_tracing_headers

    # Add middleware to FastAPI app
    app.add_middleware(RequestTracingMiddleware)

    # When calling downstream services, include tracing headers
    headers = get_tracing_headers(request)
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

Headers:
    X-Request-ID: Unique ID for this request
    X-Parent-Request-ID: ID of the parent request (for nested service calls)
    X-Origin-Service: Name of the service that originated the request chain
"""
import uuid
import time
from typing import Callable, Optional, Dict
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
import structlog

logger = structlog.get_logger()

# Header names
REQUEST_ID_HEADER = "X-Request-ID"
PARENT_REQUEST_ID_HEADER = "X-Parent-Request-ID"
ORIGIN_SERVICE_HEADER = "X-Origin-Service"


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that adds request tracing to all HTTP requests.

    Features:
    - Generates unique request ID if not provided in headers
    - Stores request ID in request.state for use in handlers
    - Binds request ID to structlog context for automatic inclusion in logs
    - Adds request ID to response headers
    - Logs request start/end with timing information
    """

    def __init__(self, app, service_name: str = "athena"):
        super().__init__(app)
        self.service_name = service_name

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get or generate request ID
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        parent_request_id = request.headers.get(PARENT_REQUEST_ID_HEADER)
        origin_service = request.headers.get(ORIGIN_SERVICE_HEADER) or self.service_name

        # Record start time
        start_time = time.time()

        # Store in request state for use in handlers
        request.state.request_id = request_id
        request.state.parent_request_id = parent_request_id
        request.state.origin_service = origin_service
        request.state.start_time = start_time

        # Bind to structlog context for automatic inclusion in all logs
        with structlog.contextvars.bound_contextvars(
            request_id=request_id,
            parent_request_id=parent_request_id,
            origin_service=origin_service,
            path=request.url.path,
            method=request.method,
            service=self.service_name
        ):
            logger.info(
                "request_started",
                client_ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent", "")[:100]
            )

            try:
                response = await call_next(request)

                # Calculate duration
                duration_ms = (time.time() - start_time) * 1000

                # Add tracing headers to response
                response.headers[REQUEST_ID_HEADER] = request_id
                response.headers["X-Response-Time-Ms"] = str(int(duration_ms))

                logger.info(
                    "request_completed",
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2)
                )

                return response

            except Exception as e:
                # Calculate duration
                duration_ms = (time.time() - start_time) * 1000

                logger.error(
                    "request_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    duration_ms=round(duration_ms, 2)
                )
                raise


def get_request_id(request: Request) -> str:
    """
    Get request ID from request state.

    Usage:
        @app.get("/example")
        async def example(request: Request):
            request_id = get_request_id(request)
            logger.info("Processing", request_id=request_id)
    """
    return getattr(request.state, "request_id", "unknown")


def get_parent_request_id(request: Request) -> Optional[str]:
    """Get parent request ID from request state."""
    return getattr(request.state, "parent_request_id", None)


def get_tracing_headers(request: Request, service_name: Optional[str] = None) -> Dict[str, str]:
    """
    Get headers to pass to downstream services for request tracing.

    When making HTTP calls to other services, include these headers
    to propagate the trace context.

    Usage:
        # In a route handler
        headers = get_tracing_headers(request, service_name="orchestrator")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://rag-service:8010/weather",
                json=data,
                headers=headers
            )

    Args:
        request: The FastAPI Request object
        service_name: Name of the current service (for X-Origin-Service)

    Returns:
        Dict with tracing headers to pass to downstream service
    """
    current_request_id = getattr(request.state, "request_id", None)
    origin_service = getattr(request.state, "origin_service", service_name)

    headers = {}

    if current_request_id:
        # The downstream service gets a new request ID
        headers[REQUEST_ID_HEADER] = str(uuid.uuid4())
        # But knows about its parent
        headers[PARENT_REQUEST_ID_HEADER] = current_request_id

    if origin_service:
        headers[ORIGIN_SERVICE_HEADER] = origin_service

    return headers


def get_tracing_headers_simple(
    request_id: Optional[str] = None,
    parent_request_id: Optional[str] = None,
    origin_service: Optional[str] = None
) -> Dict[str, str]:
    """
    Get tracing headers without requiring a Request object.

    Useful when you have the request ID but not the full Request object.

    Usage:
        headers = get_tracing_headers_simple(
            request_id="abc-123",
            origin_service="gateway"
        )
    """
    headers = {}

    if request_id:
        headers[REQUEST_ID_HEADER] = str(uuid.uuid4())
        headers[PARENT_REQUEST_ID_HEADER] = request_id

    if parent_request_id and REQUEST_ID_HEADER not in headers:
        headers[PARENT_REQUEST_ID_HEADER] = parent_request_id

    if origin_service:
        headers[ORIGIN_SERVICE_HEADER] = origin_service

    return headers


def create_request_context(
    request_id: Optional[str] = None,
    parent_request_id: Optional[str] = None,
    origin_service: Optional[str] = None
) -> Dict[str, str]:
    """
    Create a request context dict for use outside of HTTP handlers.

    Useful for background tasks or queue consumers that need tracing context.

    Usage:
        context = create_request_context(origin_service="worker")
        with structlog.contextvars.bound_contextvars(**context):
            await process_task()
    """
    return {
        "request_id": request_id or str(uuid.uuid4()),
        "parent_request_id": parent_request_id,
        "origin_service": origin_service or "background"
    }


class TracingContext:
    """
    Context manager for adding tracing to non-HTTP code.

    Usage:
        async def background_task():
            with TracingContext(origin_service="scheduler"):
                await do_work()
    """

    def __init__(
        self,
        request_id: Optional[str] = None,
        parent_request_id: Optional[str] = None,
        origin_service: str = "background"
    ):
        self.request_id = request_id or str(uuid.uuid4())
        self.parent_request_id = parent_request_id
        self.origin_service = origin_service
        self._token = None

    def __enter__(self):
        self._token = structlog.contextvars.bind_contextvars(
            request_id=self.request_id,
            parent_request_id=self.parent_request_id,
            origin_service=self.origin_service
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token:
            structlog.contextvars.unbind_contextvars(
                "request_id", "parent_request_id", "origin_service"
            )
        return False


def extract_trace_info(headers: Dict[str, str]) -> Dict[str, Optional[str]]:
    """
    Extract tracing information from request headers.

    Useful for logging or debugging incoming requests.

    Returns:
        Dict with request_id, parent_request_id, and origin_service
    """
    return {
        "request_id": headers.get(REQUEST_ID_HEADER),
        "parent_request_id": headers.get(PARENT_REQUEST_ID_HEADER),
        "origin_service": headers.get(ORIGIN_SERVICE_HEADER)
    }
