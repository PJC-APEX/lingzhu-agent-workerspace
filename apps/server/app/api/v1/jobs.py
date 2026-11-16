"""后台任务的查询、取消、重试与列表。"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from fastapi import APIRouter
from app.api.v1.deps import require_user
from app.application import job_service
from app.infrastructure.database import get_db
from app.api.v1.deps import current_user_id
from app.application.agent_image_flow import resume_agent_image_flow as _resume_agent_image_flow
from app.infrastructure.queue import publish_job as launch_job


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    return job_service.get_job(db, job_id, user_id=current_user_id())


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    # 把本模块的 _resume_agent_image_flow 传下去，测试对 jobs 模块的打桩才生效
    return job_service.cancel_job(
        db, job_id, user_id=current_user_id(), resume_image_flow=_resume_agent_image_flow
    )


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, db: Session = Depends(get_db)):
    return job_service.retry_job(db, job_id, user_id=current_user_id(), launcher=launch_job)


@router.get("/workspaces/{workspace_id}/jobs")
def get_workspace_jobs(workspace_id: str, db: Session = Depends(get_db)):
    return job_service.list_workspace_jobs(db, workspace_id, user_id=current_user_id())
