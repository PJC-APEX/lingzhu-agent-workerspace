"""Authorized ID-based image content endpoints."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.deps import current_user_id, get_db, require_user
from app.api.v1.files import file_response
from app.application import media_service

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


@router.get("/assets/{asset_id}/content")
def get_asset_content(asset_id: str, db: Session = Depends(get_db)):
    return file_response(media_service.prepare_asset(db, asset_id=asset_id, user_id=current_user_id() or ""))


@router.get("/jobs/{job_id}/previews/{preview_index}/content")
def get_preview_content(job_id: str, preview_index: int, db: Session = Depends(get_db)):
    return file_response(media_service.prepare_preview(
        db, job_id=job_id, preview_index=preview_index, user_id=current_user_id() or "",
    ))
