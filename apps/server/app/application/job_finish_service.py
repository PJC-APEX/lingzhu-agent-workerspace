"""任务收尾：写结果消息、唤醒对话流程。

一个任务跑完之后还有一段「收尾状态机」，它决定的是业务后果，而不是
怎么跑图：

- 只有 `pending` 或 `processing` 的任务才需要收尾，已完成的重复调用直接返回。
- `processing` 且没超时 = 别人正在收尾，不能重入；`processing` 且超时 =
  上一次收尾的进程死掉了，允许抢回来重做。
- 抢到之后认一次重新查询，避免用进来时的旧快照做判断。
- agent 发起的生图还有额外一步：把结果（或取消原因）回灌进对话图。
  这一步可能抛异常，抛了就退回 `pending`，交给重试/恢复扫描再捡起来。

为什么放在 application 而不是 workers：
worker 和恢复扫描器（recover_media_jobs）都要走这段逻辑，两处写两遍必然
出现「worker 认为收尾过了、扫描器认为没收尾」。集中到这里，只有一份。

不负责：真正往对话图里写什么。那由调用方通过 `resume_image_flow` 注入，
默认实现是 application/agent_image_flow.py。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import JOB_STALE_SECONDS
from app.domain.models import Job
from app.application.agent_image_flow import resume_agent_image_flow
from app.application.job_execution_service import as_utc, write_result_message

logger = logging.getLogger(__name__)

__all__ = ["finish_side_effects"]

ResumeImageFlow = Callable[[str, str, list[dict], str], None]

# 需要收尾的状态；`not_required` / `completed` 都表示不必再跑。
_RESUMABLE_STATUSES = {"pending", "processing"}


def _default_resume_image_flow(thread_id: str, job_id: str, assets: list[dict], error: str = "") -> None:
    resume_agent_image_flow(thread_id, job_id, assets, error)


def _claim_resume(db: Session, job: Job) -> bool:
    """把这次收尾「认领」下来。返回 False 表示别人在做，或已经做完。"""
    current = job.workflow_resume_status
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=JOB_STALE_SECONDS)

    claimed = db.query(Job).filter(Job.id == job.id, Job.workflow_resume_status == current)
    if current == "processing":
        started = job.workflow_resume_started_at
        # 没超时就别抢：另一次收尾正在跑，重复执行会往对话里写两条结果消息。
        if started and as_utc(started) > stale_before:
            return False
        # 只有超时的那条才能被抢，否则两个进程会同时抢到。
        claimed = claimed.filter(Job.workflow_resume_started_at < stale_before)

    claimed = claimed.update(
        {
            "workflow_resume_status": "processing",
            "workflow_resume_started_at": datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )
    if not claimed:
        db.rollback()
        return False
    db.commit()
    return True


def finish_side_effects(
    db_factory: Callable[[], Session],
    job_id: str,
    *,
    resume_image_flow: ResumeImageFlow | None = None,
) -> None:
    """写结果消息，并（对 agent 生图任务）唤醒对话流程。幂等，重试安全。

    `db_factory` 由调用方注入，worker 传自己的 SessionLocal——测试对它的打桩
    因此继续生效。
    """
    resume = resume_image_flow or _default_resume_image_flow

    with db_factory() as db:
        job = db.get(Job, job_id)
        if not job or job.workflow_resume_status not in _RESUMABLE_STATUSES:
            return
        if not _claim_resume(db, job):
            return

        # 上面 commit 之后重新取一次：认领用的是 SQL 条件更新，
        # 内存里的 job 还是认领前的快照。
        job = db.get(Job, job_id)
        payload = dict(job.payload or {})
        result = dict(job.result or {})
        thread_id = job.thread_id
        if not thread_id:
            job.workflow_resume_status = "not_required"
            db.commit()
            return

        write_result_message(db, job, thread_id)
        should_resume = job.job_type == "image_generation" and payload.get("agentGraph")
        resume_error = job.workflow_resume_error or (
            job.error if job.status in {"failed", "dead_letter"} else ""
        )
        job_status = job.status
        if not should_resume:
            job.workflow_resume_status = "completed"
        db.commit()

    if not should_resume:
        return

    try:
        resume(
            thread_id,
            job_id,
            result.get("assets", []) if job_status == "completed" else [],
            resume_error,
        )
    except Exception as exc:
        # 退回 pending 让恢复扫描再捡一次；不吞异常，交给调用方决定是否重试任务。
        with db_factory() as db:
            job = db.get(Job, job_id)
            if job:
                job.workflow_resume_status = "pending"
                job.workflow_resume_error = str(exc)
                db.commit()
        raise

    with db_factory() as db:
        job = db.get(Job, job_id)
        if job:
            job.workflow_resume_status = "completed"
            job.workflow_resume_error = None
            job.workflow_resume_started_at = None
            db.commit()
