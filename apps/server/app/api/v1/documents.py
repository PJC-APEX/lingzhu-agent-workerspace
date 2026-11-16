"""文档与原文版本相关的 HTTP 接口。

本文件只负责：接收参数、调用 application 层、组装响应。
不包含任何数据库操作，也不做业务判断。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.v1.deps import current_user_id, get_db, require_user
from app.application import document_service
from app.domain.exceptions import ServiceBusyError
from app.utils.files import safe_filename

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


class SourceRevisionUpdate(BaseModel):
    baseRevisionId: str
    content: str = Field(min_length=1, max_length=200_000)
    rewriteSessionId: str | None = None


async def _run_in_threadpool(operation, *args):
    if not document_service.acquire_upload_slot():
        raise ServiceBusyError("上传服务正忙，请稍后重试")
    try:
        return await run_in_threadpool(operation, *args)
    finally:
        document_service.release_upload_slot()


@router.post("/workspaces/{workspace_id}/documents")
async def upload_document(workspace_id: str, file: UploadFile = File(...)):
    filename = safe_filename(file.filename or "upload.txt")
    return await _run_in_threadpool(
        _upload_sync,
        workspace_id,
        current_user_id() or "",
        file.file,
        filename,
        file.content_type,
    )


@router.get("/workspaces/{workspace_id}/documents")
def get_documents(workspace_id: str, db: Session = Depends(get_db)):
    return document_service.list_workspace_documents(
        db,
        workspace_id=workspace_id,
        user_id=current_user_id() or "",
    )


@router.get("/documents/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db)):
    return document_service.get_document_detail(
        db,
        document_id=document_id,
        user_id=current_user_id() or "",
    )


@router.post("/documents/{document_id}/revisions")
def create_source_revision(document_id: str, body: SourceRevisionUpdate, db: Session = Depends(get_db)):
    """保存左侧手动编辑，并在同一事务中切换当前对比基准。"""
    return document_service.create_source_revision(
        db,
        document_id=document_id,
        user_id=current_user_id() or "",
        base_revision_id=body.baseRevisionId,
        content=body.content,
        rewrite_session_id=body.rewriteSessionId,
    )


def _upload_sync(workspace_id: str, user_id: str, file_stream, filename: str, content_type: str | None) -> dict:
    """在线程池中执行的上传逻辑：自开会话，避免跨线程复用请求会话。"""
    from app.core.container import get_session_factory

    with get_session_factory()() as db:
        return document_service.create_document_from_upload(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            file_stream=file_stream,
            filename=filename,
            content_type=content_type,
        )
