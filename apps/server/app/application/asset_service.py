"""Asset use cases: ownership, review and download preparation."""
import logging
import mimetypes

from sqlalchemy.orm import Session

from app.application.download_file import DownloadFile
from app.application.workspace_service import require_workspace
from app.domain.exceptions import NotFoundError
from app.infrastructure import file_io, storage
from app.repositories import asset_repo
from app.utils.files import safe_filename
from app.services.resource_urls import asset_content_url

logger = logging.getLogger(__name__)


def list_assets(db: Session, *, workspace_id: str, user_id: str) -> list[dict]:
    require_workspace(db, workspace_id, user_id)
    return [{
        "id": item.id, "fileName": item.file_name,
        "url": asset_content_url(item.id),
        "prompt": item.prompt, "sceneNumber": item.scene_number,
        "status": item.status, "isDemo": item.is_demo,
    } for item in asset_repo.list_by_workspace(db, workspace_id)]


def review_asset(db: Session, *, asset_id: str, decision: str, user_id: str) -> dict:
    try:
        asset = asset_repo.get_owned(db, asset_id, user_id) if user_id else None
        if asset is None:
            raise NotFoundError("找不到图片素材")
        asset.status = "approved" if decision == "approve" else "rejected"
        db.commit()
        result = {"id": asset.id, "status": asset.status}
    except Exception:
        db.rollback()
        raise
    logger.info("图片素材审核完成，asset_id=%s, decision=%s", asset.id, decision)
    return result


def prepare_download(db: Session, *, workspace_id: str, asset_ids: list[str], user_id: str) -> DownloadFile:
    require_workspace(db, workspace_id, user_id)
    unique_ids = list(dict.fromkeys(asset_ids))
    assets = asset_repo.list_selected(db, workspace_id, unique_ids)
    if len(assets) != len(unique_ids):
        raise NotFoundError("部分素材不存在或不属于当前工作区")

    directory = file_io.temporary_directory("lingzhu-asset-download-")
    try:
        downloaded = []
        for index, asset in enumerate(assets, start=1):
            filename = safe_filename(asset.file_name)
            destination = directory / f"{index:03d}-{filename}"
            try:
                storage.download_reference(asset.object_path, destination)
            except storage.ObjectStorageError as exc:
                raise NotFoundError(f"素材文件暂时不可用：{filename}") from exc
            downloaded.append(destination)
        if len(downloaded) == 1:
            asset = assets[0]
            return DownloadFile(
                downloaded[0], mimetypes.guess_type(asset.file_name)[0] or "application/octet-stream",
                filename=safe_filename(asset.file_name), cleanup_directory=directory,
            )
        archive = directory / "lingzhu-assets.zip"
        entries = [(path, f"scene-{asset.scene_number:02d}-{safe_filename(asset.file_name)}")
                   for asset, path in zip(assets, downloaded)]
        file_io.create_archive(archive, entries)
        return DownloadFile(archive, "application/zip", filename="lingzhu-assets.zip", cleanup_directory=directory)
    except Exception:
        file_io.remove_directory(directory)
        raise
