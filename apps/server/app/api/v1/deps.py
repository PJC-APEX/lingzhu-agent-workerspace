"""HTTP adapters for authentication and thread-execution dependencies."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.application import auth_service
from app.application.thread_execution import acquire_owned_thread_lease
from app.core.identity import current_user_id, reset_current_user, set_current_user
from app.infrastructure.database import get_db, get_session_factory
from app.api.v1.origin import validate_same_origin

SESSION_COOKIE = "lingzhu_session"


def authenticated_user(request: Request, db: Session = Depends(get_db)) -> dict:
    return auth_service.authenticate(db, request.cookies.get(SESSION_COOKIE))


def _authenticate_token(token: str | None) -> dict:
    with get_session_factory()() as db:
        return auth_service.authenticate(db, token)


async def require_user(request: Request):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        validate_same_origin(request)
    user = await run_in_threadpool(_authenticate_token, request.cookies.get(SESSION_COOKIE))
    context_token = set_current_user(user["id"])
    request.state.user = user
    try:
        yield user
    finally:
        reset_current_user(context_token)


def require_thread_lease(thread_id: str):
    lease = acquire_owned_thread_lease(thread_id, current_user_id() or "")
    try:
        yield lease
    finally:
        lease.release()

__all__ = [
    "current_user_id",
    "get_db",
    "require_thread_lease",
    "require_user",
    "authenticated_user",
]
