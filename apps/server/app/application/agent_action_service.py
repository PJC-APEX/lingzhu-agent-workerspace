"""确认卡片的业务规则。

一张确认卡片被用户按下「确认 / 修改 / 重试样图」之后要发生什么，属于
业务规则，不属于 HTTP：

- 谁能处理这张卡片（must 属于当前会话、必须是待确认状态）
- 抢卡：并发点两次只能有一次生效，第二次走「已处理」幂等返回而不是报错
- 改图类确认要把对应素材标记为已通过
- 视频类确认要在动手前校验字幕与素材是否仍然就绪

错误用业务异常表达，由 api 层统一转成 HTTP 状态码；这里不 import FastAPI。

不负责：恢复 LangGraph 流程（那属于 agent_execution_service）、调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session
from app.repositories import job_repo, agent_repo, asset_repo, subtitle_repo

from app.core.config import JOB_MAX_ATTEMPTS
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AgentAction, AgentThread, Asset, Job, SubtitleFile
from app.prompts.agent_prompt.image_style import DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
from app.application.prompt_service import resolve_agent_prompt_version
from app.repositories.outbox_repo import record_job_dispatch

from . import agent_service

__all__ = [
    "ActionResolution",
    "JobCreation",
    "get_action_for_thread",
    "already_processed_response",
    "resolve_retry_preview_style",
    "validate_selected_preview",
    "claim_action",
    "apply_confirmation_side_effects",
    "image_batch_readiness",
    "prepare_action_job",
    "create_job_for_action",
]


@dataclass(frozen=True)
class ActionResolution:
    """卡片处理完成后要回给前端的结果。

    status 取值与历史接口保持一致：already_processed / rejected 由本模块决定，
    queued / continued 由 api 层在恢复流程后补充。
    """

    status: str
    job_id: str | None = None


@dataclass(frozen=True)
class JobCreation:
    """确认卡片建完任务后拿到的结果。

    返回这个而不是裸 Job，是因为调用方真正需要的是「三件事都成了」这个
    整体承诺：任务建了、卡片关联了、消息落库了。只给 Job，调用方还得
    自己去拼响应，也就看不出关联那步有没有做对。
    """

    job: Job
    job_type: str
    message: str

    @property
    def job_id(self) -> str:
        return self.job.id


def get_action_for_thread(db: Session, action_id: str, thread_id: str) -> AgentAction:
    """取本会话下的卡片。跨会话访问一律按「找不到」处理，不泄露存在性。"""
    action = agent_repo.get_action(db, action_id)
    if not action:
        raise NotFoundError("待确认操作")
    if action.thread_id != thread_id:
        raise NotFoundError("该对话中的操作")
    return action


def already_processed_response(action: AgentAction, decision: str) -> ActionResolution:
    """卡片已经按「同一个决定」处理过时，返回幂等结果。

    同一个决定 = 确认对 confirmed、拒绝/重试对 rejected。决定不同则冲突。
    """
    same_decision = (action.status == "confirmed" and decision == "approve") or (
        action.status == "rejected" and decision in {"reject", "retry_preview"}
    )
    if not same_decision:
        raise ConflictError("该操作已经按其他决定处理")
    return ActionResolution(status="already_processed", job_id=action.job_id)


def resolve_retry_preview_style(db: Session, action: AgentAction, body, user_id: str):
    """校验并解析「重试样图」时提交的风格提示词版本。

    只有重试样图才需要；其它决定返回 None。
    规则来自原实现：必须是非空提示词，版本缺省回落到卡片里记录的那一版。
    """
    if body.decision != "retry_preview":
        return None
    if action.action_type != "graph_confirm_image_style" or not (body.stylePrompt or "").strip():
        raise ValidationError("只能在样图确认卡中提交非空生图风格提示词")
    version_id = body.imageStylePromptVersionId or (
        (action.payload or {}).get("imageStyle", {}).get("versionId", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID)
    )
    try:
        return resolve_agent_prompt_version(db, version_id, "image_style", user_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def validate_selected_preview(action: AgentAction, decision: str, selected_preview_index) -> None:
    """样图确认卡必须先选中一张样图。"""
    if action.action_type == "graph_confirm_image_style" and decision == "approve" and selected_preview_index is None:
        raise ValidationError("请先选择一张样图")


def claim_action(db: Session, action_id: str, thread_id: str, decision: str) -> bool:
    """抢卡片：把 awaiting_confirmation 原子地改成 confirmed / rejected。

    用条件 UPDATE 的 rowcount 判断，而不是「先查再改」——用户双击时
    两个请求会同时读到 awaiting_confirmation，双双通过检查。
    返回 False 表示没抢到，调用方应走幂等返回。
    """
    target_status = "confirmed" if decision == "approve" else "rejected"
    claimed = agent_repo.claim_action(db, action_id, thread_id, target_status)
    if not claimed:
        db.rollback()
        return False
    return True


def apply_confirmation_side_effects(db: Session, action: AgentAction, decision: str) -> None:
    """确认卡片带来的附加改动：图片类确认要把素材标为已通过。

    只处理 graph_confirm_images 的 approve，其它类型不动素材。
    提交由调用方负责——它要把「卡片状态」和「素材状态」放在同一个事务里。
    """
    if action.action_type != "graph_confirm_images" or decision != "approve":
        return
    thread = agent_repo.get_thread(db, action.thread_id)
    if not thread:
        return
    asset_ids = [item for item in (action.payload or {}).get("assetIds", []) if item]
    for asset_id in asset_ids:
        asset = job_repo.get_asset(db, asset_id)
        if asset and asset.workspace_id == thread.workspace_id:
            asset.status = "approved"


def image_batch_readiness(db: Session, thread: AgentThread, image_job_id: str | None = None):
    """检查「本次对话」是否有一批可以做时间线的图片。

    返回 (job, 错误文案)。错误文案为空表示就绪。
    规则：必须是本会话、已完成、分镜与素材一一对应、且全部审核通过。
    """
    if image_job_id:
        image_job = job_repo.get_job(db, image_job_id)
    else:
        image_job = job_repo.latest_completed_images(db, thread.workspace_id, thread.id)
    if not image_job or image_job.workspace_id != thread.workspace_id or image_job.thread_id != thread.id:
        return None, "找不到本次对话已完成的图片生成任务"
    if image_job.job_type != "image_generation" or image_job.status != "completed":
        return None, "图片生成任务尚未完成"

    scenes = (image_job.result or {}).get("storyboard") or []
    if not scenes:
        return None, "图片任务没有保存分镜信息，无法与字幕配对"
    expected_numbers = [int(scene.get("sceneNumber", index)) for index, scene in enumerate(scenes, start=1)]
    assets = asset_repo.list_for_job(db, image_job.id)
    assets_by_scene: dict[int, list[Asset]] = {}
    for asset in assets:
        assets_by_scene.setdefault(asset.scene_number, []).append(asset)
    if any(len(assets_by_scene.get(number, [])) != 1 for number in expected_numbers) or len(assets) != len(expected_numbers):
        return None, "本次图片没有与所有分镜一一对应，请重新生成或检查素材"
    if any(assets_by_scene[number][0].status != "approved" for number in expected_numbers):
        return None, "请先审核通过本次分镜生成的全部图片"
    return image_job, ""


def prepare_action_job(db: Session, thread: AgentThread, action: AgentAction, user_id: str):
    """为「确认卡片 → 后台任务」准备好工单参数。

    返回 (job_type, 面向用户的消息)。校验不通过抛业务异常：
    - 视频类必须先有本工作区的字幕，且图片批次已就绪
    - 其它类型目前不支持

    只做校验与算参数；建 Job、写 outbox、落消息、提交都由
    create_job_for_action 一次性完成，免得调用方自己拼那条长链。
    """
    if action.action_type == "generate_images":
        return "image_generation", "已确认，开始按最终文案进行分镜和生图。"
    if action.action_type == "generate_video":
        subtitle = subtitle_repo.get(db, (action.payload or {}).get("subtitleFileId"))
        if not subtitle or subtitle.workspace_id != thread.workspace_id:
            raise ConflictError("字幕状态已变化，请重新上传 SRT")
        _image_job, image_error = image_batch_readiness(db, thread, (action.payload or {}).get("imageJobId"))
        if image_error:
            raise ConflictError(image_error)
        return "video_generation", "已确认，开始解析 SRT、匹配已确认的分镜图片并组装剪映草稿。"
    raise ValidationError("暂不支持该操作类型")


def create_job_for_action(
    db: Session,
    thread: AgentThread,
    action: AgentAction,
    user_id: str,
    record_dispatch=None,
) -> JobCreation:
    """确认卡片通过后，把「建任务」这件事一次性做完。

    一个事务里做四件事，缺一件就会留下坏状态：
    1. 建 Job，并写 outbox 意图（同期写入，投递失败也不会丢任务）
    2. 把 job_id 关联回确认卡片，否则卡片永远显示"已确认但没任务"
    3. 落一条 assistant 消息（前端据此插任务卡片）
    4. 提交

    刻意**不投递**：投递是传输层的事，由调用方拿 job_id 自己去做。
    这样"业务规则"和"触发队列"分开，也让投递失败的处理留在原处不动。

    record_dispatch 允许调用方注入自己的落库函数——既有测试打桩的是
    routes 里那一份 record_job_dispatch，注入进来才不会绕过它们。
    """
    if record_dispatch is None:
        record_dispatch = record_job_dispatch

    job_type, message = prepare_action_job(db, thread, action, user_id)
    job = Job(
        workspace_id=thread.workspace_id,
        thread_id=thread.id,
        job_type=job_type,
        status="queued",
        progress=0,
        max_attempts=JOB_MAX_ATTEMPTS,
        message="等待执行",
        payload=action.payload,
    )
    job_repo.add_job(db, job)
    # outbox 记录需要 job.id，必须先 flush 拿到主键
    job_repo.flush(db)
    record_dispatch(db, job)
    action.job_id = job.id
    agent_service.save_agent_message(
        db,
        thread.id,
        "assistant",
        message,
        message_type="job",
        metadata={"jobId": job.id},
    )
    db.commit()
    db.refresh(job)
    return JobCreation(job=job, job_type=job_type, message=message)
