"""媒体任务的执行器。

这个模块当前还承担着「任务执行期」的全部职责，包括：

- 执行权抢占（用 execution_token 保证同一任务只有一个执行者）
- 心跳上报与失联判定
- 失败重试与死信（_retry_or_dead_letter）
- 完成回调与收尾副作用（_finish_job_side_effects）
- 过期任务与失败恢复的扫描补偿（recover_media_jobs）

尚未下沉到 application 层的是上面这些「执行期状态机」。
已经下沉的：任务增删查改（application/job_service.py）、
outbox 投递状态机（infrastructure/outbox.py）、
图流程恢复（application/agent_image_flow.py）。

本模块底部那些两行的函数（launch_job / _dispatch_outbox_event 等）
是转发入口，保留在原位是为了让调用方与测试的引用不断。
"""

from __future__ import annotations

import logging
import mimetypes
import re
import shutil
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents.tools.image_generation_tool import create_demo_image
from app.agents.tools.storyboard_tool import create_storyboard
from app.core.config import ASSET_DIR, EXPORT_DIR, JOB_HEARTBEAT_SECONDS, JOB_MAX_ATTEMPTS, JOB_STALE_SECONDS, PREVIEW_DIR
from app.domain.models import AgentAction, AgentMessage, Asset, ExportFile, Job, SubtitleFile, Workspace
from app.infrastructure.database import SessionLocal
from app.infrastructure import outbox, queue
from app.application import job_execution_service as execution
from app.application import job_recovery_service as recovery
from app.application import job_finish_service as finish
from app.repositories.outbox_repo import record_job_dispatch
from app.services.aligner import align_scenes_to_subtitles
from app.services.image_gen import compose_image_prompt, generate_scene_image
from app.services.image_gen import ImageGenerationError
from app.infrastructure import storage as object_storage
from app.services.jianying import write_timeline
from app.services.resource_urls import asset_content_url, preview_content_url
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
_current_job_token: ContextVar[str | None] = ContextVar("current_job_token", default=None)


def _update_job(job_id: str, **values) -> None:
    """按令牌校验后更新任务字段。状态机在 application/job_execution_service.py。"""
    with SessionLocal() as db:
        execution.update_job(db, job_id, _current_job_token.get(), **values)


def _finish_cancel_request(job_id: str) -> bool:
    with SessionLocal() as db:
        return execution.finish_cancel_request(db, job_id)


def _mark_job_completed(job_id: str, result: dict, *, resume_workflow: bool) -> bool:
    with SessionLocal() as db:
        return execution.mark_completed(
            db, job_id, result, _current_job_token.get(), resume_workflow=resume_workflow
        )


def _job_heartbeat(job_id: str, token: str) -> threading.Event:
    """起心跳线程，返回停止信号。实现在 application/job_execution_service.py。

    只传本模块的 SessionLocal：测试对 `local_tasks.SessionLocal` 的打桩因此
    仍然生效，而心跳「什么时候算断、什么时候退出」的规则只有 application 一份。

    签名变化说明：原来由调用方自己建 Event 再传进来，现在由这里建并返回。
    线程的停止条件与刷新频率都在 application 里，调用方只需要「拿到信号、最后 set()」。
    """
    return execution.start_heartbeat(
        SessionLocal,
        job_id,
        token,
        interval=JOB_HEARTBEAT_SECONDS,
    )


def _save_partial_job_result(job_id: str, token: str, result: dict) -> bool:
    with SessionLocal() as db:
        return execution.save_partial_result(db, job_id, token, result)


def _owns_job_execution(db, job_id: str, token: str) -> bool:
    return execution.owns_execution(db, job_id, token)


def _retry_or_dead_letter(job_id: str, exc: Exception) -> str:
    with SessionLocal() as db:
        return execution.retry_or_dead_letter(db, job_id, _current_job_token.get(), exc)


