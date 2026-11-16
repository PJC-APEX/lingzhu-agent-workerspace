"""Atomic thread lease writes; application owns short transactions."""
from sqlalchemy import or_, update
from app.domain.models import AgentThread


def claim(db, thread_id, token, now, expires_at) -> int:
    return db.execute(update(AgentThread).where(
        AgentThread.id == thread_id,
        or_(AgentThread.execution_token.is_(None), AgentThread.execution_lease_until < now,
            AgentThread.execution_token == token),
    ).values(execution_token=token, execution_lease_until=expires_at)
        .execution_options(synchronize_session=False)).rowcount


def renew(db, thread_id, token, expires_at) -> int:
    return db.execute(update(AgentThread).where(
        AgentThread.id == thread_id, AgentThread.execution_token == token,
    ).values(execution_lease_until=expires_at).execution_options(synchronize_session=False)).rowcount


def release(db, thread_id, token) -> None:
    db.execute(update(AgentThread).where(
        AgentThread.id == thread_id, AgentThread.execution_token == token,
    ).values(execution_token=None, execution_lease_until=None).execution_options(synchronize_session=False))
