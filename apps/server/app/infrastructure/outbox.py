"""任务 outbox 的认领与投递状态机。

为什么需要 outbox：
「把任务写进数据库」和「把任务丢进队列」是两个不同的系统，无法同事务完成。
如果先提交数据库再投队列，进程在这中间挂掉，任务就永远躺在库里没人跑；
如果先投队列再提交数据库，worker 可能先拿到消息、却查不到任务。

outbox 的做法：在写业务数据的同一个事务里，额外写一条「待投递」意图。
事务提交后立刻尝试投递；万一投递失败或进程挂掉，定时扫描器会把这条
意图捞出来重投。于是「至少投递一次」有了保证。

这一层只做状态机与并发认领，不知道任务是什么、也不知道队列是 Redis 还是别的。
「真正投递」这个动作由调用方以 `publisher` 参数传入。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import JOB_OUTBOX_LOCK_SECONDS
from app.domain.models import Job, JobOutboxEvent

logger = logging.getLogger(__name__)

__all__ = [
    "claim_and_publish",
    "list_pending_event_ids",
    "PublishFn",
]

# publisher(job_id, generation) -> 队列侧 task id；抛异常表示投递失败
PublishFn = Callable[[str, int], str]


def _claim(db: Session, event_id: str, now: datetime) -> JobOutboxEvent | None:
    """抢占一条待投递事件。

    认领是「带条件的 UPDATE」，靠影响行数判断有没有抢到：
    多个 dispatcher 同时扫描时，只有一个能把它从 pending 改成 publishing。
    正在 publishing 但锁已过期的事件也可以被重新认领——说明上一个投递者挂了。
    """
    claimed = (
        db.query(JobOutboxEvent)
        .filter(
            JobOutboxEvent.id == event_id,
            or_(
                (JobOutboxEvent.status == "pending") & (JobOutboxEvent.next_attempt_at <= now),
                (JobOutboxEvent.status == "publishing") & (JobOutboxEvent.locked_until < now),
            ),
        )
        .update(
            {
                "status": "publishing",
                "locked_until": now + timedelta(seconds=JOB_OUTBOX_LOCK_SECONDS),
                "attempts": JobOutboxEvent.attempts + 1,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not claimed:
        return None
    return db.get(JobOutboxEvent, event_id)


def _mark_failed(db: Session, event_id: str, exc: Exception) -> None:
    """投递失败：退回 pending，按尝试次数做指数退避（上限 5 分钟）。"""
    event = db.get(JobOutboxEvent, event_id)
    if event is None or event.status != "publishing":
        return
    delay = min(300, 2 ** min(event.attempts, 8))
    event.status = "pending"
    event.locked_until = None
    event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    event.last_error = str(exc)[:2000]
    db.commit()


def _mark_published(db: Session, event_id: str, job_id: str) -> None:
    """投递成功：标记已发布，并记下入队时间，供恢复扫描判断队列是否积压。"""
    event = db.get(JobOutboxEvent, event_id)
    if event is None or event.status != "publishing":
        return
    event.status = "published"
    event.published_at = datetime.now(timezone.utc)
    event.locked_until = None
    event.last_error = None
    job = db.get(Job, job_id)
    if job is not None and job.status == "queued":
        job.last_enqueued_at = datetime.now(timezone.utc)
    db.commit()


def claim_and_publish(db: Session, event_id: str, publisher: PublishFn) -> bool:
    """认领并投递一条事件。返回是否真的投出去了。

    数据库会话由调用方提供（通常用 `with SessionLocal() as db`），
    这样多次调用可以共用一个会话，避免每条事件都开一次连接。
    """
    now = datetime.now(timezone.utc)
    event = _claim(db, event_id, now)
    if event is None:
        return False

    job_id = event.job_id
    generation = event.generation

    try:
        task_id = publisher(job_id, generation)
    except Exception as exc:
        _mark_failed(db, event_id, exc)
        logger.exception(
            "event=job.outbox_publish_failed job_id=%s generation=%s", job_id, generation
        )
        return False

    _mark_published(db, event_id, job_id)
    logger.info("event=job.enqueued job_id=%s task_id=%s", job_id, task_id)
    return True


def list_pending_event_ids(db: Session, limit: int = 100) -> list[str]:
    """列出该捞出来重投的事件：还没投的，以及投到一半锁已过期的。"""
    now = datetime.now(timezone.utc)
    return [
        row[0]
        for row in db.query(JobOutboxEvent.id)
        .filter(
            or_(
                (JobOutboxEvent.status == "pending") & (JobOutboxEvent.next_attempt_at <= now),
                (JobOutboxEvent.status == "publishing") & (JobOutboxEvent.locked_until < now),
            ),
        )
        .order_by(JobOutboxEvent.created_at)
        .limit(limit)
        .all()
    ]
