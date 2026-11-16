"""Private media authorization, reference selection and download preparation."""
import mimetypes
from pathlib import Path

from sqlalchemy.orm import Session

from app.application.download_file import DownloadFile
from app.core import config
from app.domain.exceptions import NotFoundError, UpstreamError
from app.infrastructure import file_io, storage
from app.repositories import asset_repo, media_repo, job_repo
from app.utils.files import safe_filename

PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def prepare_export(db: Session, *, export_id: str, user_id: str) -> DownloadFile:
    export = media_repo.get_owned_export(db, export_id, user_id) if user_id else None
    if export is None:
        raise NotFoundError("找不到导出文件")
    local = storage.local_path(export.object_path)
    if local is None:
        directory = file_io.temporary_directory("lingzhu-export-download-")
        try:
            path = directory / safe_filename(export.file_name)
            storage.download_reference(export.object_path, path)
            return DownloadFile(path, "application/json", filename=export.file_name, cleanup_directory=directory)
        except storage.ObjectStorageError as exc:
            file_io.remove_directory(directory)
            raise NotFoundError("导出文件暂时不可用") from exc
        except Exception:
            file_io.remove_directory(directory)
            raise
    if not file_io.exists(local):
        raise NotFoundError("导出文件不存在")
    return DownloadFile(local, "application/json", filename=export.file_name)


def _prepare_reference(reference: str, filename: str) -> DownloadFile:
    key = storage.object_key_from_reference(reference)
    if key:
        directory = file_io.temporary_directory("lingzhu-private-media-")
        try:
            path = directory / safe_filename(filename)
            storage.download_reference(reference, path)
        except storage.ObjectStorageError as exc:
            file_io.remove_directory(directory)
            raise UpstreamError("素材暂时无法读取") from exc
        except Exception:
            file_io.remove_directory(directory)
            raise
        return DownloadFile(
            path, mimetypes.guess_type(filename)[0] or "application/octet-stream",
            headers=dict(PRIVATE_HEADERS), cleanup_directory=directory,
        )
    path = Path(reference)
    if not file_io.is_file(path):
        raise NotFoundError("找不到素材")
    return DownloadFile(path, mimetypes.guess_type(filename)[0] or "application/octet-stream", headers=dict(PRIVATE_HEADERS))


def _check_prefix(reference: str, collection: str, user_id: str, workspace_id: str) -> None:
    key = storage.object_key_from_reference(reference)
    if key and not (key.startswith(f"{collection}/{user_id}/{workspace_id}/")
                    or key.startswith(f"{collection}/{workspace_id}/")):
        raise NotFoundError("找不到素材")


def prepare_asset(db: Session, *, asset_id: str, user_id: str) -> DownloadFile:
    asset = asset_repo.get_owned(db, asset_id, user_id) if user_id else None
    if asset is None:
        raise NotFoundError("找不到素材")
    _check_prefix(asset.object_path, "assets", user_id, asset.workspace_id)
    return _prepare_reference(asset.object_path, asset.file_name)


def prepare_preview(db: Session, *, job_id: str, preview_index: int, user_id: str) -> DownloadFile:
    job = job_repo.get_owned_job(db, job_id, user_id) if user_id else None
    if (job is None or job.job_type != "image_generation"
            or not (job.payload or {}).get("agentGraphPreview") or preview_index < 0):
        raise NotFoundError("找不到素材")
    matches = [item for item in (job.result or {}).get("assets", [])
               if type(item.get("previewIndex")) is int and item["previewIndex"] == preview_index]
    if len(matches) != 1:
        raise NotFoundError("找不到素材")
    preview = matches[0]
    reference = str(preview.get("objectPath") or "")
    if not reference:
        raise NotFoundError("找不到素材")
    _check_prefix(reference, "previews", user_id, job.workspace_id)
    local = storage.local_path(reference)
    if local and not file_io.is_direct_child(local, config.PREVIEW_DIR):
        raise NotFoundError("找不到素材")
    return _prepare_reference(reference, preview.get("fileName") or Path(reference).name)
