from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.repositories import thread_lease_repo, agent_repo
from app.domain.exceptions import NotFoundError, ConflictError
from app.infrastructure.database import get_session_factory

logger = logging.getLogger(__name__)

LEASE_SECONDS = 900
HEARTBEAT_SECONDS = 30


def acquire_owned_thread_lease(thread_id: str, user_id: str) -> ThreadExecutionLease:
    lease = ThreadExecutionLease(thread_id)
    with get_session_factory()() as db:
        if not user_id or agent_repo.get_owned_thread(db, thread_id, user_id) is None:
            raise NotFoundError("找不到智能体会话")
        if not lease.acquire(db):
            raise ConflictError("该对话正在处理中，请稍后重试")
    return lease


class ThreadExecutionLease:
    """A short-transaction lease serializes checkpoint writes without holding DB locks during model calls."""

    def __init__(self, thread_id: str, token: str | None = None):
        self.thread_id = thread_id
        self.token = token or str(uuid4())
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def acquire(self, db: Session) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=LEASE_SECONDS)
        # One conditional UPDATE is the claim; model calls happen only after this short transaction commits.
        claimed = thread_lease_repo.claim(db, self.thread_id, self.token, now, expires_at)
        db.commit()
        if claimed != 1:
            logger.info("event=agent.thread_lease_busy thread_id=%s", self.thread_id)
            return False
        logger.info("event=agent.thread_lease_acquired thread_id=%s", self.thread_id)
        self._heartbeat = threading.Thread(
            target=self._renew_loop,
            name=f"thread-lease-{self.thread_id[:8]}",
            daemon=True,
        )
        self._heartbeat.start()
        return True

    def _renew_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                with get_session_factory()() as db:
                    updated = thread_lease_repo.renew(db, self.thread_id, self.token, datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS))
                    db.commit()
                if updated != 1:
                    logger.error("event=agent.thread_lease_lost thread_id=%s", self.thread_id)
                    return
            except Exception:
                logger.exception("event=agent.thread_lease_renew_failed thread_id=%s", self.thread_id)

    def release(self, db: Session | None = None) -> None:
        self._stop.set()
        if self._heartbeat and self._heartbeat is not threading.current_thread():
            self._heartbeat.join(timeout=1)
        try:
            def clear_lease(session: Session) -> None:
                thread_lease_repo.release(session, self.thread_id, self.token)
                session.commit()

            if db is not None:
                clear_lease(db)
            else:
                with get_session_factory()() as session:
                    clear_lease(session)
            logger.info("event=agent.thread_lease_released thread_id=%s", self.thread_id)
        except Exception:
            logger.exception("event=agent.thread_lease_release_failed thread_id=%s", self.thread_id)


def acquire_thread_lease(thread_id: str, wait_seconds: float = 60) -> ThreadExecutionLease:
    """Worker checkpoint resumes may wait briefly for the active API turn to release its lease."""
    lease = ThreadExecutionLease(thread_id)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        with get_session_factory()() as db:
            if lease.acquire(db):
                return lease
        time.sleep(0.25)
    raise RuntimeError("智能体对话仍在处理中，后台恢复将在后续重试")
