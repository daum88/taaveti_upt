"""Access-control dependency for operator-only web actions."""

from __future__ import annotations

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from settings import is_loopback_host


def require_operator(request: Request) -> None:
    """Allow loopback-bound local actions or a configured bearer-token holder."""
    settings = request.app.state.settings
    if settings.allow_insecure_non_loopback:
        return

    remote_host = request.client.host if request.client else None
    if is_loopback_host(settings.server_host) and (
        remote_host == "testclient" or (remote_host and is_loopback_host(remote_host))
    ):
        return
    if settings.operator_token and compare_digest(_bearer_token(request), settings.operator_token):
        return
    if settings.operator_token:
        raise HTTPException(
            status_code=401,
            detail="A valid operator token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(status_code=403, detail="Operator actions are available only from the local server.")


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" else ""


OperatorAccess = Annotated[None, Depends(require_operator)]
