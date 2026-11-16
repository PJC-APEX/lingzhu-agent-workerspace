"""文档业务编排。

职责边界：
- 校验归属与业务规则，违规抛 `app.domain.exceptions` 中的业务异常
- 协调存储落盘与数据库写入的顺序，保证失败时不留残留文件
- 控制事务边界（commit / rollback）

不负责：参数解析、HTTP 状态码、响应序列化。
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from app.core import config
from app.core.config import UPLOAD_DIR
from app.domain.exceptions import (
    ConflictError,
    NotFoundError,
    TooLargeError,
    UnsupportedFormatError,
    UpstreamError,
    ValidationError,
)
from app.domain.models import Document, DocumentRevision, RewriteSession, Workspace, new_id
from app.repositories import document_repo, workspace_repo
from app.infrastructure import storage as object_storage
from app.utils.files import extract_text, safe_filename

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 全局上传并发上限：超限直接拒绝，避免大量解析任务打满线程池
_UPLOAD_LIMITER = threading.BoundedSemaphore(config.UPLOAD_MAX_CONCURRENCY)

__all__ = [
    "acquire_upload_slot",
    "create_document_from_upload",
    "create_source_revision",
    "get_document_detail",
    "list_workspace_documents",
]


def acquire_upload_slot() -> bool:
    """尝试获取一个上传槽位，调用方负责在结束时 release_upload_slot。"""
    return _UPLOAD_LIMITER.acquire(blocking=False)


def release_upload_slot() -> None:
    _UPLOAD_LIMITER.release()


def _require_workspace(db: Session, workspace_id: str, user_id: str) -> Workspace:
    """取工作区并校验归属，语义与原 `_get_or_404(db, Workspace, ...)` 一致。"""
    workspace = workspace_repo.get_owned(db, workspace_id, user_id)
    if not workspace:
        raise NotFoundError("工作区")
    return workspace


def create_document_from_upload(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    file_stream,
    filename: str,
    content_type: str | None,
) -> dict:
    """落盘 → 解析 → 上传存储 → 写库。

    顺序刻意设计为「先存储、后数据库」：数据库失败时删除已上传的对象，
    避免旧实现中两条数据库连接导致的孤儿文件。
    """
    safe_name = safe_filename(filename or "upload.txt")
    _require_workspace(db, workspace_id, user_id)

    destination = UPLOAD_DIR / f"{new_id()}-{safe_name}"
    data = file_stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise TooLargeError("文件不能超过 20MB")
    destination.write_bytes(data)

    try:
        content = extract_text(destination, safe_name)
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        raise UnsupportedFormatError(str(exc)) from exc
    except Exception as exc:
        destination.unlink(missing_ok=True)
        logger.exception("event=document.parse_failed filename=%s", safe_name)
        raise ValidationError("文档解析失败") from exc
    if not content.strip():
        destination.unlink(missing_ok=True)
        raise ValidationError("文件中没有解析到可用文字")

    object_ref: str | None = None
    try:
        object_ref = object_storage.persist_file(
            destination,
            f"uploads/{user_id}/{workspace_id}/{destination.name}",
            content_type or "application/octet-stream",
        )
    except object_storage.ObjectStorageError as exc:
        destination.unlink(missing_ok=True)
        raise UpstreamError("文档存储暂时不可用") from exc
    if object_ref.startswith("storage://"):
        # 已转存到对象存储，本地暂存文件不再需要
        destination.unlink(missing_ok=True)

    try:
        document = document_repo.add_document(
            db,
            workspace_id=workspace_id,
            file_name=safe_name,
            media_type=content_type or "text/plain",
            object_path=object_ref,
        )
        revision = document_repo.add_revision(
            db,
            document_id=document.id,
            revision_number=1,
            content=content,
        )
        db.commit()
    except Exception:
        db.rollback()
        # 数据库写入失败：清理已上传的对象，避免孤儿文件
        if object_ref:
            try:
                object_storage.delete_reference(object_ref)
            except Exception:
                logger.exception("event=document.cleanup_failed ref=%s", object_ref)
        destination.unlink(missing_ok=True)
        raise

    logger.info(
        "event=source.uploaded document_id=%s file=%s chars=%s",
        document.id,
        safe_name,
        len(content),
    )
    return {
        "documentId": document.id,
        "revisionId": revision.id,
        "fileName": safe_name,
        "content": content,
        "characterCount": len(content),
    }


def list_workspace_documents(db: Session, *, workspace_id: str, user_id: str) -> list[dict]:
    _require_workspace(db, workspace_id, user_id)
    documents = document_repo.list_documents(db, workspace_id)
    # 一次取回所有文档的最新版本摘要（版本号与字数在库里算好，正文不传回）
    latest = document_repo.latest_revisions_by_document_ids(db, [item.id for item in documents])
    return [
        {
            "id": item.id,
            "fileName": item.file_name,
            "revisionId": latest[item.id]["id"] if item.id in latest else None,
            "characterCount": latest[item.id]["character_count"] if item.id in latest else 0,
        }
        for item in documents
    ]


def get_document_detail(db: Session, *, document_id: str, user_id: str) -> dict:
    document = _require_document(db, document_id, user_id)
    revision = document_repo.latest_revision(db, document.id)
    return {
        "id": document.id,
        "fileName": document.file_name,
        "revisionId": revision.id if revision else None,
        "content": revision.content if revision else "",
    }


def create_source_revision(
    db: Session,
    *,
    document_id: str,
    user_id: str,
    base_revision_id: str,
    content: str,
    rewrite_session_id: str | None = None,
) -> dict:
    """保存左侧原文的手动编辑，并在同一事务内切换对比基准。"""
    document = _require_document(db, document_id, user_id)
    latest = document_repo.latest_revision(db, document.id)
    if not latest or latest.id != base_revision_id:
        raise ConflictError("左侧原文已更新，请刷新后再保存")
    if not content.strip():
        raise ValidationError("原文不能为空")

    session: RewriteSession | None = None
    if rewrite_session_id:
        session = db.get(RewriteSession, rewrite_session_id)
        if not session:
            raise NotFoundError("改写会话")
        source_document = document_repo.get_document(db, session.document_id)
        if not source_document or not workspace_repo.get_owned(db, source_document.workspace_id, user_id):
            raise NotFoundError("改写会话")
        if source_document.workspace_id != document.workspace_id:
            raise NotFoundError("改写会话不属于当前工作区")

    revision = document_repo.add_revision(
        db,
        document_id=document.id,
        revision_number=latest.revision_number + 1,
        content=content,
    )
    if session:
        session.comparison_document_id = document.id
        session.comparison_revision_id = revision.id
    db.commit()
    db.refresh(revision)

    logger.info(
        "event=source.manual_edit_saved document_id=%s revision_id=%s revision_number=%s chars=%s session_id=%s",
        document.id,
        revision.id,
        revision.revision_number,
        len(content),
        session.id if session else "none",
    )
    payload = {
        "documentId": document.id,
        "revisionId": revision.id,
        "revisionNumber": revision.revision_number,
        "fileName": document.file_name,
        "content": revision.content,
        "session": None,
    }
    if session:
        # 与旧响应保持一致：关联改写会话时顺带返回会话快照
        from app.application.rewrite_snapshot import session_payload

        payload["session"] = session_payload(db, session, user_id)
    return payload


def _require_document(db: Session, document_id: str, user_id: str) -> Document:
    document = document_repo.get_document(db, document_id)
    if not document or not workspace_repo.get_owned(db, document.workspace_id, user_id):
        raise NotFoundError("原文")
    return document