def _finish_job_side_effects(job_id: str) -> None:
    """写结果消息并唤醒对话流程。幂等，收尾重试安全。

    收尾状态机在 application/job_finish_service.py。这里把本模块的两样东西
    注入下去：
    - SessionLocal：测试对 `local_tasks.SessionLocal` 的打桩继续生效
    - _resume_agent_image_flow：测试对 `local_tasks._resume_agent_image_flow`
      的打桩继续生效，同时避免 application 反过来 import workers。
    """
    finish.finish_side_effects(
        SessionLocal,
        job_id,
        resume_image_flow=_resume_agent_image_flow,
    )


def _as_utc(value: datetime) -> datetime:
    """兼容入口。实现在 application/job_execution_service.py。"""
    return execution.as_utc(value)


def _remove_preview_files(previews: list[dict]) -> None:
    preview_root = Path(PREVIEW_DIR).resolve()
    for preview in previews:
        reference = str(preview.get("objectPath") or "")
        try:
            path = object_storage.local_path(reference)
            if path and path.resolve().parent == preview_root:
                path.unlink(missing_ok=True)
                if path.parent.resolve() != preview_root:
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
            elif object_storage.object_key_from_reference(reference):
                object_storage.delete_reference(reference)
        except (OSError, object_storage.ObjectStorageError):
            logger.warning("event=image_preview.cleanup_failed file=%s", Path(reference).name)


def _store_generated_file(
    path: Path,
    workspace_id: str,
    file_name: str,
    collection: str,
    stable_suffix: str | None = None,
) -> str:
    with SessionLocal() as db:
        workspace = db.get(Workspace, workspace_id)
        owner_id = workspace.owner_user_id if workspace else "unknown"
    media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    reference = object_storage.persist_file(
        path,
        f"{collection}/{owner_id}/{workspace_id}/{stable_suffix or file_name}",
        media_type,
    )
    if reference.startswith("storage://"):
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return reference


def _parse_srt(text: str) -> list[dict]:
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), None)
        if not time_line:
            continue
        start, end = [part.strip() for part in time_line.split("-->", 1)]
        body = " ".join(line for line in lines if line != time_line and not line.isdigit())
        if body:
            cues.append({"start": start, "end": end, "text": body})
    return cues


def _resume_agent_image_flow(thread_id: str, job_id: str, assets: list[dict], error: str = "") -> None:
    """兼容入口。实现在 application/agent_image_flow.py。

    保留这个同名转发是因为调用方（routes、本模块内部）都按这个名字引用，
    测试也按 `routes._resume_agent_image_flow` 打桩。
    """
    from app.application.agent_image_flow import resume_agent_image_flow

    resume_agent_image_flow(thread_id, job_id, assets, error)


