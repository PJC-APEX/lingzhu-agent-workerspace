"""任务恢复扫描的业务规则。

后台任务可能因为 worker 崩溃、进程被杀、机器重启而停在 running 状态，
永远没人收尾。这个模块就是那个「打扫战场」的规则：

1. 心跳超时的 running 任务：次数没用尽就打回 queued 重新排队，
   用尽了就升级成 dead_letter
2. 卡在 queued 但没人管的：补一条投递意图，或判定次数用尽
3. 图流程的回灌没做完的：交给 job_finish_service 补做

数据库会话由调用方提供（worker 传自己的 SessionLocal），投递意图的
落库函数也可注入——这样既有测试对 worker 模块里那份符号的打桩继续生效。

不负责：真正投递 outbox、真正恢复图流程。调用方拿到结果自己去做，
那两件事需要跨进程/跨会话，不属于「算规则」。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.application import job_execution_service as execution
from app.domain.models import Job, JobOutboxEvent
from app.repositories.outbox_repo import record_job_dispatch

logger = logging.getLogger(__name__)

__all__ = ["RecoveryResult", "scan_and_recover"]

# 任务是这两个状态时，它的待投递意图已经没有意义了：
# 投出去 worker 也会因为「状态不是 queued」直接跳过。
_DEAD_OUTBOX_STATUSES = ("pending", "publishing")


class RecoveryResult:
    """这一轮扫描的结果，供调用方决定还要做什么。"""

    __slots__ = ("recovered", "dead_lettered", "pending_resumes")

    def __init__(self) -> None:
        self.recovered = 0
        self.dead_lettered = 0
        self.pending_resumes: list[str] = []

    def as_dict(self, dispatched: int = 0) -> dict:
        return {
            "recovered": self.recovered,
            "deadLettered": self.dead_lettered,
            "dispatched": dispatched,
        }


def _cancel_outbox_intents(db: Session, job_id: str) -> int:
    """把任务的待投递意图标成 cancelled。

    用 cancelled 而不是删除：保留痕迹，排查时能看出「这里本来要投，
    但任务已经不需要执行了」。这和 job_service 取消任务时的处理一致。
    """
    return (
        db.query(JobOutboxEvent)
        .filter(
            JobOutboxEvent.job_id == job_id,
            JobOutboxEvent.status.in_(_DEAD_OUTBOX_STATUSES),
        )
        .update({"status": "cancelled"}, synchronize_session=False)
    )


def _next_generation(db: Session, job_id: str) -> int:
    latest = (
        db.query(JobOutboxEvent.generation)
        .filter(JobOutboxEvent.job_id == job_id)
        .order_by(JobOutboxEvent.generation.desc())
        .first()
    )
    return latest[0] + 1 if latest else 0


def scan_and_recover(
    db: Session,
    *,
    now: datetime | None = None,
    stale_seconds: int,
    record_dispatch=None,
) -> RecoveryResult:
    """扫一轮，把能救的任务救回来。不提交事务、不投递。

    now 可注入是为了让测试能精确控制「多久算超时」，不必等真实时间流逝。
    """
    if record_dispatch is None:
        record_dispatch = record_job_dispatch
    now = now or datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=stale_seconds)
    result = RecoveryResult()

    # 先粗筛（SQL 只表达「可能过期」），再用 is_stale 精判。
    # 超时阈值规则只有 application 一份，不在这里重写比较条件。
    candidates = db.query(Job).filter(
        Job.status == "running",
        Job.heartbeat_at < stale_before,
    ).with_for_update(skip_locked=True).all()
    candidates += db.query(Job).filter(
        Job.status == "running",
        Job.heartbeat_at.is_(None),
        Job.updated_at < stale_before,
    ).with_for_update(skip_locked=True).all()

    for job in candidates:
        if not execution.is_stale(job.heartbeat_at, job.updated_at, now):
            continue
        job.execution_token = None
        job.heartbeat_at = None
        job.error = job.error or "Worker 心跳超时，任务执行状态未知"
        if job.attempt_count >= job.max_attempts:
            job.status = "dead_letter"
            job.message = "任务重试次数已用尽"
            _cancel_outbox_intents(db, job.id)
            result.dead_lettered += 1
        else:
            job.status = "queued"
            job.progress = 0
            job.message = "Worker 中断，任务已重新排队"
            record_dispatch(db, job, job.attempt_count)
            result.recovered += 1

    for job in db.query(Job).filter(Job.status == "queued").all():
        if job.attempt_count >= job.max_attempts:
            job.status = "dead_letter"
            job.message = "任务重试次数已用尽"
            job.error = job.error or "任务达到最大尝试次数"
            _cancel_outbox_intents(db, job.id)
            result.dead_lettered += 1
            continue
        last_enqueued = execution.as_utc(job.last_enqueued_at) if job.last_enqueued_at else None
        if last_enqueued and last_enqueued > stale_before:
            continue
        has_open_intent = db.query(JobOutboxEvent.id).filter(
            JobOutboxEvent.job_id == job.id,
            JobOutboxEvent.status.in_(_DEAD_OUTBOX_STATUSES),
        ).first()
        if has_open_intent:
            continue
        record_dispatch(db, job, _next_generation(db, job.id))

    result.pending_resumes = [job.id for job in db.query(Job.id).filter(
        or_(
            Job.workflow_resume_status == "pending",
            (Job.workflow_resume_status == "processing") & (Job.workflow_resume_started_at < stale_before),
        ),
        Job.job_type == "image_generation",
    ).limit(100).all()]
    return result
