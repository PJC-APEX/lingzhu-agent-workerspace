"""Rewrite use cases: ownership, version rules, candidate orchestration and transactions."""
from __future__ import annotations

import logging
from time import perf_counter
from sqlalchemy.orm import Session

from app.application.rewrite_access import require_owned
from app.application.rewrite_commands import RewriteCommand
from app.application.rewrite_snapshot import working_content, comparison_segments, session_payload
from app.domain.exceptions import AppError, ConflictError, NotFoundError, ValidationError, UpstreamError
from app.domain.models import Document, DocumentRevision, RewriteSession, WorkingRevision, RewriteRun, new_id
from app.repositories import rewrite_repo
from app.prompts.normal_rewrite_prompts import get_prompt_by_style_id
from app.services.model_gateway import ModelCallError
from app.services.normal_rewrite.service import rewrite_text as run_normal_rewrite
from app.utils.diff import utf16_offset_to_index

logger = logging.getLogger(__name__)

def create_rewrite_session(db: Session, *, workspace_id: str, document_id: str, source_revision_id: str, user_id: str) -> dict:
    try:
        document = require_owned(db, Document, document_id, user_id, "原文")
        if document.workspace_id != workspace_id:
            raise NotFoundError("找不到工作区中的原文")
        original = require_owned(db, DocumentRevision, source_revision_id, user_id, "原文版本")
        if original.document_id != document.id:
            raise AppError("原文版本不属于该文档")
        session = rewrite_repo.add_session(db, 
            id=new_id(),
            document_id=document.id,
            source_revision_id=original.id,
            comparison_document_id=document.id,
            comparison_revision_id=original.id,
        )
        working = rewrite_repo.add_working_revision(db, session_id=session.id, content=original.content)
        session.current_revision_id = working.id
        db.commit()
        db.refresh(session)
        logger.info(
            "event=workbench.copied_to_rewrite session_id=%s document_id=%s source_revision_id=%s chars=%s",
            session.id,
            document.id,
            original.id,
            len(original.content),
        )
        return session_payload(db, session, user_id)
    except Exception:
        db.rollback()
        raise


def update_comparison_baseline(db: Session, *, session_id: str, document_id: str, revision_id: str, user_id: str) -> dict:
    try:
        session = require_owned(db, RewriteSession, session_id, user_id, "改写会话")
        source_document = require_owned(db, Document, session.document_id, user_id, "改写来源文档")
        comparison_document = require_owned(db, Document, document_id, user_id, "对比文档")
        if comparison_document.workspace_id != source_document.workspace_id:
            raise NotFoundError("对比文档不属于当前工作区")
        revision = require_owned(db, DocumentRevision, revision_id, user_id, "对比文档版本")
        if revision.document_id != comparison_document.id:
            raise AppError("对比版本不属于所选文档")
        session.comparison_document_id = comparison_document.id
        session.comparison_revision_id = revision.id
        db.commit()
        logger.info(
            "event=workbench.comparison_baseline_changed session_id=%s source_document_id=%s comparison_document_id=%s comparison_revision_id=%s",
            session.id,
            session.document_id,
            comparison_document.id,
            revision.id,
        )
        return session_payload(db, session, user_id)
    except Exception:
        db.rollback()
        raise


def get_rewrite_session(db: Session, *, session_id: str, user_id: str) -> dict:
    session = require_owned(db, RewriteSession, session_id, user_id, "改写会话")
    return session_payload(db, session, user_id)


def update_rewrite_session(db: Session, *, session_id: str, base_revision_id: str, content: str, user_id: str) -> dict:
    try:
        session = require_owned(db, RewriteSession, session_id, user_id, "改写会话")
        current = working_content(db, session, user_id)
        if current.id != base_revision_id:
            raise ConflictError("中间文案已更新，请刷新后再保存")
        revision = rewrite_repo.add_working_revision(db, session_id=session.id, parent_revision_id=current.id, content=content)
        session.current_revision_id = revision.id
        db.commit()
        db.refresh(session)
        logger.info("中间工作副本已保存，session_id=%s, chars=%s", session.id, len(content))
        return session_payload(db, session, user_id)
    except Exception:
        db.rollback()
        raise


