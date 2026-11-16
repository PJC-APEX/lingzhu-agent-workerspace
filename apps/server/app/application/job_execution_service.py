"""任务执行期的状态机。

这里放的是「一个任务正在跑」时所有关于状态的业务规则，它们不属于
某个 worker、也不属于某个接口：

- 执行权：同一任务只能有一个执行者，用 `execution_token` 认领与校验。
  令牌对不上就说明执行权已经被别人拿走（或任务被取消），这时不能再写状态。
- 心跳：跑的过程中定期上报，扫描器据此判断执行者是否已经死掉。
- 失败处理：区分「永久失败」（重试没意义）与「暂时失败」（退避后重试），
  次数用尽则转入死信。
- 收尾：任务完成后写结果消息、唤醒对话流程。
- 取消落地：收到取消请求后，在下一个检查点把状态固化为 cancelled。

为什么放在 application 而不是 workers：
这些规则在 worker 里写一遍、在恢复扫描器里又要写一遍，两处一旦不一致
就会出现「worker 认为在跑、扫描器认为已死」。集中到这里，只有一份。

不负责：真正跑图、写文件、调模型。那些留在 workers。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import JOB_HEARTBEAT_SECONDS, JOB_STALE_SECONDS
from app.domain.models import AgentAction, AgentMessage, Job, JobOutboxEvent
from app.services.image_gen import ImageGenerationError
from app.repositories.outbox_repo import record_job_dispatch

logger = logging.getLogger(__name__)

__all__ = [
    "as_utc",
    "claim_job",
    "finish_cancel_request",
    "mark_completed",
    "owns_execution",
    "retry_or_dead_letter",
    "save_partial_result",
    "start_heartbeat",
    "update_job",
    "write_result_message",
]

# 任务已进入取消流程后，只有「落地为 cancelled」这一次写是被允许的，
# 其它状态更新一律丢弃——否则执行者会在取消之后又把状态改回 running。
_CANCEL_LOCKED_STATUSES = {"cancel_requested", "cancelled"}


def as_utc(value: datetime) -> datetime:
    """SQLite 读回来的是 naive datetime，统一按 UTC 处理。"""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def claim_job(db: Session, job_id: str, token: str) -> Job | None:
    """认领任务：把 queued 原子地改成 running，并写入本次执行的令牌。

    用带条件的 UPDATE 的 rowcount 判断，而不是「先查再改」——
    后者在两个 worker 同时拿到同一条消息时会双双认为自己是执行者。
    返回 None 表示没抢到（已被别人执行、已取消、或次数用尽）。
    """
    claimed = (
        db.query(Job)
        .filter(
            Job.id == job_id,
            Job.status == "queued",
            Job.attempt_count < Job.max_attempts,
        )
        .update(
            {
                "status": "running",
                "progress": 5,
                "message": "正在准备任务",
                "attempt_count": Job.attempt_count + 1,
                "heartbeat_at": datetime.now(timezone.utc),
                "execution_token": token,
            },
            synchronize_session=False,
        )
    )
    if not claimed:
        db.rollback()
        return None
    db.commit()
    return db.get(Job, job_id)


def update_job(
    db: Session,
    job_id: str,
    token: str | None,
    *,
    heartbeat: bool = True,
    **values,
) -> bool:
    """按令牌校验后更新任务字段。返回是否真的写进去了。

    三种情况会拒绝写入：
    1. 令牌与库里不一致 —— 执行权已不在本执行者手上
    2. 任务已在取消流程中，而这次更新不是「落地取消」
    3. 任务不存在
    """
    job = db.get(Job, job_id)
    if job is None:
        return False
    if token and job.execution_token != token:
        logger.warning("event=job.execution_lease_lost job_id=%s", job_id)
        return False
    new_status = values.get("status")
    if job.status in _CANCEL_LOCKED_STATUSES and new_status != "cancelled":
        return False
    for key, value in values.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(timezone.utc)
    if heartbeat and token and job.status == "running":
        job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()
    return True


def finish_cancel_request(db: Session, job_id: str) -> bool:
    """把「正在取消」固化为「已取消」。返回任务是否处于取消态。

    注意返回值的含义：True 表示「这个任务该停了」，包含已经取消完成的情况。
    调用方据此决定是否提前退出执行循环。
    """
    job = db.get(Job, job_id)
    if job is None or job.status not in _CANCEL_LOCKED_STATUSES:
        return False
    if job.status == "cancel_requested":
        job.status = "cancelled"
        job.message = "任务已取消"
        db.commit()
    return True


def owns_execution(db: Session, job_id: str, token: str) -> bool:
    """当前执行者是否仍然持有执行权。

    `cancel_requested` 也算持有：取消要等当前这一张图跑完，
    在循环的下一个检查点才停，避免中途撕出半张图。
    """
    job = db.get(Job, job_id)
    return bool(
        job
        and job.status in {"running", "cancel_requested"}
        and job.execution_token == token
    )


def save_partial_result(db: Session, job_id: str, token: str, result: dict) -> bool:
    """保存阶段性结果（已生成的样图等），顺带刷新心跳。

    只写 running 且令牌匹配的行：任务被取消或被别人接管时，这份中间结果
    不该覆盖掉对方的。
    """
    updated = (
        db.query(Job)
        .filter(
            Job.id == job_id,
            Job.status == "running",
            Job.execution_token == token,
        )
        .update(
            {"result": result, "heartbeat_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


def mark_completed(
    db: Session,
    job_id: str,
    result: dict,
    token: str | None,
    *,
    resume_workflow: bool,
) -> bool:
    """标记任务完成。只有 running 且令牌匹配时才算数。

    `workflow_resume_status` 决定收尾阶段要不要唤醒对话流程。
    """
    query = db.query(Job).filter(Job.id == job_id, Job.status == "running")
    if token:
        query = query.filter(Job.execution_token == token)
    updated = query.update(
        {
            "status": "completed",
            "progress": 100,
            "message": "任务完成",
            "result": result,
            "heartbeat_at": None,
            "execution_token": None,
            "workflow_resume_status": "pending" if resume_workflow else "not_required",
        },
        synchronize_session=False,
    )
    db.commit()
    return updated == 1


def _is_permanent_failure(exc: Exception) -> bool:
    """判断这个错误重试有没有意义。

    配置缺失、鉴权失败、请求格式错这类问题，重试一百次结果都一样，
    直接进死信，别浪费时间和额度。
    """
    if isinstance(exc, (ValueError, TypeError)):
        return True
    if isinstance(exc, ImageGenerationError):
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "未配置", "http 400", "http 401", "http 403", "http 404", "http 422",
                "格式不正确", "无效", "不支持的图片格式",
            )
        )
    return False


def retry_or_dead_letter(db: Session, job_id: str, token: str | None, exc: Exception) -> str:
    """失败后决定是重试还是转死信。返回 "retry" / "dead_letter" / 当前状态。

    退避按尝试次数翻倍（1、2、4……上限 300 秒），避免对端故障时
    所有任务在同一秒一起重试把自己压垮。
    """
    now = datetime.now(timezone.utc)
    job = db.get(Job, job_id)
    if job is None or job.status != "running" or job.execution_token != token:
        return job.status if job else "missing"

    permanent = _is_permanent_failure(exc)
    exhausted = job.attempt_count >= job.max_attempts
    current_attempt = job.attempt_count
    job.error = str(exc)
    job.execution_token = None
    job.heartbeat_at = None

    if permanent or exhausted:
        job.status = "dead_letter"
        job.message = "任务无法自动恢复，已转入失败任务"
        state = "dead_letter"
    else:
        delay = min(300, 2 ** max(job.attempt_count - 1, 0))
        job.status = "queued"
        job.progress = 0
        job.message = f"任务暂时失败，将在 {delay} 秒后重试"
        job.last_enqueued_at = None
        latest = (
            db.query(JobOutboxEvent.generation)
            .filter(JobOutboxEvent.job_id == job.id)
            .order_by(JobOutboxEvent.generation.desc())
            .first()
        )
        generation = latest[0] + 1 if latest else 0
        record_job_dispatch(
            db, job, generation=generation, next_attempt_at=now + timedelta(seconds=delay)
        )
        state = "retry"

    db.commit()
    logger.warning(
        "event=job.execution_%s job_id=%s attempt=%s error_type=%s",
        state,
        job_id,
        current_attempt,
        type(exc).__name__,
    )
    return state


def write_result_message(db: Session, job: Job, thread_id: str) -> str | None:
    """任务完成后写一条面向用户的结果消息，返回消息 id。

    幂等：已经有 result_message_id 就直接返回，避免收尾重试时写重复消息。
    """
    action = db.query(AgentAction).filter(AgentAction.job_id == job.id).first()
    if job.status != "completed" or action is None or job.result_message_id:
        return job.result_message_id

    content = (
        "图片素材已生成，请到素材库检查质量。"
        if job.job_type == "image_generation"
        else "SRT 字幕与分镜图片已配对，JSON 时间线已生成。当前 JSON 格式尚未按剪映小助手样例验证。"
    )
    message = AgentMessage(
        thread_id=thread_id,
        role="assistant",
        message_type="job_result",
        content=content,
        metadata_json={"jobId": job.id, "result": dict(job.result or {})},
    )
    db.add(message)
    db.flush()
    job.result_message_id = message.id
    return message.id


def start_heartbeat(
    db_factory: Callable[[], Session],
    job_id: str,
    token: str,
    interval: float | None = None,
) -> threading.Event:
    """起一个后台线程定期刷新心跳，返回用于停止的 Event。

    刷新失败（数据库抖动）不致命，记日志继续——真正的失联判定在扫描器那边。
    发现令牌不再匹配就主动退出，说明执行权已经易主。
    """
    stop = threading.Event()
    wait_seconds = JOB_HEARTBEAT_SECONDS if interval is None else interval

    def _beat() -> None:
        while not stop.wait(wait_seconds):
            try:
                with db_factory() as db:
                    updated = (
                        db.query(Job)
                        .filter(
                            Job.id == job_id,
                            Job.status == "running",
                            Job.execution_token == token,
                        )
                        .update(
                            {"heartbeat_at": datetime.now(timezone.utc)},
                            synchronize_session=False,
                        )
                    )
                    db.commit()
                if not updated:
                    return
            except Exception:
                logger.exception("event=job.heartbeat_failed job_id=%s", job_id)

    threading.Thread(
        target=_beat,
        name=f"job-heartbeat-{job_id[:8]}",
        daemon=True,
    ).start()
    return stop


def is_stale(heartbeat_at: datetime | None, updated_at: datetime | None, now: datetime | None = None) -> bool:
    """判断一个 running 任务是否已经失联，供恢复扫描器使用。"""
    reference = as_utc(now or datetime.now(timezone.utc))
    stale_before = reference - timedelta(seconds=JOB_STALE_SECONDS)
    if heartbeat_at is not None:
        return as_utc(heartbeat_at) < stale_before
    return updated_at is not None and as_utc(updated_at) < stale_before
