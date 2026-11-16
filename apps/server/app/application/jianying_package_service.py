"""Authorize and prepare a complete local-Jianying import package."""

from sqlalchemy.orm import Session

from app.application.download_file import DownloadFile
from app.domain.exceptions import ConflictError, NotFoundError
from app.infrastructure import jianying_package
from app.infrastructure.storage import ObjectStorageError
from app.domain.exceptions import UpstreamError
from app.repositories import asset_repo, job_repo, subtitle_repo
from app.services.aligner import align_scenes_to_subtitles
from app.services.jianying import build_timeline
from app.utils.files import safe_filename


def prepare_package(db: Session, *, video_job_id: str, user_id: str) -> DownloadFile:
    job = job_repo.get_owned_job(db, video_job_id, user_id) if user_id else None
    if job is None or job.job_type != "video_generation":
        raise NotFoundError("找不到视频任务")
    if job.status != "completed":
        raise ConflictError("视频任务尚未完成")

    payload = job.payload or {}
    subtitle = subtitle_repo.get(db, payload.get("subtitleFileId") or "")
    image_job = job_repo.get_owned_job(db, payload.get("imageJobId") or "", user_id)
    if (subtitle is None or subtitle.workspace_id != job.workspace_id or not subtitle.audio_object_path
            or image_job is None or image_job.job_type != "image_generation"
            or image_job.status != "completed" or image_job.workspace_id != job.workspace_id
            or image_job.thread_id != job.thread_id):
        raise NotFoundError("找不到完整的字幕、配音或图片素材")

    scenes = (image_job.result or {}).get("storyboard") or []
    expected = [int(scene.get("sceneNumber", index)) for index, scene in enumerate(scenes, start=1)]
    assets = asset_repo.list_for_job(db, image_job.id)
    by_scene = {asset.scene_number: asset for asset in assets}
    if (not scenes or len(by_scene) != len(assets) or set(by_scene) != set(expected)
            or any(by_scene[number].status != "approved" for number in expected)):
        raise ConflictError("图片素材不完整或尚未全部审核通过")

    alignment = align_scenes_to_subtitles(scenes, subtitle.cues)
    package_assets = []
    timeline_assets = []
    for scene_number in expected:
        asset = by_scene[scene_number]
        name = safe_filename(asset.file_name)
        relative = f"images/scene-{scene_number:04d}-{name}"
        package_assets.append({"sceneNumber": scene_number, "fileName": name,
                               "path": relative, "reference": asset.object_path})
        timeline_assets.append({"sceneNumber": scene_number, "fileName": name,
                                "objectPath": f"materials/{relative}"})

    timeline = build_timeline(alignment, timeline_assets)
    subtitle_name = safe_filename(subtitle.file_name or "voice.srt")
    audio_name = safe_filename(subtitle.audio_file_name or "voiceover.bin")
    try:
        path = jianying_package.build_package(
            filename=f"lingzhu-jianying-{job.id}.zip",
            timeline=timeline,
            subtitle={"fileName": subtitle_name, "path": f"subtitles/{subtitle_name}",
                      "reference": subtitle.object_path},
            audio={"fileName": audio_name, "path": f"audio/{audio_name}",
                   "reference": subtitle.audio_object_path},
            assets=package_assets,
        )
    except ObjectStorageError as exc:
        raise UpstreamError("导出素材暂时无法读取，请稍后重试") from exc
    return DownloadFile(path, "application/zip", filename=path.name, cleanup_directory=path.parent)
