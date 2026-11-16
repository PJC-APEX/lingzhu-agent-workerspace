"""Persist business messages and confirmation cards from graph outcomes."""
from __future__ import annotations
import logging
from app.domain.models import AgentAction, AgentMessage
from app.core.container import get_session_factory
from app.repositories import agent_repo
logger = logging.getLogger(__name__)

def persist_agent_graph_result(thread_id: str, result: dict) -> dict | None:
    """把图的回复或暂停点写进现有聊天消息和操作记录。"""
    state = result.get("state") or {}
    pending_items = result.get("interrupts") or []
    pending = pending_items[0] if pending_items else None
    reply = str(state.get("reply") or "")

    with get_session_factory()() as db:
        if pending:
            kind = pending.get("kind")
            if kind in {"image_job", "image_preview_job"}:
                job_id = str(pending.get("jobId") or "")
                if not job_id:
                    return None
                message = AgentMessage(
                    thread_id=thread_id,
                    role="assistant",
                    content="样图生成任务已提交。" if kind == "image_preview_job" else "图片生成任务已提交。",
                    message_type="job",
                    metadata_json={"jobId": job_id, "graphFlow": True},
                )
                agent_repo.add_record(db, message)
                db.commit()
                db.refresh(message)
                return {"id": message.id, "role": "assistant", "content": message.content, "type": message.message_type, "metadata": message.metadata_json}

            if kind == "wait_srt":
                message = AgentMessage(
                    thread_id=thread_id,
                    role="assistant",
                    content=pending.get("message") or "请上传已经与配音对齐的 SRT 字幕。",
                    message_type="text",
                    metadata_json={"flowStage": "waiting_srt"},
                )
                agent_repo.add_record(db, message)
                db.commit()
                db.refresh(message)
                return {"id": message.id, "role": "assistant", "content": message.content, "type": message.message_type, "metadata": message.metadata_json}

            action_payload = {
                "kind": kind,
                "assetIds": [asset.get("id") for asset in state.get("image_assets", []) if asset.get("id")],
            }
            if kind == "confirm_storyboard":
                action_payload["storyboardPrompt"] = {
                    "promptId": state.get("storyboard_used_prompt_id", state.get("storyboard_prompt_id")),
                    "versionId": state.get("storyboard_used_prompt_version_id", state.get("storyboard_prompt_version_id")),
                    "version": state.get("storyboard_used_prompt_version", state.get("storyboard_prompt_version")),
                }
            if kind == "confirm_image_style":
                action_payload["previewAssets"] = pending.get("assets", [])
                action_payload["imageStyle"] = pending.get("imageStyle", {})
                action_payload["imageStylePrompt"] = (pending.get("imageStyle") or {}).get("content", "")
            action = AgentAction(
                thread_id=thread_id,
                action_type=f"graph_{kind}",
                payload=action_payload,
            )
            agent_repo.add_record(db, action)
            agent_repo.flush(db)
            metadata = {"actionId": action.id, "actionType": action.action_type, "flowStage": state.get("stage")}
            selected_rewrite_prompt = state.get("rewrite_prompt_version_id")
            selected_storyboard_prompt = state.get("storyboard_prompt_version_id")
            if selected_rewrite_prompt:
                metadata["rewritePromptVersionId"] = selected_rewrite_prompt
            if selected_storyboard_prompt:
                metadata["storyboardPromptVersionId"] = selected_storyboard_prompt
            if kind == "confirm_final":
                metadata["previewText"] = pending.get("previewText", state.get("final_text", ""))
                content = "请确认这篇文案是否作为最终稿进入分镜。确认后才会调用分镜模型。"
            elif kind == "confirm_storyboard":
                scenes = pending.get("scenes", [])
                metadata["scenes"] = scenes
                metadata["storyboardPrompt"] = action_payload.get("storyboardPrompt")
                metadata["previewText"] = "\n\n".join(
                    f"分镜 {scene.get('sceneNumber')}\n旁白：{scene.get('narration')}\n画面：{scene.get('imagePrompt')}"
                    for scene in scenes
                )
                content = "请检查分镜。确认后才会开始生图；不满意时点返回修改，再告诉我需要重做的分镜。"
            elif kind == "confirm_image_style":
                metadata["previewAssets"] = pending.get("assets", [])
                metadata["imageStyle"] = pending.get("imageStyle", {})
                content = "请选择一张样图作为第一镜。确认后才会把正式图片保存到素材库，并生成其余分镜。"
            elif kind == "confirm_images":
                assets = pending.get("assets", [])
                metadata["previewAssets"] = assets
                metadata["imageStyle"] = state.get("image_style_used", {})
                content = "请检查生成图片。确认通过后请上传配音 SRT；不满意时点返回修改并指定图片编号。"
            else:
                content = reply or "请确认是否继续当前操作。"

            message = AgentMessage(
                thread_id=thread_id,
                role="assistant",
                content=content,
                message_type="confirmation",
                metadata_json=metadata,
            )
            agent_repo.add_record(db, message)
            db.commit()
            db.refresh(message)
            logger.info("event=agent_graph.confirmation_saved thread_id=%s kind=%s action_id=%s", thread_id, kind, action.id)
            return {"id": message.id, "role": "assistant", "content": message.content, "type": message.message_type, "metadata": metadata}

        if state.get("package_job_id"):
            metadata = {"mode": state.get("mode", "live"), "jobId": state["package_job_id"]}
            message_type = "job"
        elif state.get("draft_path"):
            metadata = {
                "mode": state.get("mode", "live"),
                "result": {"draftPath": state["draft_path"]},
            }
            message_type = "job_result"
        elif state.get("download_url"):
            metadata = {
                "mode": state.get("mode", "live"),
                "result": {
                    "exportId": state.get("export_id"),
                    "downloadUrl": state["download_url"],
                    "draftUrl": state.get("draft_url"),
                    "helperUrl": state.get("helper_url"),
                    "format": "lingzhu-timeline-v1",
                },
            }
            message_type = "job_result"
        else:
            metadata = {"mode": state.get("mode", "live"), "flowStage": state.get("stage", "chat")}
            if state.get("rewrite_used_prompt_id"):
                metadata["rewritePrompt"] = {
                    "promptId": state["rewrite_used_prompt_id"],
                    "versionId": state.get("rewrite_used_prompt_version_id"),
                    "version": state.get("rewrite_used_prompt_version"),
                }
            message_type = "text"

        if not reply and not state.get("download_url") and not state.get("draft_path"):
            return None
        message = AgentMessage(
            thread_id=thread_id,
            role="assistant",
            content=reply or "JSON 时间线已生成。",
            message_type=message_type,
            metadata_json=metadata,
        )
        agent_repo.add_record(db, message)
        db.commit()
        db.refresh(message)
        return {"id": message.id, "role": "assistant", "content": message.content, "type": message.message_type, "metadata": metadata}
