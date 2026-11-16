from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from langgraph.config import get_stream_writer
from langgraph.types import Command, interrupt

from app.core.config import PREVIEW_DIR
from app.graph.state import AgentGraphState, GeneratedAssetData, HumanDecision, ImageJobResult
from app.infrastructure.database import get_session_factory
from app.prompts.agent_prompt.intent import build_intent_prompt, build_pending_confirmation_prompt
from app.prompts.agent_prompt.rewrite import (
    DEFAULT_REWRITE_PROMPT_VERSION_ID,
    EXPLICIT_REWRITE_INSTRUCTION,
)
from app.prompts.agent_prompt.storyboard import DEFAULT_STORYBOARD_PROMPT_VERSION_ID
from app.prompts.agent_prompt.image_style import DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
from app.application.agent_context_service import prepare_agent_rewrite
from app.services.agent_rewrite.service import stream_agent_rewrite
from app.core.identity import current_user_id
from app.infrastructure import storage as object_storage
from app.services.aligner import AlignmentError, align_scenes_to_subtitles
from app.services.jianying_draft import create_jianying_draft
from app.services.model_gateway import ModelCallError, complete_chat, model_available
from app.services.storyboard import StoryboardOutputError, StoryboardSceneSchema, generate_storyboard, revise_storyboard_scene
from app.application import agent_prompt_service, agent_session_service, image_job_service

# 下面几个名字在本模块再导出，只为让测试里的 monkeypatch 继续有效
# （tests 会打桩 nodes.launch_job / nodes.record_job_dispatch）。
# 真正的业务编排在 app.application 层。
from app.application.image_job_service import launch_job  # noqa: F401
from app.repositories.outbox_repo import record_job_dispatch  # noqa: F401

logger = logging.getLogger(__name__)


_INTENT_VALUES = {
    "chat",
    "rewrite",
    "finalize_text",
    "finalize_and_generate_storyboard",
    "generate_storyboard",
    "regenerate_storyboard",
    "revise_storyboard",
    "revise_images",
    "retry_images",
    "storyboard_confirmation_help",
}
_SOURCE_VALUES = {"message", "attachment", "working_text", "saved_draft", "none"}
_OPERATION_INTENTS = {"revise_storyboard", "revise_images", "retry_images", "wait_srt", "export_timeline"}
_FINALIZE_INTENTS = {"finalize_text", "finalize_and_generate_storyboard"}


class IntentClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "chat", "rewrite", "finalize_text", "finalize_and_generate_storyboard",
        "generate_storyboard", "regenerate_storyboard", "revise_storyboard",
        "revise_images", "retry_images", "storyboard_confirmation_help",
    ]
    source: Literal["message", "attachment", "working_text", "saved_draft", "none"]
    copy_text: str = ""


class PendingConfirmationClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["approve", "cancel", "revise_text", "revise_workflow", "revise_style", "chat"]


def _source_copy_text(state: AgentGraphState, source: str, message_copy: str = "") -> str:
    if source == "message":
        copy_text = message_copy.strip()
        normalized_message = "".join((state.get("message") or "").split())
        normalized_copy = "".join(copy_text.split())
        return copy_text if normalized_copy and normalized_copy in normalized_message else ""
    if source == "attachment":
        return (state.get("context_text") or "").strip()
    if source == "working_text":
        return (state.get("working_text") or "").strip()
    if source == "saved_draft":
        return (state.get("draft_text") or "").strip()
    return ""


