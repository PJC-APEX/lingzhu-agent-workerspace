"""任务（Job）与素材（Asset）的数据访问。

只做查询与写入，不做业务判断、不自行提交事务。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models import Asset, Job, Workspace

__all__ = [
    "add_job",
    "add_jobs",
    "flush",
    "get_asset",
    "get_job",
    "get_owned_job",
    "list_assets",
    "mark_assets_rejected",
]


def get_job(db: Session, job_id: str) -> Job | None:
    return db.get(Job, job_id)


def find_video_export(db: Session, thread_id: str, image_job_id: str, subtitle_file_id: str):
    jobs = db.query(Job).filter(Job.thread_id == thread_id, Job.job_type == "video_generation").order_by(Job.created_at.desc()).all()
    return next((job for job in jobs if (job.payload or {}).get("imageJobId") == image_job_id
                 and (job.payload or {}).get("subtitleFileId") == subtitle_file_id), None)


def latest_completed_images(db: Session, workspace_id: str, thread_id: str) -> Job | None:
    return db.query(Job).filter(
        Job.workspace_id == workspace_id, Job.thread_id == thread_id,
        Job.job_type == "image_generation", Job.status == "completed",
    ).order_by(Job.updated_at.desc(), Job.created_at.desc()).first()


def get_owned_job(db: Session, job_id: str, user_id: str) -> Job | None:
    """按 id 取任务，并校验它属于该用户的某个工作区。

    任务本身没有 owner 字段，归属要通过 workspace.owner_user_id 判断。
    None 表示不存在或不属于该用户——调用方不应区分这两者，
    否则就泄露了「这个 id 确实存在」的信息。
    """
    row = (
        db.query(Job)
        .join(Workspace, Workspace.id == Job.workspace_id)
        .filter(Job.id == job_id, Workspace.owner_user_id == user_id)
        .first()
    )
    return row


def add_job(db: Session, job: Job) -> Job:
    db.add(job)
    return job


def add_jobs(db: Session, jobs: list[Job]) -> list[Job]:
    db.add_all(jobs)
    return jobs


def flush(db: Session) -> None:
    """把挂起的对象推到数据库，让服务端默认值就位。不结束事务。"""
    db.flush()


def get_asset(db: Session, asset_id: str) -> Asset | None:
    return db.get(Asset, asset_id)


def list_assets(db: Session, asset_ids: list[str]) -> list[Asset]:
    if not asset_ids:
        return []
    return db.query(Asset).filter(Asset.id.in_(asset_ids)).all()


def mark_assets_rejected(db: Session, assets: list[Asset]) -> None:
    """把素材标为已驳回。只改对象状态，由调用方决定何时提交。"""
    for asset in assets:
        asset.status = "rejected"