def run_job(job_id: str) -> None:
    """由 Celery worker（后台任务进程）执行一个生图或视频任务。"""
    execution_token = str(uuid4())
    with SessionLocal() as db:
        job = execution.claim_job(db, job_id, execution_token)
        if job is None:
            logger.info("event=job.skipped job_id=%s reason=not_queued", job_id)
            return
        job_type = job.job_type
        payload = dict(job.payload or {})
        workspace_id = job.workspace_id
        thread_id = job.thread_id
    token_context = _current_job_token.set(execution_token)
    heartbeat_stop = _job_heartbeat(job_id, execution_token)
    logger.info("后台任务开始，job_id=%s, type=%s", job_id, job_type)
    try:
        if _finish_cancel_request(job_id):
            if job_type == "image_generation" and thread_id and payload.get("agentGraph"):
                _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
            if payload.get("agentGraphPreview"):
                _remove_preview_files(payload.get("previousPreviews", []))
            return
        if job_type == "image_generation":
            text = payload.get("finalText", "")
            scenes = payload.get("storyboard") or create_storyboard(text)
            style_prompt = payload.get("imageStylePrompt", "")
            selected_preview = payload.get("selectedPreview")
            selected_numbers = {int(value) for value in payload.get("sceneNumbers", [])}
            if not selected_numbers and not selected_preview:
                selected_numbers = {int(scene.get("sceneNumber", index)) for index, scene in enumerate(scenes, start=1)}
            selected_scenes = [
                scene for index, scene in enumerate(scenes, start=1)
                if int(scene.get("sceneNumber", index)) in selected_numbers
            ]
            if not selected_scenes and not selected_preview:
                raise ValueError("图片任务没有找到指定分镜")
            created_assets = []
            if payload.get("agentGraphPreview"):
                first_scene = selected_scenes[0]
                with SessionLocal() as db:
                    current_job = db.get(Job, job_id)
                    previous_result = dict(current_job.result or {}) if current_job else {}
                preview_assets = list(previous_result.get("assets") or [])
                for preview_index in range(int(payload.get("previewCount", 2))):
                    if any(int(item.get("previewIndex", -1)) == preview_index for item in preview_assets):
                        continue
                    if _finish_cancel_request(job_id):
                        _remove_preview_files(preview_assets)
                        if thread_id and payload.get("agentGraph"):
                            _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
                        return
                    scene_number = int(first_scene.get("sceneNumber", 1))
                    prompt = compose_image_prompt(first_scene["imagePrompt"], style_prompt)
                    generated = generate_scene_image(scene_number, prompt, output_dir=PREVIEW_DIR)
                    suffix = Path(generated.file_name).suffix
                    object_ref = _store_generated_file(
                        Path(generated.object_path), workspace_id, generated.file_name, "previews",
                        f"jobs/{job_id}/preview-{preview_index}{suffix}",
                    )
                    preview_assets.append({
                        "id": f"preview-{job_id}-{preview_index}",
                        "jobId": job_id,
                        "sceneNumber": scene_number,
                        "previewIndex": preview_index,
                        "fileName": generated.file_name,
                        "objectPath": object_ref,
                        "url": preview_content_url(job_id, preview_index),
                        "status": "needs_review",
                        "isDemo": False,
                        "prompt": generated.prompt,
                    })
                    if not _save_partial_job_result(job_id, execution_token, {
                        "assets": preview_assets,
                        "storyboard": scenes,
                        "mode": "live",
                        "imageStylePromptId": payload.get("imageStylePromptId"),
                        "imageStylePromptVersionId": payload.get("imageStylePromptVersionId"),
                        "imageStylePromptVersion": payload.get("imageStylePromptVersion"),
                    }):
                        return
                    _update_job(job_id, progress=15 + (preview_index + 1) * 35, message=f"样图已生成 {preview_index + 1}/2")
                for old_preview in payload.get("previousPreviews", []):
                    _remove_preview_files([old_preview])
                created_assets = preview_assets
                result = {
                    "assets": preview_assets,
                    "storyboard": scenes,
                    "mode": "live",
                    "imageStylePromptId": payload.get("imageStylePromptId"),
                    "imageStylePromptVersionId": payload.get("imageStylePromptVersionId"),
                    "imageStylePromptVersion": payload.get("imageStylePromptVersion"),
                }
            else:
                if selected_preview:
                    preview_ref = str(selected_preview.get("objectPath") or "")
                    preview_name = str(selected_preview.get("fileName") or "")
                    with SessionLocal() as db:
                        selected_asset = db.query(Asset).filter(
                            Asset.job_id == job_id,
                            Asset.scene_number == int(selected_preview["sceneNumber"]),
                        ).first()
                    if selected_asset:
                        created_assets.append({
                            "id": selected_asset.id,
                            "jobId": job_id,
                            "fileName": selected_asset.file_name,
                            "objectPath": selected_asset.object_path,
                            "url": asset_content_url(selected_asset.id),
                            "sceneNumber": selected_asset.scene_number,
                            "status": selected_asset.status,
                            "isDemo": selected_asset.is_demo,
                        })
                        selected_preview = None
                if selected_preview:
                    preview_ref = str(selected_preview.get("objectPath") or "")
                    preview_name = str(selected_preview.get("fileName") or "")
                    local_preview = object_storage.local_path(preview_ref)
                    if local_preview and (local_preview.parent.resolve() != Path(PREVIEW_DIR).resolve() or not local_preview.is_file()):
                        raise ValueError("选中的样图已失效，请重新生成样图")
                    if local_preview and not object_storage.is_configured():
                        promoted_path = Path(ASSET_DIR) / preview_name
                        promoted_path.parent.mkdir(parents=True, exist_ok=True)
                        # Keep the preview until the Asset row commits so a restarted attempt can promote it again.
                        shutil.copyfile(local_preview, promoted_path)
                        asset_ref = str(promoted_path)
                    else:
                        stage_dir = Path(PREVIEW_DIR) / job_id
                        stage_dir.mkdir(parents=True, exist_ok=True)
                        promoted_path = stage_dir / preview_name
                        try:
                            object_storage.download_reference(preview_ref, promoted_path)
                        except object_storage.ObjectStorageError as exc:
                            raise ValueError("选中的样图已失效，请重新生成样图") from exc
                        asset_ref = _store_generated_file(
                            promoted_path, workspace_id, preview_name, "assets",
                            f"jobs/{job_id}/scene-{int(selected_preview['sceneNumber'])}{Path(preview_name).suffix}",
                        )
                    with SessionLocal() as db:
                        if not _owns_job_execution(db, job_id, execution_token):
                            if _finish_cancel_request(job_id) and thread_id and payload.get("agentGraph"):
                                _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
                            return
                        asset = Asset(
                            workspace_id=workspace_id,
                            job_id=job_id,
                            file_name=preview_name,
                            object_path=asset_ref,
                            prompt=str(selected_preview.get("prompt") or ""),
                            scene_number=int(selected_preview["sceneNumber"]),
                            status="needs_review",
                            is_demo=False,
                        )
                        db.add(asset)
                        db.commit()
                        db.refresh(asset)
                        created_assets.append({
                            "id": asset.id,
                            "jobId": job_id,
                            "fileName": asset.file_name,
                            "objectPath": asset.object_path,
                            "url": asset_content_url(asset.id),
                            "sceneNumber": asset.scene_number,
                            "status": asset.status,
                            "isDemo": asset.is_demo,
                        })
                    _remove_preview_files([selected_preview])
                for index, scene in enumerate(selected_scenes, start=1):
                    if _finish_cancel_request(job_id):
                        if thread_id and payload.get("agentGraph"):
                            _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
                        return
                    scene_number = int(scene.get("sceneNumber", index))
                    with SessionLocal() as db:
                        if not _owns_job_execution(db, job_id, execution_token):
                            if _finish_cancel_request(job_id) and thread_id and payload.get("agentGraph"):
                                _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
                            return
                    with SessionLocal() as db:
                        existing_asset = db.query(Asset).filter(
                            Asset.job_id == job_id,
                            Asset.scene_number == scene_number,
                        ).first()
                    if existing_asset:
                        created_assets.append({
                            "id": existing_asset.id,
                            "jobId": job_id,
                            "fileName": existing_asset.file_name,
                            "objectPath": existing_asset.object_path,
                            "url": asset_content_url(existing_asset.id),
                            "sceneNumber": existing_asset.scene_number,
                            "status": existing_asset.status,
                            "isDemo": existing_asset.is_demo,
                        })
                        continue
                    prompt = compose_image_prompt(scene["imagePrompt"], style_prompt)
                    if payload.get("agentGraph"):
                        generated = generate_scene_image(scene_number, prompt)
                        file_name = generated.file_name
                        file_path = Path(generated.object_path)
                        object_ref = _store_generated_file(
                            file_path, workspace_id, file_name, "assets",
                            f"jobs/{job_id}/scene-{scene_number}{Path(file_name).suffix}",
                        )
                        saved_prompt = generated.prompt
                        is_demo = False
                    else:
                        file_name = f"{job_id}-{scene_number}.svg"
                        file_path = ASSET_DIR / file_name
                        create_demo_image(file_path, scene_number, prompt)
                        object_ref = _store_generated_file(
                            file_path, workspace_id, file_name, "assets",
                            f"jobs/{job_id}/scene-{scene_number}{Path(file_name).suffix}",
                        )
                        saved_prompt = prompt
                        is_demo = True
                    with SessionLocal() as db:
                        if not _owns_job_execution(db, job_id, execution_token):
                            if _finish_cancel_request(job_id) and thread_id and payload.get("agentGraph"):
                                _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
                            return
                        asset = Asset(
                            workspace_id=workspace_id,
                            job_id=job_id,
                            file_name=file_name,
                            object_path=object_ref,
                            prompt=saved_prompt,
                            scene_number=scene_number,
                            status="needs_review",
                            is_demo=is_demo,
                        )
                        db.add(asset)
                        db.commit()
                        db.refresh(asset)
                        created_assets.append({
                            "id": asset.id,
                            "jobId": job_id,
                            "fileName": asset.file_name,
                            "objectPath": asset.object_path,
                        "url": asset_content_url(asset.id),
                            "sceneNumber": asset.scene_number,
                            "status": asset.status,
                            "isDemo": asset.is_demo,
                        })
                    _update_job(job_id, progress=min(15 + int(index / max(len(selected_scenes), 1) * 75), 90), message=f"已生成分镜图片 {index}/{len(selected_scenes)}")
                for preview in payload.get("previewAssets", []):
                    _remove_preview_files([preview])
                result = {
                    "assets": created_assets,
                    "storyboard": scenes,
                    "mode": "live" if payload.get("agentGraph") else "demo",
                    "imageStylePromptId": payload.get("imageStylePromptId"),
                    "imageStylePromptVersionId": payload.get("imageStylePromptVersionId"),
                    "imageStylePromptVersion": payload.get("imageStylePromptVersion"),
                }
        elif job_type == "video_generation":
            with SessionLocal() as db:
                subtitle = db.get(SubtitleFile, payload["subtitleFileId"])
                image_job = db.get(Job, payload.get("imageJobId")) if payload.get("imageJobId") else None
                if not subtitle or subtitle.workspace_id != workspace_id or not image_job or image_job.job_type != "image_generation" or image_job.status != "completed":
                    raise ValueError("找不到已完成的图片生成任务或 SRT 文件，请重新发起视频生成")
                if image_job.workspace_id != workspace_id or image_job.thread_id != thread_id:
                    raise ValueError("图片生成任务不属于当前工作区和对话")
                scenes = (image_job.result or {}).get("storyboard") or []
                assets = db.query(Asset).filter(Asset.job_id == image_job.id).order_by(Asset.scene_number, Asset.created_at).all()
                expected_numbers = [int(scene.get("sceneNumber", index)) for index, scene in enumerate(scenes, start=1)]
                assets_by_scene = {asset.scene_number: asset for asset in assets}
                if not scenes or len(assets_by_scene) != len(assets) or set(assets_by_scene) != set(expected_numbers):
                    raise ValueError("本次图片素材与分镜不完整或序号重复，无法生成时间线")
                if any(assets_by_scene[number].status != "approved" for number in expected_numbers):
                    raise ValueError("请先审核通过本次分镜生成的全部图片")

                alignment = align_scenes_to_subtitles(scenes, subtitle.cues)
                existing_export = db.query(ExportFile).filter(ExportFile.job_id == job_id).first()
                if existing_export:
                    result = {
                        "exportId": existing_export.id,
                        "fileName": existing_export.file_name,
                        "downloadUrl": f"/api/v1/exports/{existing_export.id}/download",
                        "format": "lingzhu-timeline-v1",
                    }
                    assets = []
                else:
                    assets = db.query(Asset).filter(Asset.job_id == image_job.id).order_by(Asset.scene_number, Asset.created_at).all()
                asset_data = [
                    {
                        "sceneNumber": asset.scene_number,
                        "fileName": asset.file_name,
                        "objectPath": object_storage.access_url(asset.object_path),
                    }
                    for asset in assets
                ]
                if not existing_export:
                    file_name = f"{job_id}.json"
                    file_path = EXPORT_DIR / file_name
                    timeline = write_timeline(file_path, alignment, asset_data)
                    workspace = db.get(Workspace, workspace_id)
                    export_ref = object_storage.persist_file(
                        file_path,
                        f"exports/{workspace.owner_user_id if workspace else 'unknown'}/{workspace_id}/{file_name}",
                        "application/json",
                    )
                    if export_ref.startswith("storage://"):
                        file_path.unlink(missing_ok=True)
                    export = ExportFile(
                        job_id=job_id,
                        file_name=file_name,
                        object_path=export_ref,
                        export_format="lingzhu-timeline-json",
                    )
                    if not _owns_job_execution(db, job_id, execution_token):
                        return
                    db.add(export)
                    db.commit()
                    db.refresh(export)
                    result = {
                        "exportId": export.id,
                        "fileName": file_name,
                        "downloadUrl": f"/api/v1/exports/{export.id}/download",
                        "format": timeline["format"],
                        "videoClipCount": len(timeline["videoTrack"]),
                        "subtitleCount": len(timeline["subtitleTrack"]),
                        "matchRatio": timeline["alignment"]["matchRatio"],
                    }
            _update_job(job_id, progress=90, message="字幕和图片已按分镜顺序配对，JSON 时间线已生成")
        else:
            raise ValueError(f"不支持的任务类型：{job_type}")
        if not _mark_job_completed(
            job_id,
            result,
            resume_workflow=bool(thread_id),
        ):
            if _finish_cancel_request(job_id) and job_type == "image_generation" and thread_id and payload.get("agentGraph"):
                _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
            return
        _finish_job_side_effects(job_id)
    except Exception as exc:
        if payload.get("agentGraphPreview"):
            _remove_preview_files(locals().get("preview_assets", []))
        if _finish_cancel_request(job_id):
            if job_type == "image_generation" and thread_id and payload.get("agentGraph"):
                _resume_agent_image_flow(thread_id, job_id, [], "任务已取消")
            return
        logger.exception("后台任务执行失败，job_id=%s, type=%s", job_id, job_type)
        _retry_or_dead_letter(job_id, exc)
    finally:
        heartbeat_stop.set()
        _current_job_token.reset(token_context)