def run_rewrite(db: Session, *, session_id: str, command: RewriteCommand, user_id: str) -> dict:
    try:
        session = require_owned(db, RewriteSession, session_id, user_id, "改写会话")
        current = working_content(db, session, user_id)
        if current.id != command.base_revision_id:
            raise ConflictError("中间文案已更新，请刷新后再改写")
        scope_type = command.scope.get("type", "document")
        input_text = current.content
        replace_range = None
        if scope_type == "selection":
            try:
                start = utf16_offset_to_index(current.content, int(command.scope["from"]))
                end = utf16_offset_to_index(current.content, int(command.scope["to"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError("选区无效，请重新选择后改写") from exc
            selected_text = current.content[start:end]
            if not selected_text or selected_text != command.scope.get("selectedText"):
                raise ConflictError("选中的文字已变化，请重新选择后改写")
            input_text = selected_text
            replace_range = (start, end)
        elif scope_type != "document":
            raise ValidationError("改写范围只支持全文或选中内容")
        if not input_text.strip():
            raise ValidationError("没有可改写的文案")
        try:
            rewrite_prompt = get_prompt_by_style_id(command.style_id)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        candidates = []
        mode = "demo"
        rewrite_started_at = perf_counter()
        try:
            for _ in range(command.candidate_count):
                result, mode = run_normal_rewrite(input_text, rewrite_prompt.prompt_id, command.prompt, command.model_id, command.strength)
                candidates.append(result)
        except Exception as exc:
            detail = exc.message if isinstance(exc, ModelCallError) else "本次改写未能完成，请重试或切换模型后再试。"
            raise UpstreamError(detail) from exc
        candidate_outputs = [
            current.content[:replace_range[0]] + item + current.content[replace_range[1]:] if replace_range else item
            for item in candidates
        ]
        output_text = candidate_outputs[0]
        revision = rewrite_repo.add_working_revision(db, session_id=session.id, parent_revision_id=current.id, content=output_text)
        session.current_revision_id = revision.id
        run = rewrite_repo.add_rewrite_run(db, 
            session_id=session.id,
            input_revision_id=current.id,
            model_id=command.model_id,
            style_id=rewrite_prompt.style_id,
            prompt_id=rewrite_prompt.prompt_id,
            prompt=command.prompt,
            candidates=candidate_outputs,
            selected_index=0,
            mode=mode,
        )
        db.commit()
        db.refresh(revision)
        db.refresh(run)
        elapsed_ms = int((perf_counter() - rewrite_started_at) * 1000)
        logger.info(
            "event=rewrite.completed run_id=%s session_id=%s model=%s style_id=%s prompt_id=%s scope=%s mode=%s candidates=%s elapsed_ms=%s",
            run.id,
            session.id,
            command.model_id,
            rewrite_prompt.style_id,
            rewrite_prompt.prompt_id,
            scope_type,
            mode,
            len(candidates),
            elapsed_ms,
        )
        return {
            "runId": run.id,
            "workingRevisionId": revision.id,
            "content": output_text,
            "candidates": candidate_outputs,
            "selectedIndex": 0,
            "mode": mode,
            "diffSegments": comparison_segments(db, session, revision, user_id),
        }
    except Exception:
        db.rollback()
        raise


def select_rewrite_candidate(db: Session, *, run_id: str, candidate_index: int, user_id: str) -> dict:
    try:
        run = require_owned(db, RewriteRun, run_id, user_id, "改写记录")
        if candidate_index >= len(run.candidates):
            raise ValidationError("候选编号无效")
        session = require_owned(db, RewriteSession, run.session_id, user_id, "改写会话")
        current = working_content(db, session, user_id)
        # 候选文本基于改写开始时的版本；切换候选时保留为同一版本下的分支。
        parent = rewrite_repo.get_working_revision(db, run.input_revision_id)
        if not parent or current.parent_revision_id != parent.id:
            raise ConflictError("中间文案已继续修改，不能应用较早的候选")
        selected = run.candidates[candidate_index]
        revision = rewrite_repo.add_working_revision(db, session_id=session.id, parent_revision_id=run.input_revision_id, content=selected)
        session.current_revision_id = revision.id
        run.selected_index = candidate_index
        db.commit()
        db.refresh(revision)
        logger.info("已应用改写候选，run_id=%s, candidate=%s", run.id, candidate_index)
        return {"workingRevisionId": revision.id, "content": revision.content, "diffSegments": comparison_segments(db, session, revision, user_id)}
    except Exception:
        db.rollback()
        raise
