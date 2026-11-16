"""智能体对话一轮执行的业务规则。

一轮对话要做的事，按发生顺序：

1. 幂等闸门：同一个 request_id 只处理一次；重放时原样回放上次的结果
   （包括失败），内容指纹不一致则视为「这个 id 已经被别的请求占了」
2. 落用户消息；首轮对话顺便用首句当标题
3. 记录这一轮正在处理（AgentTurnRequest），并发撞唯一约束时按重放处理
4. 决定这一轮是「新输入」还是「续上中断的流程」，并把续接参数算好
5. 跑完落结果消息，或把失败原因写回这一轮记录

错误用业务异常表达，由 api 层统一转 HTTP；这里不 import FastAPI，
也不产出 SSE 文本——那是传输格式，留在 api 层。

不负责：真正调用模型、写检查点。那是 graph 层。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import (
    AgentAction,
    AgentMessage,
    AgentThread,
    AgentTurnRequest,
    Asset,
    SubtitleFile,
)
from app.repositories import agent_repo, job_repo, subtitle_repo
from app.application.prompt_service import resolve_agent_prompt_version

logger = logging.getLogger(__name__)

__all__ = [
    "PromptSelection",
    "TurnContext",
    "ResumePlan",
    "fingerprint_request",
    "resolve_prompt_selection",
    "find_existing_turn",
    "prepare_turn",
    "save_agent_message",
    "plan_resume",
    "persist_turn_result",
]

# 聊天里对确认卡片的回答不推进流程，只当普通对话处理。
_DETACHED_CHAT = "chat"


@dataclass(frozen=True)
class PromptSelection:
    """三个提示词版本在一轮对话里的快照。"""

    rewrite: object
    storyboard: object
    image_style: object

    def to_metadata(self) -> dict:
        def one(item) -> dict:
            return {"promptId": item.prompt_id, "versionId": item.version_id, "version": item.version}

        return {
            "rewritePrompt": one(self.rewrite),
            "storyboardPrompt": one(self.storyboard),
            "imageStylePrompt": one(self.image_style),
        }

    def to_graph_state(self) -> dict:
        """写进 LangGraph state 的字段。"""
        return {
            "rewrite_prompt_version_id": self.rewrite.version_id,
            "storyboard_prompt_version_id": self.storyboard.version_id,
            "image_style_prompt_id": self.image_style.prompt_id,
            "image_style_prompt_version_id": self.image_style.version_id,
            "image_style_prompt_version": self.image_style.version,
            "image_style_prompt": self.image_style.content,
        }


@dataclass(frozen=True)
class TurnContext:
    """一轮对话落库后的现场。api 层拿着它去跑流。"""

    thread: AgentThread
    thread_id: str
    request_id: str
    user_message: AgentMessage
    turn_request: AgentTurnRequest
    prompt_selection: PromptSelection
    attached_text: str = ""


@dataclass
class ResumePlan:
    """这一轮是「新输入」还是「续上中断」，以及续接参数。"""

    resume: dict | None = None
    resume_state_update: dict | None = None
    detached_chat: bool = False
    resume_target: str = "workflow"
    # 给日志用：决策是否推进了某张确认卡片
    resolved_confirmation: bool = False
    extra: dict = field(default_factory=dict)


def fingerprint_request(body) -> str:
    """去掉 request_id 本身后算指纹，用来判断 id 是否被换内容复用。"""
    payload = body.model_dump(mode="json", exclude={"requestId"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_prompt_selection(db: Session, body, user_id: str) -> PromptSelection:
    """解析三个提示词版本。版本无效时按参数错误处理。"""
    try:
        return PromptSelection(
            rewrite=resolve_agent_prompt_version(db, body.rewritePromptVersionId, "rewrite", user_id),
            storyboard=resolve_agent_prompt_version(db, body.storyboardPromptVersionId, "storyboard", user_id),
            image_style=resolve_agent_prompt_version(db, body.imageStylePromptVersionId, "image_style", user_id),
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def get_thread(db: Session, thread_id: str, user_id: str) -> AgentThread:
    """取本用户的会话。

    归属校验不能省：会话本身没有 owner 字段，必须上溯到工作区的
    owner_user_id。旧代码走 api 层的 _get_or_404，那里是带校验的；
    只做 db.get 会让任何人拿到别人的 thread_id 就能读写。
    """
    thread = agent_repo.get_owned_thread(db, thread_id, user_id)
    if not thread:
        raise NotFoundError("找不到智能体会话")
    return thread


def find_existing_turn(db: Session, thread_id: str, request_id: str, request_hash: str) -> AgentTurnRequest | None:
    """同一个 request_id 已经处理过就返回那条记录，内容不一致直接冲突。"""
    existing = agent_repo.find_turn(db, thread_id, request_id)
    if not existing:
        return None
    if existing.request_hash != request_hash:
        raise ConflictError("request_id 已用于另一条请求")
    return existing


def save_agent_message(
    db: Session,
    thread_id: str,
    role: str,
    content: str,
    message_type: str = "text",
    metadata: dict | None = None,
) -> AgentMessage:
    item = AgentMessage(
        thread_id=thread_id,
        role=role,
        content=content,
        message_type=message_type,
        metadata_json=metadata or {},
    )
    agent_repo.add_record(db, item)
    agent_repo.flush(db)
    return item


def _find_retry_source(db: Session, thread_id: str, body) -> AgentMessage:
    """重试时复用原来的用户消息，避免对话里出现两条一模一样的提问。"""
    user_message = agent_repo.get_message(db, body.retryMessageId)
    if (
        not user_message
        or user_message.thread_id != thread_id
        or user_message.role != "user"
        or user_message.content != body.message
    ):
        raise NotFoundError("找不到可重试的原始请求")
    return user_message


def prepare_turn(db: Session, thread: AgentThread, body, user_id: str, *, request_id: str | None = None) -> TurnContext:
    """落用户消息 + 建这一轮记录。已存在的 turn 由调用方先处理掉。"""
    attached_text = (body.contextText or "").strip()
    selection = resolve_prompt_selection(db, body, user_id)
    request_id = request_id or body.requestId or str(uuid.uuid4())
    request_hash = fingerprint_request(body)

    if body.retryMessageId:
        user_message = _find_retry_source(db, thread.id, body)
    else:
        user_message = save_agent_message(
            db,
            thread.id,
            "user",
            body.message,
            metadata={
                "hasAttachment": bool(attached_text),
                "attachedChars": len(attached_text),
                "longMessage": len(body.message) >= 500,
                "subtitleFileId": body.subtitleFileId,
                **selection.to_metadata(),
            },
        )
        if thread.title == "新对话":
            thread.title = body.message[:40]

    turn_request = AgentTurnRequest(
        thread_id=thread.id,
        request_id=request_id,
        request_hash=request_hash,
        user_message_id=user_message.id,
        status="processing",
    )
    agent_repo.add_record(db, turn_request)
    try:
        db.commit()
    except IntegrityError as exc:
        # 并发的同 id 请求可能先一步写入唯一约束，回滚后按重放处理。
        db.rollback()
        existing = agent_repo.find_turn(db, thread.id, request_id)
        if existing and existing.request_hash == request_hash:
            raise _ReplayRequired(existing) from exc
        raise ConflictError("该请求正在处理中") from exc

    return TurnContext(
        thread=thread,
        thread_id=thread.id,
        request_id=request_id,
        user_message=user_message,
        turn_request=turn_request,
        prompt_selection=selection,
        attached_text=attached_text,
    )


class _ReplayRequired(Exception):
    """内部信号：撞到并发唯一约束，且已有记录，应改为重放。"""

    def __init__(self, turn: AgentTurnRequest) -> None:
        self.turn = turn
        super().__init__("replay")


def plan_resume(db: Session, thread: AgentThread, body, ctx: TurnContext, active_interrupts: list, classify) -> ResumePlan:
    """根据当前中断点决定这一轮怎么跑。

    这是原实现里最长的一段判断，规则原样照搬：
    - 等 SRT：带了字幕就带着继续，没带就 409
    - 等确认：把用户这句话理解成「同意 / 改文案 / 改风格 / 取消 / 只是聊天」，
      并按理解结果更新对应的确认卡片与素材状态
    - 生图任务排队/执行中：不允许用文字推进，提示去点卡片上的取消
    - 其它中断：提示先处理当前卡片
    """
    plan = ResumePlan()
    if not active_interrupts:
        return plan

    pending = active_interrupts[0]
    kind = pending.get("kind")

    if kind == "wait_srt":
        if body.subtitleFileId:
            subtitle = subtitle_repo.get(db, body.subtitleFileId)
            if not subtitle or subtitle.workspace_id != thread.workspace_id:
                raise ValidationError("找不到当前工作区的 SRT 文件")
            if not subtitle.audio_object_path:
                raise ValidationError("请上传同时包含配音音频和 SRT 字幕的文件夹")
            plan.resume = {"subtitleFileId": body.subtitleFileId}
        else:
            raise ConflictError("流程正在等待 SRT，请先上传字幕文件再发送")
        return plan

    if kind in {"image_job", "image_preview_job"}:
        raise ConflictError("图片任务正在排队或执行，请使用任务卡片上的取消按钮")

    if not (kind or "").startswith("confirm_"):
        raise ConflictError("请先通过当前确认卡片继续或返回修改")

    pending_decision = classify(body.message, kind or "confirm_final", body.modelId)
    if pending_decision["kind"] == _DETACHED_CHAT:
        plan.detached_chat = True
        return plan

    if pending_decision["kind"] == "approve":
        plan.resume = {"decision": "approve"}
    elif pending_decision["kind"] == "cancel":
        plan.resume = {"decision": "revise"}
    else:
        target = (
            "text" if pending_decision["kind"] == "revise_text"
            else "style" if pending_decision["kind"] == "revise_style"
            else "workflow"
        )
        plan.resume_target = target
        plan.resume = {"decision": "revise", "instruction": body.message, "target": target}

    action = agent_repo.latest_pending_action(db, thread.id, f"graph_{kind}")
    if action:
        action.status = "confirmed" if plan.resume["decision"] == "approve" else "rejected"
        if kind == "confirm_images" and plan.resume["decision"] == "approve":
            for asset_id in (action.payload or {}).get("assetIds", []):
                asset = job_repo.get_asset(db, asset_id)
                if asset and asset.workspace_id == thread.workspace_id:
                    asset.status = "approved"
        db.commit()
        plan.resolved_confirmation = True
    return plan


def build_input_state(ctx: TurnContext, body, user_id: str) -> dict:
    """新输入时喂给图流程的初始 state。字段名与图节点约定一致，不能改。"""
    return {
        "thread_id": ctx.thread_id,
        "workspace_id": ctx.thread.workspace_id,
        "user_id": user_id,
        "model_id": body.modelId,
        "message": body.message,
        "current_message_id": ctx.user_message.id,
        "context_text": ctx.attached_text,
        "message_subtitle_file_id": body.subtitleFileId or "",
        **ctx.prompt_selection.to_graph_state(),
    }


def build_resume_state_update(ctx: TurnContext, user_id: str) -> dict:
    """续接时只刷新这几个字段，其余沿用检查点里的旧值。"""
    return {
        "user_id": user_id,
        **ctx.prompt_selection.to_graph_state(),
        "current_message_id": ctx.user_message.id,
    }


def persist_turn_result(db: Session, turn: AgentTurnRequest, message: dict | None) -> bool:
    """把这一轮的结果写回记录。返回是否成功（有可保存的消息）。

    失败分支返回 False 而不是抛异常：调用方要先给前端发一条 error 事件，
    再结束流，不能中途抛出把已经发出去的 started 事件晾在那里。
    """
    if message:
        turn.status = "completed"
        turn.response_message_id = message["id"]
        turn.error_message = None
        turn.error_category = None
        db.commit()
        return True
    turn.status = "failed"
    turn.error_message = "本次请求未返回可保存的消息，请重新发送。"
    turn.error_category = "request"
    db.commit()
    return False


def persist_turn_failure(
    db: Session,
    turn: AgentTurnRequest,
    error_message: str,
    error_category: str,
) -> None:
    turn.status = "failed"
    turn.error_message = error_message
    turn.error_category = error_category
    db.commit()