@celery_app.task(
    name=queue.MEDIA_JOB_TASK_NAME,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_media_job(job_id: str) -> None:
    run_job(job_id)


# 把自己注册进队列层，queue 就不必反过来 import 本模块（否则循环导入）。
queue.register_task(queue.MEDIA_JOB_TASK_NAME, process_media_job)


def _dispatch_outbox_event(event_id: str) -> bool:
    """认领并投递一条 outbox 事件。状态机在 infrastructure/outbox.py。

    投递动作用本模块的 process_media_job —— 测试对
    `local_tasks.process_media_job.apply_async` 的打桩因此继续生效。
    """
    with SessionLocal() as db:
        return outbox.claim_and_publish(
            db,
            event_id,
            publisher=lambda job_id, generation: queue.enqueue_media_job(
                job_id, generation, task=process_media_job
            ),
        )


def _dispatch_pending_outbox(limit: int = 100) -> int:
    with SessionLocal() as db:
        event_ids = outbox.list_pending_event_ids(db, limit)
    return sum(1 for event_id in event_ids if _dispatch_outbox_event(event_id))


def launch_job(job_id: str) -> str:
    """确保任务有一条待投递意图，并立刻尝试投递。

    实现在 infrastructure/queue.py。这里传本模块的 SessionLocal 与投递函数，
    保证测试对 `local_tasks.SessionLocal` / `process_media_job` 的打桩继续生效。
    """
    return queue.publish_job(
        job_id,
        session_factory=SessionLocal,
        publisher=lambda job_id_, generation: queue.enqueue_media_job(
            job_id_, generation, task=process_media_job
        ),
    )


@celery_app.task(name="lingzhu.dispatch_job_outbox")
def dispatch_job_outbox() -> int:
    return _dispatch_pending_outbox()


@celery_app.task(name="lingzhu.recover_media_jobs")
def recover_media_jobs() -> dict:
    with SessionLocal() as db:
        result = recovery.scan_and_recover(
            db,
            stale_seconds=JOB_STALE_SECONDS,
            record_dispatch=record_job_dispatch,
        )
        db.commit()

    for job_id in result.pending_resumes:
        try:
            _finish_job_side_effects(job_id)
        except Exception:
            logger.exception("event=job.workflow_resume_recovery_failed job_id=%s", job_id)
    dispatched = _dispatch_pending_outbox()
    summary = result.as_dict(dispatched)
    logger.info(
        "event=job.recovery_complete recovered=%s dead_lettered=%s dispatched=%s",
        result.recovered,
        result.dead_lettered,
        dispatched,
    )
    return summary

