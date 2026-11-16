"""Execution-local identity, set/reset by entry-point adapters."""
from contextvars import ContextVar

_current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)


def current_user_id() -> str | None:
    return _current_user_id.get()


def set_current_user(user_id: str):
    return _current_user_id.set(user_id)


def reset_current_user(token) -> None:
    _current_user_id.reset(token)
