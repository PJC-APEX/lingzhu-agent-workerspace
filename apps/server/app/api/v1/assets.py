"""HTTP adapters for assets, prepared downloads and subtitle uploads."""
from __future__ import annotations

from fastapi import Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter
from app.api.v1.deps import require_user
from app.api.v1.files import file_response
from app.application import asset_service, subtitle_service
from app.domain.exceptions import ServiceBusyError
from app.infrastructure.database import get_db
from app.api.v1.deps import current_user_id


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


class AssetReview(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


class AssetDownloadRequest(BaseModel):
    assetIds: list[str] = Field(min_length=1, max_length=100)


async def _run_upload_in_threadpool(operation, **kwargs):
    if not subtitle_service.acquire_upload_slot():
        raise ServiceBusyError('上传服务正忙，请稍后重试')
    try:
        return await run_in_threadpool(operation, **kwargs)
    finally:
        subtitle_service.release_upload_slot()


@router.get("/workspaces/{workspace_id}/assets")
def get_assets(workspace_id: str, db: Session = Depends(get_db)):
    return asset_service.list_assets(db, workspace_id=workspace_id, user_id=current_user_id() or "")


@router.post("/workspaces/{workspace_id}/assets/download")
def download_assets(workspace_id: str, body: AssetDownloadRequest, db: Session = Depends(get_db)):
    return file_response(asset_service.prepare_download(
        db, workspace_id=workspace_id, asset_ids=body.assetIds, user_id=current_user_id() or "",
    ))


@router.post("/assets/{asset_id}/review")
def review_asset(asset_id: str, body: AssetReview, db: Session = Depends(get_db)):
    return asset_service.review_asset(
        db, asset_id=asset_id, decision=body.decision, user_id=current_user_id() or "",
    )


@router.post("/workspaces/{workspace_id}/subtitle-files")
async def upload_subtitle(workspace_id: str, file: UploadFile = File(...)):
    part = subtitle_service.subtitle_part(file.filename or "voiceover.srt", file.file)
    return await _run_upload_in_threadpool(
        subtitle_service.upload_subtitle,
        workspace_id=workspace_id, user_id=current_user_id() or "", part=part,
    )


@router.post("/workspaces/{workspace_id}/subtitle-bundles")
async def upload_subtitle_bundle(
    workspace_id: str,
    files: list[UploadFile] = File(...),
):
    subtitle, audio = subtitle_service.select_bundle([
        subtitle_service.UploadPart(item.filename or "", item.file) for item in files
    ])
    return await _run_upload_in_threadpool(
        subtitle_service.upload_bundle,
        workspace_id=workspace_id, user_id=current_user_id() or "", subtitle=subtitle, audio=audio,
    )
