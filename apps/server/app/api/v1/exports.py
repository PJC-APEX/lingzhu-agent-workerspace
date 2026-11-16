"""HTTP responses for authorized export and private-media downloads."""
from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from fastapi import APIRouter
from app.api.v1.deps import require_user
from app.api.v1.files import file_response
from app.application import media_service
from app.infrastructure.database import get_db
from app.api.v1.deps import current_user_id


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, db: Session = Depends(get_db)):
    return file_response(media_service.prepare_export(
        db, export_id=export_id, user_id=current_user_id() or "",
    ))


