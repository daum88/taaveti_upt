"""HTTP error mapping for web adapters."""

from fastapi.responses import JSONResponse

from services.agent_service import ServiceError


def service_error_response(error: ServiceError) -> JSONResponse:
    """Map an agent-service error to its declared HTTP response."""
    return JSONResponse(error.to_payload(), status_code=error.status_code)