def classify_pending_confirmation(message: str, pending_kind: str, model_id: str) -> dict:
    if not model_available(model_id):
        return {"kind": "chat"}
    try:
        result = complete_chat(
            [{"role": "system", "content": build_pending_confirmation_prompt(message, pending_kind)}],
            model_id=model_id,
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = PendingConfirmationClassification.model_validate(json.loads(result.text))
        return {"kind": payload.kind}
    except Exception as exc:
        logger.warning("event=agent.confirmation_classification_fallback reason=%s", type(exc).__name__)
    return {"kind": "chat"}


def intent_node(state: AgentGraphState) -> AgentGraphState:
    """按当前消息做语义路由；文案内容只在明确定稿后写入草稿。"""
    message = (state.get("message") or "").strip()
    attached = (state.get("context_text") or "").strip()
    stage = state.get("stage", "chat")
    message_subtitle_file_id = state.get("message_subtitle_file_id")
    if message_subtitle_file_id:
        subtitle_update: AgentGraphState = {
            "subtitle_file_id": message_subtitle_file_id,
            "message_subtitle_file_id": "",
        }
    else:
        subtitle_update = {}
    if stage == "waiting_srt":
        return {
            **subtitle_update,
            "draft_path": "",
            "package_job_id": "",
            "intent": "export_timeline" if state.get("subtitle_file_id") or message_subtitle_file_id else "wait_srt",
        }
    intent = "chat"
    source = "none"
    message_copy = ""
    intent_source = "fallback"
    try:
        if not model_available(state.get("model_id", "deepseek-v4-flash")):
            raise RuntimeError("intent model unavailable")
        result = complete_chat(
            [{
                "role": "system",
                "content": build_intent_prompt(
                    message,
                    attached,
                    stage,
                    bool((state.get("draft_text") or "").strip()),
                    bool((state.get("working_text") or "").strip()),
                ),
            }],
            model_id=state.get("model_id", "deepseek-v4-flash"),
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = IntentClassification.model_validate(json.loads(result.text))
        candidate_intent = payload.intent
        candidate_source = payload.source
        message_copy = payload.copy_text.strip()
        if candidate_source == "none" and message_copy and candidate_intent in _FINALIZE_INTENTS | {"generate_storyboard"}:
            candidate_source = "message"
        if candidate_intent in _INTENT_VALUES:
            intent = candidate_intent
            source = candidate_source if candidate_source in _SOURCE_VALUES else "none"
            intent_source = "model"
    except Exception as exc:
        logger.warning("event=agent.intent_fallback reason=%s", type(exc).__name__)

    # A finalization without a known source cannot change canonical copy.
    if intent in _FINALIZE_INTENTS | {"rewrite"} and source == "none":
        intent = "chat"
    if intent == "rewrite" and source == "saved_draft" and not (state.get("draft_text") or "").strip():
        intent = "chat"
        source = "none"
    if intent in _FINALIZE_INTENTS | {"rewrite"} and source == "working_text" and not (state.get("working_text") or "").strip():
        intent = "chat"
        source = "none"
    if intent in _FINALIZE_INTENTS | {"rewrite"} and source == "attachment" and not attached:
        intent = "chat"
        source = "none"

    update: AgentGraphState = {
        **subtitle_update,
        "draft_path": "",
        "package_job_id": "",
        "intent": intent,
        "rewrite_source": source,
        "rewrite_requested": intent == "rewrite",
    }
    if intent in _FINALIZE_INTENTS:
        finalized_text = _source_copy_text(state, source, message_copy)
        if finalized_text:
            update["draft_text"] = finalized_text
            update["working_text"] = finalized_text
            update["pending_revisions"] = []
        else:
            update.update({"intent": "chat", "rewrite_source": "none", "rewrite_requested": False})
    elif intent == "generate_storyboard":
        storyboard_text = _source_copy_text(state, source, message_copy)
        if source == "message" and not storyboard_text:
            update.update({"intent": "chat", "rewrite_source": "none", "rewrite_requested": False})
        if not storyboard_text and source == "none":
            working_text = (state.get("working_text") or "").strip()
            saved_draft = (state.get("draft_text") or "").strip()
            if working_text:
                storyboard_text = working_text
                source = "working_text"
            elif saved_draft:
                storyboard_text = saved_draft
                source = "saved_draft"
        update["rewrite_source"] = source
        update["final_text"] = storyboard_text
        if storyboard_text and source in {"message", "attachment"}:
            update["working_text"] = storyboard_text
    logger.info(
        "event=agent.intent_resolved thread_id=%s intent=%s classifier=%s copy_source=%s message_len=%s",
        state.get("thread_id", "unknown"),
        intent,
        intent_source,
        source,
        len(message),
    )
    return update


def route_turn(state: AgentGraphState) -> str:
    intent = state.get("intent", "rewrite")
    if intent == "retry_images":
        return "launch_images"
    if intent in _OPERATION_INTENTS:
        return intent
    if intent == "generate_storyboard":
        if state.get("rewrite_source") in {"message", "attachment"}:
            return "prepare_final"
        return "storyboard" if (state.get("final_text") or "").strip() else "prepare_final"
    return {
        "confirm_text": "prepare_final",
        "regenerate_storyboard": "storyboard",
        "rewrite": "rewrite",
        "finalize_text": "prepare_final",
        "finalize_and_generate_storyboard": "prepare_final",
        "chat": "rewrite",
        "storyboard_confirmation_help": "storyboard_confirmation_help",
    }.get(intent, "rewrite")


def storyboard_confirmation_help_node(state: AgentGraphState) -> AgentGraphState:
    return {
        "stage": "chat",
        "pending": {},
        "reply": "当前没有待确认的分镜卡片。请先通过分镜流程生成分镜；生成后会出现可确认的分镜卡片。",
        "mode": "live",
    }


def rewrite_node(state: AgentGraphState) -> AgentGraphState:
    thread_id = state["thread_id"]
    message = state.get("message", "")
    intent = state.get("intent", "chat")
    source = state.get("rewrite_source", "none")
    if source == "working_text":
        draft_text = (state.get("working_text") or "").strip()
    elif source == "saved_draft":
        draft_text = (state.get("draft_text") or state.get("final_text") or "").strip()
    else:
        draft_text = ""
    context_text = state.get("context_text") if source == "attachment" else ""
    prompt_version_id = state.get("rewrite_prompt_version_id") or DEFAULT_REWRITE_PROMPT_VERSION_ID
    if intent == "rewrite" and prompt_version_id != DEFAULT_REWRITE_PROMPT_VERSION_ID:
        with get_session_factory()() as db:
            selected_prompt = agent_prompt_service.resolve_rewrite_prompt(db, prompt_version_id, state.get("user_id") or current_user_id())
    else:
        selected_prompt = agent_prompt_service.default_rewrite_prompt()
    prepared = prepare_agent_rewrite(
        thread_id,
        message,
        context_text,
        state.get("model_id", "deepseek-v4-flash"),
        current_message_id=state.get("current_message_id"),
        draft_text=draft_text,
        pending_revisions=[],
        custom_instruction=selected_prompt.content if state.get("rewrite_requested") else "",
        prompt_id=selected_prompt.prompt_id,
        prompt_version_id=selected_prompt.version_id,
        prompt_version=selected_prompt.version,
    )
    if prepared.estimated_tokens > prepared.token_budget:
        return {"reply": "本轮内容超过智能体 token 预算，请缩短文案或分段发送。", "mode": "error", "stage": "chat", "rewrite_requested": False}

    if intent == "rewrite":
        prepared.prompt_messages[0]["content"] += EXPLICIT_REWRITE_INSTRUCTION

    writer = get_stream_writer()
    parts: list[str] = []
    for delta in stream_agent_rewrite(prepared):
        parts.append(delta)
        writer({"type": "message.delta", "delta": delta})
    reply = "".join(parts)
    logger.info("event=agent_graph.rewrite_completed thread_id=%s model_id=%s chars=%s", thread_id, prepared.model_id, len(reply))
    update: AgentGraphState = {
        "reply": reply,
        "mode": prepared.mode,
        "stage": "chat",
        "rewrite_requested": False,
        "rewrite_prompt_id": selected_prompt.prompt_id,
        "rewrite_prompt_version": selected_prompt.version,
        "rewrite_used_prompt_id": selected_prompt.prompt_id if state.get("rewrite_requested") else "agent_rewrite_default_v1",
        "rewrite_used_prompt_version_id": selected_prompt.version_id if state.get("rewrite_requested") else DEFAULT_REWRITE_PROMPT_VERSION_ID,
        "rewrite_used_prompt_version": selected_prompt.version,
    }
    if intent == "rewrite" and prepared.mode == "live" and reply.strip():
        update["working_text"] = reply.strip()
    return update


def prepare_final_node(state: AgentGraphState) -> AgentGraphState:
    if state.get("intent") == "generate_storyboard":
        final_text = (state.get("final_text") or "").strip()
    else:
        final_text = (state.get("draft_text") or "").strip()
    if not final_text:
        return {
            "stage": "chat",
            "reply": "请先在智能体对话中发送或改写最终文案，再告诉我生成分镜。",
            "mode": "live",
        }
    logger.info("event=agent_graph.final_draft_ready thread_id=%s chars=%s", state["thread_id"], len(final_text))
    return {
        "final_text": final_text,
        "stage": "confirm_final",
        "pending": {"kind": "confirm_final", "previewText": final_text},
        "reply": "请确认下面这篇文案是否作为最终稿进入分镜。",
    }


def confirm_final_node(state: AgentGraphState) -> AgentGraphState:
    decision = HumanDecision.model_validate(interrupt(state["pending"]))
    if decision.decision == "approve":
        final_text = (state.get("final_text") or "").strip()
        return {
            "draft_text": final_text,
            "working_text": final_text,
            "stage": "storyboard",
            "reply": "文案已确认，开始生成分镜。",
        }
    if decision.instruction.strip():
        return Command(
            goto="rewrite",
            update={
                "stage": "chat",
                "message": decision.instruction.strip(),
                "context_text": "",
                "working_text": state.get("final_text") or state.get("draft_text", ""),
                "reply": "",
                "rewrite_requested": True,
                "intent": "rewrite",
                "rewrite_source": "working_text",
            },
        )
    return {
        "stage": "chat",
        "reply": "好的，先不生成分镜。你可以继续告诉我需要怎样修改文案。",
        "pending": {},
        "working_text": state.get("draft_text", ""),
        "final_text": state.get("draft_text", ""),
    }


def storyboard_node(state: AgentGraphState) -> AgentGraphState:
    prompt_version_id = state.get("storyboard_prompt_version_id") or DEFAULT_STORYBOARD_PROMPT_VERSION_ID
    with get_session_factory()() as db:
        selected_prompt = agent_prompt_service.resolve_storyboard_prompt(db, prompt_version_id, state.get("user_id") or current_user_id())
    try:
        result = generate_storyboard(
            state["final_text"],
            state.get("model_id", "deepseek-v4-flash"),
            custom_instruction=selected_prompt.content,
            prompt_id=selected_prompt.prompt_id,
            prompt_version_id=selected_prompt.version_id,
            prompt_version=selected_prompt.version,
        )
    except StoryboardOutputError as exc:
        logger.warning("event=agent_graph.storyboard_recoverable_failure thread_id=%s reason=%s", state["thread_id"], str(exc))
        return {
            "stage": "storyboard_generation_failed",
            "pending": {},
            "reply": f"分镜生成没有通过格式和文案完整性检查（已自动重试一次）。文案仍已保留；请重新发送“重新生成分镜”再试。原因：{exc}",
            "mode": "error",
        }
    except ModelCallError:
        raise
    except Exception as exc:
        logger.exception("event=agent_graph.storyboard_model_failure thread_id=%s", state["thread_id"])
        return {
            "stage": "chat",
            "pending": {},
            "reply": f"分镜服务暂时无法完成本次生成，文案和定稿均已保留。请稍后重新发送“重新生成分镜”。原因：{type(exc).__name__}",
            "mode": "error",
        }
    validated = [StoryboardSceneSchema.model_validate(scene).model_dump() for scene in result.scenes]
    logger.info(
        "event=agent_graph.storyboard_ready thread_id=%s scenes=%s prompt_id=%s prompt_version=%s",
        state["thread_id"],
        len(validated),
        selected_prompt.prompt_id,
        selected_prompt.version,
    )
    return {
        "storyboard": validated,
        "stage": "confirm_storyboard",
        "pending": {"kind": "confirm_storyboard", "scenes": validated},
        "storyboard_prompt_id": selected_prompt.prompt_id,
        "storyboard_prompt_version": selected_prompt.version,
        "storyboard_used_prompt_id": selected_prompt.prompt_id,
        "storyboard_used_prompt_version_id": selected_prompt.version_id,
        "storyboard_used_prompt_version": selected_prompt.version,
        "reply": "分镜已整理完成，请检查每个分镜的旁白和画面提示词。",
    }


def confirm_storyboard_node(state: AgentGraphState) -> AgentGraphState:
    decision = HumanDecision.model_validate(interrupt(state["pending"]))
    if decision.decision == "approve":
        return {
            "stage": "image_preview_generation",
            "reply": "分镜已确认，先为第一镜生成两张风格样图。",
            "pending": {},
        }
    if decision.instruction.strip():
        if decision.target == "text":
            return Command(
                goto="rewrite",
                update={
                    "stage": "chat",
                    "message": decision.instruction.strip(),
                    "context_text": "",
                    "working_text": state.get("final_text") or state.get("draft_text", ""),
                    "reply": "",
                    "pending": {},
                    "rewrite_requested": True,
                    "intent": "rewrite",
                    "rewrite_source": "working_text",
                },
            )
        numbers = _requested_scene_numbers(decision.instruction, state.get("storyboard", []))
        if len(numbers) == 1:
            try:
                revised = revise_storyboard_scene(
                    state["storyboard"],
                    numbers[0],
                    decision.instruction,
                    state.get("model_id", "deepseek-v4-flash"),
                )
            except Exception as exc:
                logger.exception("event=agent_graph.confirmed_storyboard_revision_failed thread_id=%s scene=%s", state["thread_id"], numbers[0])
                return {
                    "storyboard": state.get("storyboard", []),
                    "stage": "confirm_storyboard",
                    "pending": {"kind": "confirm_storyboard", "scenes": state.get("storyboard", [])},
                    "reply": f"分镜修改没有完成，原分镜已保留，可以重新尝试。原因：{type(exc).__name__}",
                    "mode": "error",
                }
            return Command(
                goto="confirm_storyboard",
                update={
                    "storyboard": revised,
                    "stage": "confirm_storyboard",
                    "pending": {"kind": "confirm_storyboard", "scenes": revised},
                    "reply": f"第 {numbers[0]} 个分镜已重新整理，请检查修改后的分镜。",
                },
            )
    return {
        "stage": "revise_storyboard",
        "reply": "请告诉我需要重做第几分镜，以及希望怎样调整。",
        "pending": {},
        "working_text": state.get("draft_text", ""),
        "final_text": state.get("draft_text", ""),
    }


def launch_image_preview_node(state: AgentGraphState) -> AgentGraphState:
    first_scene = min(state.get("storyboard", []), key=lambda scene: int(scene["sceneNumber"]))
    with get_session_factory()() as db:
        job = image_job_service.start_image_preview_job(
            db,
            workspace_id=state["workspace_id"],
            thread_id=state["thread_id"],
            state=state,
            first_scene=first_scene,
            # 透传本模块的同名符号，测试对其打桩后仍能覆盖到 application 层
            record_dispatch=record_job_dispatch,
            launcher=launch_job,
        )
    return {"image_job_id": job.id, "stage": "waiting_image_preview"}


def wait_image_preview_node(state: AgentGraphState) -> AgentGraphState:
    result = ImageJobResult.model_validate(interrupt({
        "kind": "image_preview_job",
        "jobId": state["image_job_id"],
        "sceneNumbers": [int(min(state["storyboard"], key=lambda scene: int(scene["sceneNumber"]))["sceneNumber"])],
    }))
    if result.error:
        return {
            "stage": "confirm_storyboard",
            "reply": f"样图生成失败：{result.error}。确认分镜后可以重试。",
        }
    previews = [asset.model_dump() for asset in result.assets]
    if len(previews) != 2 or {item.get("previewIndex") for item in previews} != {0, 1}:
        raise ValueError("样图任务必须返回两张可选图片")
    style = {
        "promptId": state.get("image_style_prompt_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
        "versionId": state.get("image_style_prompt_version_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
        "version": state.get("image_style_prompt_version", 1),
        "content": state.get("image_style_prompt", ""),
    }
    return {
        "image_previews": previews,
        "image_job_id": result.jobId,
        "image_style_snapshot": style,
        "stage": "confirm_image_style",
        "pending": {"kind": "confirm_image_style", "assets": previews, "imageStyle": style},
        "reply": "请选择一张样图作为第一镜。可以直接使用当前风格，也可以修改风格后重新生成样图。",
    }


def confirm_image_style_node(state: AgentGraphState) -> AgentGraphState:
    decision = HumanDecision.model_validate(interrupt(state["pending"]))
    if decision.decision == "revise" and decision.target == "style":
        snapshot = state.get("image_style_snapshot") or {}
        instruction = decision.instruction.strip()
        if snapshot.get("content") and not decision.replaceStyle:
            instruction = f"{snapshot['content'].strip()}\n\n额外风格要求：{instruction}"
        return Command(
            goto="launch_image_preview",
            update={
                "image_style_prompt": instruction,
                "image_style_prompt_id": snapshot.get("promptId", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
                "image_style_prompt_version_id": snapshot.get("versionId", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
                "image_style_prompt_version": snapshot.get("version", 1),
                "stage": "image_preview_generation",
                "reply": "正在按修改后的风格重新生成两张样图。",
            },
        )
    if decision.decision == "approve":
        previews = state.get("image_previews", [])
        selected_index = decision.selectedPreviewIndex
        selected = next((item for item in previews if item.get("previewIndex") == selected_index), None)
        if not selected:
            raise ValueError("请先选择一张样图")
        return {
            "selected_preview": selected,
            "regen_scene_numbers": [
                int(scene["sceneNumber"])
                for scene in state.get("storyboard", [])
                if int(scene["sceneNumber"]) != int(selected["sceneNumber"])
            ],
            "stage": "image_generation",
            "reply": "风格已确认，开始生成其余分镜图片。",
        }
    for preview in state.get("image_previews", []):
        reference = str(preview.get("objectPath") or "")
        try:
            preview_path = object_storage.local_path(reference)
            if preview_path and preview_path.resolve().parent == Path(PREVIEW_DIR).resolve():
                preview_path.unlink(missing_ok=True)
                if preview_path.parent.resolve() != Path(PREVIEW_DIR).resolve():
                    try:
                        preview_path.parent.rmdir()
                    except OSError:
                        pass
            elif object_storage.object_key_from_reference(reference):
                object_storage.delete_reference(reference)
        except (OSError, object_storage.ObjectStorageError):
            logger.warning("event=agent_graph.image_preview_cleanup_failed file=%s", Path(reference).name)
    return {
        "stage": "confirm_storyboard",
        "pending": {},
        "image_previews": [],
        "reply": "已返回分镜确认。",
    }


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if value.startswith("十"):
        return 10 + digits.get(value[1:], 0)
    if "十" in value:
        tens, units = value.split("十", 1)
        return digits.get(tens, 0) * 10 + digits.get(units, 0)
    if len(value) == 1:
        return digits.get(value)
    return None


def _requested_scene_numbers(message: str, scenes: list[dict]) -> list[int]:
    matched = re.findall(r"第\s*([0-9一二两三四五六七八九十]+)\s*(?:个)?\s*(?:分镜|镜头|张)", message)
    numbers = sorted({_chinese_number(item) for item in matched} - {None})
    valid = {int(scene["sceneNumber"]) for scene in scenes}
    return [number for number in numbers if number in valid]


def revise_storyboard_node(state: AgentGraphState) -> AgentGraphState:
    scenes = state.get("storyboard", [])
    numbers = _requested_scene_numbers(state.get("message", ""), scenes)
    if len(numbers) != 1:
        return {"stage": "revise_storyboard", "reply": "请一次指定一个分镜，例如“第五分镜的画面改成夜景”。"}
    try:
        revised = revise_storyboard_scene(scenes, numbers[0], state.get("message", ""), state.get("model_id", "deepseek-v4-flash"))
    except StoryboardOutputError as exc:
        logger.warning("event=agent_graph.storyboard_revision_recoverable_failure thread_id=%s scene=%s reason=%s", state["thread_id"], numbers[0], str(exc))
        return {
            "stage": "chat",
            "reply": f"分镜画面提示词修改没有通过格式检查（已自动重试一次），原分镜保持不变。请重新提出修改要求。原因：{exc}",
            "mode": "error",
        }
    except ModelCallError:
        raise
    except Exception as exc:
        logger.exception("event=agent_graph.storyboard_revision_model_failure thread_id=%s scene=%s", state["thread_id"], numbers[0])
        return {
            "stage": "confirm_storyboard",
            "storyboard": scenes,
            "pending": {"kind": "confirm_storyboard", "scenes": scenes},
            "reply": f"分镜修改没有完成，原分镜已保留，可以重新尝试。原因：{type(exc).__name__}",
            "mode": "error",
        }
    logger.info("event=agent_graph.storyboard_scene_revised thread_id=%s scene=%s", state["thread_id"], numbers[0])
    return {
        "storyboard": revised,
        "stage": "confirm_storyboard",
        "pending": {"kind": "confirm_storyboard", "scenes": revised},
        "reply": f"第 {numbers[0]} 个分镜已重新整理，请检查修改后的分镜。",
    }


def launch_images_node(state: AgentGraphState) -> AgentGraphState:
    if state.get("selected_preview"):
        scene_numbers = list(state.get("regen_scene_numbers", []))
    else:
        scene_numbers = state.get("regen_scene_numbers") or [int(scene["sceneNumber"]) for scene in state["storyboard"]]
    with get_session_factory()() as db:
        job = image_job_service.start_image_generation_job(
            db,
            workspace_id=state["workspace_id"],
            thread_id=state["thread_id"],
            state=state,
            storyboard=state["storyboard"],
            scene_numbers=scene_numbers,
            # 透传本模块的同名符号，测试对其打桩后仍能覆盖到 application 层
            record_dispatch=record_job_dispatch,
            launcher=launch_job,
        )

    logger.info("event=agent_graph.image_job_started thread_id=%s job_id=%s scenes=%s", state["thread_id"], job.id, scene_numbers)
    return {"image_job_id": job.id, "stage": "waiting_image_job"}


def wait_image_job_node(state: AgentGraphState) -> AgentGraphState:
    job_id = state["image_job_id"]
    scene_numbers = [int(scene["sceneNumber"]) for scene in state["storyboard"]] if state.get("selected_preview") else state.get("regen_scene_numbers") or [int(scene["sceneNumber"]) for scene in state["storyboard"]]
    result = interrupt({"kind": "image_job", "jobId": job_id, "sceneNumbers": scene_numbers})
    checked = ImageJobResult.model_validate(result)
    if checked.error:
        logger.error("event=agent_graph.image_job_failed thread_id=%s job_id=%s error=%s", state["thread_id"], checked.jobId, checked.error)
        return {
            "image_job_id": checked.jobId,
            "stage": "image_generation_failed",
            "reply": "图片生成任务已取消。需要时可以重新确认分镜后再试。" if checked.error == "任务已取消" else f"图片生成失败：{checked.error}。确认 API 配置后可以重试。",
            "pending": {},
        }
    current = {int(asset["sceneNumber"]): asset for asset in state.get("image_assets", [])}
    for asset in checked.assets:
        if asset.jobId and asset.jobId != checked.jobId:
            raise ValueError("图片结果中的后台任务编号不一致")
        current[asset.sceneNumber] = asset.model_dump()
    merged = [current[number] for number in sorted(current)]
    expected_numbers = {int(scene["sceneNumber"]) for scene in state.get("storyboard", [])}
    if {int(asset["sceneNumber"]) for asset in merged} != expected_numbers:
        raise ValueError("图片结果与确认后的分镜不匹配")
    return {
        "image_job_id": checked.jobId,
        "image_assets": merged,
        "stage": "confirm_images",
        "pending": {"kind": "confirm_images", "assets": merged},
        "regen_scene_numbers": [],
        "image_style_used": {
            "promptId": state.get("image_style_prompt_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
            "versionId": state.get("image_style_prompt_version_id", DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID),
            "version": state.get("image_style_prompt_version", 1),
            "content": state.get("image_style_prompt", ""),
        },
        "reply": "图片已生成，请检查质量；通过后我会等待你上传配音 SRT。",
    }


def confirm_images_node(state: AgentGraphState) -> AgentGraphState:
    decision = HumanDecision.model_validate(interrupt(state["pending"]))
    if decision.decision == "approve":
        return {"stage": "waiting_srt", "pending": {}, "reply": "图片质量已确认，请上传剪映导出的配音 SRT 字幕。"}
    if decision.instruction.strip():
        return Command(
            goto="revise_images",
            update={"stage": "revise_images", "message": decision.instruction.strip(), "pending": {}},
        )
    return {"stage": "revise_images", "pending": {}, "reply": "请告诉我需要重做的图片编号和调整方向。"}


def revise_images_node(state: AgentGraphState) -> AgentGraphState:
    message = state.get("message", "")
    scenes = state.get("storyboard", [])
    numbers = _requested_scene_numbers(message, scenes)
    if not numbers:
        return {"stage": "revise_images", "reply": "请指定要重做的图片，例如“第五张重新生成，画面改亮一些”。"}

    instruction = message.strip()
    agent_session_service.reject_assets(
        [
            asset["id"]
            for asset in state.get("image_assets", [])
            if int(asset["sceneNumber"]) in numbers and asset.get("id")
        ]
    )
    revised = []
    for scene in scenes:
        current = dict(scene)
        if int(current["sceneNumber"]) in numbers:
            current["imagePrompt"] = f"{current['imagePrompt']}\n用户补充的画面调整要求：{instruction}"
        revised.append(current)
    logger.info("event=agent_graph.image_revision_requested thread_id=%s scenes=%s", state["thread_id"], numbers)
    return {"storyboard": revised, "regen_scene_numbers": numbers, "stage": "image_generation", "reply": f"准备重新生成第 {', '.join(map(str, numbers))} 张图片。"}


def wait_srt_node(state: AgentGraphState) -> AgentGraphState:
    if state.get("subtitle_file_id"):
        return {"stage": "timeline_export"}
    result = interrupt({"kind": "wait_srt", "message": "请上传同时包含配音音频和 SRT 字幕的文件夹。"})
    subtitle_file_id = str(result.get("subtitleFileId", "")) if isinstance(result, dict) else ""
    if not subtitle_file_id:
        return {"stage": "waiting_srt", "reply": "没有收到 SRT 文件，请上传后再继续。"}
    return {"subtitle_file_id": subtitle_file_id, "stage": "timeline_export"}


def assemble_jianying_draft_node(state: AgentGraphState) -> AgentGraphState:
    from app.core.config import JIANYING_DRAFT_DIR

    subtitle = agent_session_service.get_subtitle_snapshot(state["subtitle_file_id"], state["workspace_id"])
    try:
        alignment = align_scenes_to_subtitles(state["storyboard"], subtitle.cues)
    except AlignmentError as exc:
        logger.warning("event=agent_graph.subtitle_alignment_rejected thread_id=%s subtitle_id=%s reason=%s", state["thread_id"], subtitle.id, str(exc))
        return {
            "stage": "waiting_srt",
            "subtitle_file_id": "",
            "reply": f"{exc}。请重新上传与当前分镜对应的 SRT 后再试。",
            "mode": "error",
        }
    assets = [
        {"sceneNumber": asset["sceneNumber"], "fileName": asset["fileName"], "objectPath": asset["objectPath"]}
        for asset in state.get("image_assets", [])
    ]
    from app.core.config import JIANYING_EXPORT_MODE
    if JIANYING_EXPORT_MODE == "package":
        from app.application.jianying_export_service import submit_export
        job_id = submit_export(
            thread_id=state["thread_id"], workspace_id=state["workspace_id"],
            image_job_id=state["image_job_id"], subtitle_file_id=state["subtitle_file_id"],
            user_id=state.get("user_id") or current_user_id() or "",
        )
        return {"stage": "completed", "package_job_id": job_id, "draft_path": "", "download_url": "",
                "reply": "正在准备剪映素材包。任务完成后，下载素材包并使用 Windows 导入工具生成本机草稿。", "mode": "live"}
    references = [subtitle.object_path, subtitle.audio_object_path or "", *(asset["objectPath"] for asset in assets)]
    if any(object_storage.object_key_from_reference(reference) for reference in references):
        with tempfile.TemporaryDirectory(prefix="lingzhu-jianying-input-") as temp_name:
            temp_dir = Path(temp_name)
            subtitle_path = object_storage.download_reference(subtitle.object_path, temp_dir / Path(subtitle.file_name).name)
            audio_path = object_storage.download_reference(
                subtitle.audio_object_path or "",
                temp_dir / Path(subtitle.audio_file_name or "voiceover.bin").name,
            )
            local_assets = []
            for index, asset in enumerate(assets):
                local_path = temp_dir / f"asset-{index}-{Path(asset['fileName']).name}"
                object_storage.download_reference(asset["objectPath"], local_path)
                local_assets.append({**asset, "objectPath": str(local_path)})
            exported = create_jianying_draft(
                alignment,
                local_assets,
                subtitle_path,
                audio_path,
                JIANYING_DRAFT_DIR,
            )
    else:
        exported = create_jianying_draft(
            alignment,
            assets,
            subtitle.object_path,
            subtitle.audio_object_path or "",
            JIANYING_DRAFT_DIR,
        )

    logger.info("event=agent_graph.jianying_draft_ready thread_id=%s path=%s", state["thread_id"], exported.path)
    return {
        "stage": "completed",
        "draft_path": str(exported.path),
        "reply": f"SRT 已解析并与已确认图片完成配对，剪映草稿已生成：{exported.path}。请在剪映专业版中打开并检查。",
        "mode": "live",
    }
