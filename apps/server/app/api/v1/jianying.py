"""Download a self-contained package for local Jianying draft creation."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.deps import current_user_id, get_db, require_user
from app.api.v1.files import file_response
from app.application.jianying_package_service import prepare_package
from app.application.download_file import DownloadFile
from app.infrastructure.jianying_importer import build_importer

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


@router.get("/jianying/importer")
def download_importer():
    path = build_importer()
    return file_response(DownloadFile(path, "application/octet-stream", filename="LingzhuJianyingImporter.exe"))


@router.get("/jobs/{job_id}/jianying-package")
def download_jianying_package(job_id: str, db: Session = Depends(get_db)):
    package = prepare_package(db, video_job_id=job_id, user_id=current_user_id() or "")
    return file_response(package)
