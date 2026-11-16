"""任务系统的业务编排。

职责边界：
- 任务的查询、取消、手动重试、按工作区列出
- 状态机判断（哪些状态可以取消、哪些可以重试）
- 乐观锁式的状态抢占：用带状态条件的 UPDATE 影响行数判断是否抢到，
  而不是「先读后写」——后者在并发下会双重执行
- 事务边界：状态变更与 outbox 投递意图同事务提交，提交后再触发投递

不负责：HTTP 语义、任务的实际执行、队列通道细节。

关于投递与恢复函数的注入：
`launch_job` 与 `resume_image_flow` 都接受调用方覆盖。图节点和路由把自己手上的
那一份传进来，这样测试对 `routes.launch_job` / `routes._resume_agent_image_flow`
的 monkeypatch 仍然生效。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import JOB_MAX_ATTEMPTS
from app.domain.exceptions import ConflictError, InternalError, NotFoundError
from app.domain.models import Job, JobOutboxEvent
from app.repositories import job_repo, workspace_repo
from app.repositories.outbox_repo import record_job_dispatch
from app.services.resource_urls import resource_urls

logger = logging.getLogger(__name__)

# 手动重试只对这两种状态开放：失败，或已停止自动重试
RETRYABLE_STATUSES = ("failed", "dead_letter")

# 已进入终态或已在取消中，不接受再次取消
CANCEL_SETTLED_STATUSES = {"cancel_requested", "cancelled", "completed", "failed", "dead_letter"}

__all__ = [
    "cancel_job",
    "get_job",
    "list_workspace_jobs",
    "retry_job",
    "job_payload",
]


def job_payload(job: Job) -> dict:
    """任务对外的字段形状。统一在这里定义，避免各处各写一套。"""
    result = {
        "id": job.id,
        "type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "result": resource_urls(job.result or {}, job_id=job.id),
        "error": job.error,
    }
    if job.job_type == "video_generation" and job.status == "completed":
        result["result"] = {**result["result"], "packageUrl": f"/api/v1/jobs/{job.id}/jianying-package"}
    return result


def _require_job(db: Session, job_id: str, user_id: str | None) -> Job:
    """取任务并校验归属。

    `user_id` 为 None 表示「内部调用，调用方已经验证过归属」——
    目前只有需要跨用户操作的后台扫描逻辑会这样调用；
    从 HTTP 进来的路径必须带上用户，否则任何一个带 job_id 的请求
    都能读到、取消、重试别人的任务。
    """
    if user_id is None:
        job = job_repo.get_job(db, job_id)
    else:
        job = job_repo.get_owned_job(db, job_id, user_id)
    if not job:
        raise NotFoundError("后台任务")
    return job


def get_job(db: Session, job_id: str, *, user_id: str | None = None) -> dict:
    return job_payload(_require_job(db, job_id, user_id))


def list_workspace_jobs(
    db: Session, workspace_id: str, *, user_id: str | None = None, limit: int = 50
) -> list[dict]:
    # 工作区不存在时先报错：否则前端拿到空列表，会误以为「这个工作区没有任务」。
    # 带 user_id 时同时校验归属——不能列出别人的任务。
    workspace = (
        workspace_repo.get(db, workspace_id)
        if user_id is None
        else workspace_repo.get_owned(db, workspace_id, user_id)
    )
    if workspace is None:
        raise NotFoundError("工作区")
    jobs = (
        db.query(Job)
        .filter(Job.workspace_id == workspace_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    return [job_payload(job) for job in jobs]


def cancel_job(db: Session, job_id: str, *, user_id: str | None = None, resume_image_flow=None) -> dict:
    """取消任务。

    排队中的任务直接置为 cancelled，并把 outbox 里待投递的意图一起取消——
    否则 worker 之后还会把这个任务捡起来跑，取消等于没取消。

    已在执行的任务只能置为 cancel_requested，由 worker 自己找时机停下。

    「取消成功」时若该任务是智能体图里的生图任务，需要把对话流程唤醒，
    否则前端会一直卡在等待状态。
    """
    job = _require_job(db, job_id, user_id)

    try:
        queued_cancelled = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status == "queued")
            .update({"status": "cancelled", "message": "任务已取消"}, synchronize_session=False)
        )

        resume_graph = False
        running_cancelled = 0
        if queued_cancelled:
            db.query(JobOutboxEvent).filter(
                JobOutboxEvent.job_id == job_id,
                JobOutboxEvent.status == "pending",
            ).update({"status": "cancelled"}, synchronize_session=False)
            job.status = "cancelled"
            job.message = "任务已取消"
            resume_graph = job.job_type == "image_generation" and bool((job.payload or {}).get("agentGraph"))
        else:
            running_cancelled = (
                db.query(Job)
                .filter(Job.id == job_id, Job.status == "running")
                .update({"status": "cancel_requested", "message": "正在停止当前任务"}, synchronize_session=False)
            )
            if running_cancelled:
                job.status = "cancel_requested"
                job.message = "正在停止当前任务"
            else:
                db.refresh(job)
                if job.status not in CANCEL_SETTLED_STATUSES:
                    raise ConflictError("当前任务状态不支持取消")

        db.commit()
    except Exception:
        db.rollback()
        raise

    if resume_graph and job.thread_id:
        try:
            _resume(job.thread_id, job.id, [], "任务已取消", resume_image_flow)
        except Exception as exc:
            logger.exception(
                "event=agent_graph.cancel_resume_failed thread_id=%s job_id=%s", job.thread_id, job.id
            )
            # 取消本身已经落库成功，只是收尾失败。用 500 而非 409：
            # 409 会让前端以为取消没生效，从而重试一个已经取消的任务。
            raise InternalError("任务已取消，但对话流程恢复失败") from exc

    return {"id": job.id, "status": job.status, "message": job.message}


def retry_job(db: Session, job_id: str, *, user_id: str | None = None, launcher=None) -> dict:
    """手动重试一个已失败的任务。

    重置尝试次数、心跳与执行令牌，把它们全部清空——留下的旧令牌会让
    worker 误以为上一轮还在跑，从而拒绝接收这次新执行。
    """
    job = _require_job(db, job_id, user_id)

    try:
        claimed = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status.in_(list(RETRYABLE_STATUSES)))
            .update(
                {
                    "status": "queued",
                    "progress": 0,
                    "attempt_count": 0,
                    "max_attempts": JOB_MAX_ATTEMPTS,
                    "heartbeat_at": None,
                    "execution_token": None,
                    "last_enqueued_at": None,
                    "message": "已手动重新排队",
                    "error": None,
                    "workflow_resume_status": "not_required",
                    "workflow_resume_error": None,
                },
                synchronize_session=False,
            )
        )
        if not claimed:
            raise ConflictError("只有失败且已停止自动重试的任务可以手动重试")
        db.refresh(job)
        # 每次重试生成号递增，避免复用到上一轮已被消费的 outbox 事件
        latest = (
            db.query(JobOutboxEvent.generation)
            .filter(JobOutboxEvent.job_id == job.id)
            .order_by(JobOutboxEvent.generation.desc())
            .first()
        )
        generation = latest[0] + 1 if latest else 0
        record_job_dispatch(db, job, generation)
        db.commit()
    except Exception:
        db.rollback()
        raise

    try:
        (launcher or _default_launcher)(job.id)
    except Exception:
        # 投递失败不回滚：outbox 里已留有 pending 意图，等补偿逻辑重投
        logger.exception("event=job.manual_retry_publish_deferred job_id=%s", job.id)

    return {"id": job.id, "status": "queued", "message": "任务已重新排队"}


def _default_launcher(job_id: str) -> None:
    from app.infrastructure.queue import publish_job

    publish_job(job_id)


def _resume(thread_id: str, job_id: str, assets: list, error: str, resume_image_flow=None) -> None:
    if resume_image_flow is not None:
        resume_image_flow(thread_id, job_id, assets, error)
        return
    from app.application.agent_image_flow import resume_agent_image_flow

    resume_agent_image_flow(thread_id, job_id, assets, error)
