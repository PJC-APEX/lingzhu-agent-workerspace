"""文档与原文版本的数据访问。

只做查询与写入，不做业务判断、不自行提交事务。
"""

from __future__ import annotations

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.domain.models import Document, DocumentRevision

__all__ = [
    "add_document",
    "add_revision",
    "get_document",
    "get_revision",
    "latest_revision",
    "latest_revisions_by_document_ids",
    "list_documents",
]


def get_document(db: Session, document_id: str) -> Document | None:
    return db.get(Document, document_id)


def list_documents(db: Session, workspace_id: str) -> list[Document]:
    return (
        db.query(Document)
        .filter(Document.workspace_id == workspace_id)
        .order_by(Document.created_at.desc())
        .all()
    )


def get_revision(db: Session, revision_id: str) -> DocumentRevision | None:
    return db.get(DocumentRevision, revision_id)


def latest_revision(db: Session, document_id: str) -> DocumentRevision | None:
    return (
        db.query(DocumentRevision)
        .filter(DocumentRevision.document_id == document_id)
        .order_by(DocumentRevision.revision_number.desc())
        .first()
    )


def latest_revisions_by_document_ids(db: Session, document_ids: list[str]) -> dict[str, dict]:
    """一次性取多个文档各自的最新版本摘要。

    返回 {document_id: {"id": ..., "revision_number": ..., "character_count": ...}}。

    两层优化：
    1. 先在子查询里算出每个 document_id 的最大版本号，只把命中的那几行取回来。
       早先的写法是「全量排序后取首条」，虽然只有一次往返，但所有历史版本
       都会被传回 Python——一个文档改 50 版就白拉 49 行。
    2. 字数用 SQL 的 LENGTH(content) 在库里算，正文本身不传回应用层。
       列表页只需要「多长」，不需要「是什么」。
    """
    if not document_ids:
        return {}
    latest_numbers = (
        db.query(
            DocumentRevision.document_id.label("document_id"),
            func.max(DocumentRevision.revision_number).label("max_number"),
        )
        .filter(DocumentRevision.document_id.in_(document_ids))
        .group_by(DocumentRevision.document_id)
        .subquery()
    )
    rows = (
        db.query(
            DocumentRevision.id.label("id"),
            DocumentRevision.document_id.label("document_id"),
            DocumentRevision.revision_number.label("revision_number"),
            func.length(DocumentRevision.content).label("character_count"),
        )
        .join(
            latest_numbers,
            and_(
                DocumentRevision.document_id == latest_numbers.c.document_id,
                DocumentRevision.revision_number == latest_numbers.c.max_number,
            ),
        )
        .all()
    )
    return {
        row.document_id: {
            "id": row.id,
            "revision_number": row.revision_number,
            "character_count": row.character_count or 0,
        }
        for row in rows
    }


def add_document(db: Session, *, workspace_id: str, file_name: str, media_type: str, object_path: str | None) -> Document:
    document = Document(
        workspace_id=workspace_id,
        file_name=file_name,
        media_type=media_type,
        object_path=object_path,
    )
    db.add(document)
    db.flush()
    return document


def add_revision(db: Session, *, document_id: str, revision_number: int, content: str) -> DocumentRevision:
    revision = DocumentRevision(
        document_id=document_id,
        revision_number=revision_number,
        content=content,
    )
    db.add(revision)
    db.flush()
    return revision
