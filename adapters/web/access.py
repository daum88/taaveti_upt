"""Access-control dependencies for web adapters."""

from fastapi import HTTPException, Request


def require_local_operator(request: Request) -> None:
    """Reject operator actions originating outside the local host."""
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="Operator actions are available only from the local server.")
