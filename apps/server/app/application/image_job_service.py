"""生图任务的业务编排。

职责边界：
- 决定「一次生图要建几个 Job、每个 Job 装什么载荷」
- 控制事务边界：Job 与它的 outbox 投递意图必须同一事务写入
- 事务提交后再触发投递；投递失败只记日志，不使已落库的任务回滚

不负责：HTTP 语义、具体投递通道（Celery / 本地线程）、任务执行。

关于 `launch_job`：它在本模块的模块级公开，因此 `app.graph.nodes.launch_job`
（由 nodes 重新导出）上的 monkeypatch 打桩依旧生效，测试无需改动。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import JOB_MAX_ATTEMPTS
from app.domain.models import Job
from app.prompts.agent_prompt.image_style import DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
from app.repositories import job_repo
from app.repositories.outbox_repo import record_job_dispatch

from app.infrastructure.queue import publish_job as launch_job

logger = logging.getLogger(__name__)

__all__ = ["launch_job", "start_image_preview_job", "start_image_generation_job"]


def _commit_and_dispatch(db: Session, job: Job, record_dispatch=None) -> None:
    """Job 与 outbox 意图同事务提交，随后尽力投递。

    投递失败不回滚：outbox 表里已经留下 pending 意图，由补偿逻辑重投。

    record_dispatch 允许调用方传入自己的落库函数——图节点会把它转发成
    `nodes.record_job_dispatch`，这样既有测试的 monkeypatch 仍然生效。
    """
    job_repo.add_job(db, job)
    (record_dispatch or record_job_dispatch)(db, job)
    db.commit()
    db.refresh(job)


def _dispatch(job_id: str, event: str, thread_id: str, launcher=None) -> None:
    try:
        (launcher or launch_job)(job_id)
    except Exception:
        logger.exception("event=%s thread_id=%s job_id=%s", event, thread_id, job_id)


def start_image_preview_job(
    db: Session,
    *,
    workspace_id: str,
    thread_id: str,
    state: dict,
    first_scene: dict,
    record_dispatch=None,
    launcher=None,
) -> Job:
    """创建「两张风格样图」任务并投递。

    state 读的是 AgentGraphState 里的样式快照字段，这里只负责搬运，不做判断。
    返回已提交的任务对象，调用方负责写回 graph 状态。
    """
    job = Job(
        workspace_id=workspace_id,
        thread_id=thread_id,
        job_type="image_generation",
        status="queued",
        max_attempts=JOB_MAX_ATTEMPTS,
        progress=0,
        message="正在生成两张生图风格样图",
        payload={
            "storyboard": [first_scene],
            "sceneNumbers": [int(first_scene["sceneNumber"])],
            "previewCount": 2,
            "imageStylePrompt": state.get("image_style_prompt", ""),
            "imageStylePromptId": state.get("image_style_prompt_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
            "imageStylePromptVersionId": state.get("image_style_prompt_version_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
            "imageStylePromptVersion": state.get("image_style_prompt_version", 1),
            "previousPreviews": state.get("image_previews", []),
            "modelId": state.get("model_id", "deepseek-v4-flash"),
            "agentGraph": True,
            "agentGraphPreview": True,
        },
    )
    _commit_and_dispatch(db, job, record_dispatch)
    _dispatch(job.id, "agent_graph.image_preview_enqueue_deferred", thread_id, launcher)
    return job


def start_image_generation_job(
    db: Session,
    *,
    workspace_id: str,
    thread_id: str,
    state: dict,
    storyboard: list,
    scene_numbers: list[int],
    record_dispatch=None,
    launcher=None,
) -> Job:
    """创建「逐镜生成图片」任务并投递。

    scene_numbers 由调用方决定（首次生成 / 指定镜头重做），本函数不推导。
    """
    job = Job(
        workspace_id=workspace_id,
        thread_id=thread_id,
        job_type="image_generation",
        status="queued",
        max_attempts=JOB_MAX_ATTEMPTS,
        progress=0,
        message="等待逐镜生成图片",
        payload={
            "storyboard": storyboard,
            "sceneNumbers": scene_numbers,
            "selectedPreview": state.get("selected_preview"),
            "previewAssets": state.get("image_previews", []),
            "imageStylePrompt": state.get("image_style_prompt", ""),
            "imageStylePromptId": state.get("image_style_prompt_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
            "imageStylePromptVersionId": state.get("image_style_prompt_version_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
            "imageStylePromptVersion": state.get("image_style_prompt_version", 1),
            "modelId": state.get("model_id", "deepseek-v4-flash"),
            "regeneration": bool(state.get("image_assets")),
            "agentGraph": True,
        },
    )
    _commit_and_dispatch(db, job, record_dispatch)
    _dispatch(job.id, "agent_graph.image_job_enqueue_deferred", thread_id, launcher)
    return job
