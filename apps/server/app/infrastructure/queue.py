"""队列通道（broker）适配。

这一层只回答一个问题：怎么把一个「执行请求」丢到队列里去。
它不知道任务是什么、任务该怎么跑，也不碰数据库。

当前实现是 Celery + Redis。要换成别的（比如 SQS、Kafka），
只需要改这个文件，上层 application 与 outbox 都不受影响。

关于任务注册：
queue 不认识任何具体的 worker 模块——那样会形成
`local_tasks → queue → local_tasks` 的循环导入。
改成注册表：worker 模块在导入时调用 `register_task(...)` 把自己交上来，
queue 只管用。谁注册、注册了什么，是 worker 侧的事。
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from app.domain.models import Job, JobOutboxEvent

logger = logging.getLogger(__name__)

__all__ = [
    "enqueue_media_job",
    "publish_job",
    "register_task",
    "MEDIA_JOB_TASK_NAME",
    "PublishFn",
]

# publisher(job_id, generation) -> 队列侧 task id
PublishFn = Callable[[str, int], str]

# 队列里执行媒体任务的名字。定义在这一层，worker 侧注册时引用同一个常量，
# 避免两边字符串写不一致。
MEDIA_JOB_TASK_NAME = "lingzhu.process_media_job"


class QueueTask(Protocol):
    """队列侧能接受的东西：一个能 apply_async 的 Celery 任务对象。"""

    def apply_async(self, *, args: list, task_id: str): ...


# 任务名 -> Celery 任务对象。由 worker 模块在导入时填充。
_REGISTERED_TASKS: dict[str, QueueTask] = {}


def register_task(name: str, task: QueueTask) -> None:
    """由 worker 模块在导入时调用，把自己注册成可投递的任务。

    重复注册同一个名字会被后来的覆盖——这在测试里很方便：
    换一个假任务就能验证投递行为。
    """
    _REGISTERED_TASKS[name] = task


def _require_task(name: str) -> QueueTask:
    task = _REGISTERED_TASKS.get(name)
    if task is None:
        # 常见原因：worker 模块还没被导入（比如只起了 API 进程、没起 worker）。
        raise RuntimeError(
            f"队列任务 {name} 尚未注册，调用方需要先 import 对应的 worker 模块"
        )
    return task


def enqueue_media_job(job_id: str, generation: int, task: QueueTask | None = None) -> str:
    """把任务投到 broker，返回队列侧的 task id。

    重复投递是安全的：真正的执行权由数据库里的任务状态抢占决定，
    broker 里多一条消息只会让 worker 白白醒来一次，然后发现抢不到就退出。
    """
    target = task or _require_task(MEDIA_JOB_TASK_NAME)

    # task_id 带上 generation：同一任务的不同重试轮次在 broker 里是不同的消息，
    # 不会互相覆盖。
    result = target.apply_async(args=[job_id], task_id=f"{job_id}-{generation}")
    return result.id


def _publish_via_celery(job_id: str, generation: int) -> str:
    return enqueue_media_job(job_id, generation)


def publish_job(job_id: str, *, session_factory=None, publisher: "PublishFn | None" = None) -> str:
    """确保任务有一条待投递意图，并立即尝试投递。

    调用方通常在业务事务里已经写过 outbox 意图；这里兜底处理
    「意图缺失」的情况（比如历史数据、或意图被清理过），保证任务不会
    因为少一条意图就永远躺在库里没人跑。

    投递失败不抛异常：意图留在库里，由定时扫描器重投。
    """
    from app.infrastructure import outbox
    from app.repositories.outbox_repo import record_job_dispatch

    if session_factory is None:
        from app.infrastructure.database import get_session_factory

        session_factory = get_session_factory()
    publish = publisher or _publish_via_celery

    with session_factory() as db:
        job = db.get(Job, job_id)
        if not job:
            raise ValueError("找不到后台任务")
        event = (
            db.query(JobOutboxEvent)
            .filter(JobOutboxEvent.job_id == job_id, JobOutboxEvent.status == "pending")
            .order_by(JobOutboxEvent.generation)
            .first()
        )
        if not event:
            latest = (
                db.query(JobOutboxEvent.generation)
                .filter(JobOutboxEvent.job_id == job_id)
                .order_by(JobOutboxEvent.generation.desc())
                .first()
            )
            generation = (latest[0] + 1) if latest else 0
            event = record_job_dispatch(db, job, generation)
            db.commit()
        event_id = event.id
        outbox.claim_and_publish(db, event_id, publisher=publish)
    return job_id
