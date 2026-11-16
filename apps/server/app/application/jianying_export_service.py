"""Submit a package-compatible video job from an approved graph workflow."""
from app.core.container import get_session_factory
from app.core.config import JOB_MAX_ATTEMPTS
from app.domain.models import Job
from app.domain.exceptions import NotFoundError, ConflictError
from app.repositories import job_repo, subtitle_repo, asset_repo
from app.repositories.outbox_repo import record_job_dispatch
from app.infrastructure.queue import publish_job


def submit_export(*, thread_id: str, workspace_id: str, image_job_id: str,
                  subtitle_file_id: str, user_id: str) -> str:
    with get_session_factory()() as db:
        image_job = job_repo.get_owned_job(db, image_job_id, user_id)
        subtitle = subtitle_repo.get(db, subtitle_file_id)
        if (not image_job or image_job.workspace_id != workspace_id or image_job.thread_id != thread_id
                or image_job.job_type != "image_generation" or image_job.status != "completed"
                or not subtitle or subtitle.workspace_id != workspace_id or not subtitle.audio_object_path):
            raise NotFoundError("找不到当前对话的图片、字幕或配音")
        scenes = (image_job.result or {}).get("storyboard") or []
        assets = asset_repo.list_for_job(db, image_job_id)
        expected = {int(scene["sceneNumber"]) for scene in scenes}
        if (not expected or len(assets) != len(expected)
                or {asset.scene_number for asset in assets} != expected
                or any(asset.status != "approved" for asset in assets)):
            raise ConflictError("请先审核通过全部分镜图片")
        # Graph retries reuse this export intent, including after the commit
        # succeeded but before its graph checkpoint was saved.
        job = job_repo.find_video_export(db, thread_id, image_job_id, subtitle_file_id)
        if job is None:
            try:
                job = Job(workspace_id=workspace_id, thread_id=thread_id,
                          job_type="video_generation", status="queued", max_attempts=JOB_MAX_ATTEMPTS,
                          payload={"imageJobId": image_job_id, "subtitleFileId": subtitle_file_id},
                          message="正在准备剪映素材包")
                job_repo.add_job(db, job)
                record_job_dispatch(db, job)
                db.commit()
            except Exception:
                db.rollback()
                raise
        job_id = job.id
        should_publish = job.status == "queued"
    # The durable outbox repairs publish failures.
    try:
        if should_publish:
            publish_job(job_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("event=jianying.export_publish_deferred job_id=%s", job_id)
    return job_id
