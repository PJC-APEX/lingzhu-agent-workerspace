"""Ownership, atomicity and version behavior after moving rewrite use cases."""
from dataclasses import replace

import pytest

from app.application import prompt_service, rewrite_service
from app.application.rewrite_commands import RewriteCommand
from app.domain.exceptions import ConflictError, NotFoundError, UpstreamError, ValidationError
from app.domain.models import (
    AgentPromptPreset, AgentPromptVersion, Document, DocumentRevision,
    RewriteRun, RewriteSession, WorkingRevision, Workspace,
)


@pytest.fixture
def workbench(client):
    workspace = client.post("/api/v1/workspaces", json={"name": "改写分层"}).json()
    source = client.post(
        f"/api/v1/workspaces/{workspace['id']}/documents",
        files={"file": ("source.txt", "前😀原文后".encode(), "text/plain")},
    ).json()
    session = client.post(
        f"/api/v1/workspaces/{workspace['id']}/rewrite-sessions",
        json={"documentId": source["documentId"], "sourceRevisionId": source["revisionId"]},
    ).json()
    factory = client.app.state.testing_session_factory
    with factory() as db:
        user_id = db.get(Workspace, workspace["id"]).owner_user_id
    return factory, user_id, workspace, source, session


def command(session, **changes):
    return replace(RewriteCommand(
        base_revision_id=session["workingRevisionId"], scope={"type": "document"},
        model_id="deepseek-v4-flash", style_id="humanize", prompt="", strength=55,
        candidate_count=1,
    ), **changes)


def test_selection_rewrite_and_candidate_switch_preserve_unicode_and_version_chain(workbench, monkeypatch):
    factory, user_id, _, _, session = workbench
    replies = iter(["新稿一", "新稿二"])
    inputs = []

    def generate(text, *args):
        inputs.append(text)
        return next(replies), "demo"

    monkeypatch.setattr(rewrite_service, "run_normal_rewrite", generate)
    with factory() as db:
        result = rewrite_service.run_rewrite(
            db, session_id=session["id"], user_id=user_id,
            command=command(session, candidate_count=2,
                            scope={"type": "selection", "from": 3, "to": 5, "selectedText": "原文"}),
        )
    assert inputs == ["原文", "原文"]
    assert result["candidates"] == ["前😀新稿一后", "前😀新稿二后"]
    with factory() as db:
        selected = rewrite_service.select_rewrite_candidate(
            db, run_id=result["runId"], candidate_index=1, user_id=user_id,
        )
        revision = db.get(WorkingRevision, selected["workingRevisionId"])
        assert revision.parent_revision_id == session["workingRevisionId"]
        assert db.get(RewriteRun, result["runId"]).selected_index == 1
    assert selected["content"] == "前😀新稿二后"


@pytest.mark.parametrize("scope,error,message", [
    ({"type": "selection", "from": "bad", "to": 5}, ValidationError, "选区无效，请重新选择后改写"),
    ({"type": "selection", "from": 3, "to": 5, "selectedText": "已变化"}, ConflictError, "选中的文字已变化，请重新选择后改写"),
    ({"type": "unknown"}, ValidationError, "改写范围只支持全文或选中内容"),
])
def test_invalid_scope_keeps_contract_and_never_calls_model(workbench, monkeypatch, scope, error, message):
    factory, user_id, _, _, session = workbench
    def unexpected(*args):
        pytest.fail("invalid scope reached model generation")
    monkeypatch.setattr(rewrite_service, "run_normal_rewrite", unexpected)
    with factory() as db, pytest.raises(error, match=message):
        rewrite_service.run_rewrite(db, session_id=session["id"], user_id=user_id,
                                    command=command(session, scope=scope))


def test_model_failure_does_not_create_revision_or_run(workbench, monkeypatch):
    factory, user_id, _, _, session = workbench
    def fail(*args):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(rewrite_service, "run_normal_rewrite", fail)
    with factory() as db:
        with pytest.raises(UpstreamError, match="本次改写未能完成"):
            rewrite_service.run_rewrite(db, session_id=session["id"], user_id=user_id,
                                        command=command(session))
        assert db.query(RewriteRun).count() == 0
        assert db.query(WorkingRevision).count() == 1
        assert db.get(RewriteSession, session["id"]).current_revision_id == session["workingRevisionId"]


def test_run_insert_failure_rolls_back_flushed_revision_and_current_pointer(workbench, monkeypatch):
    factory, user_id, _, _, session = workbench
    monkeypatch.setattr(rewrite_service, "run_normal_rewrite", lambda *args: ("新稿", "demo"))
    def fail(*args, **kwargs):
        raise RuntimeError("run insert failed")
    monkeypatch.setattr(rewrite_service.rewrite_repo, "add_rewrite_run", fail)
    with factory() as db:
        with pytest.raises(RuntimeError, match="run insert failed"):
            rewrite_service.run_rewrite(db, session_id=session["id"], user_id=user_id,
                                        command=command(session))
        assert db.query(WorkingRevision).count() == 1
        assert db.query(RewriteRun).count() == 0
        assert db.get(RewriteSession, session["id"]).current_revision_id == session["workingRevisionId"]


def test_services_enforce_ownership_without_http_dependencies(workbench):
    factory, user_id, workspace, source, session = workbench
    with factory() as db:
        for call in (
            lambda: rewrite_service.get_rewrite_session(db, session_id=session["id"], user_id="stranger"),
            lambda: rewrite_service.create_rewrite_session(db, workspace_id=workspace["id"],
                document_id=source["documentId"], source_revision_id=source["revisionId"], user_id="stranger"),
            lambda: rewrite_service.update_rewrite_session(db, session_id=session["id"],
                base_revision_id=session["workingRevisionId"], content="越权", user_id="stranger"),
        ):
            with pytest.raises(NotFoundError):
                call()
        assert db.query(WorkingRevision).count() == 1


def test_snapshot_rejects_foreign_baseline_even_when_session_is_owned(workbench):
    factory, user_id, _, _, session = workbench
    with factory() as db:
        foreign = Workspace(name="foreign", owner_user_id="stranger")
        db.add(foreign)
        db.flush()
        document = Document(workspace_id=foreign.id, file_name="foreign.txt")
        db.add(document)
        db.flush()
        revision = DocumentRevision(document_id=document.id, content="private")
        db.add(revision)
        db.flush()
        db.get(RewriteSession, session["id"]).comparison_revision_id = revision.id
        db.commit()
        with pytest.raises(NotFoundError, match="找不到对比原文版本"):
            rewrite_service.get_rewrite_session(db, session_id=session["id"], user_id=user_id)


def test_stale_working_revision_keeps_409_wording(workbench):
    factory, user_id, _, _, session = workbench
    with factory() as db:
        rewrite_service.update_rewrite_session(db, session_id=session["id"],
            base_revision_id=session["workingRevisionId"], content="已编辑", user_id=user_id)
        with pytest.raises(ConflictError, match="中间文案已更新，请刷新后再保存"):
            rewrite_service.update_rewrite_session(db, session_id=session["id"],
                base_revision_id=session["workingRevisionId"], content="旧请求", user_id=user_id)


def test_prompt_version_failure_rolls_back_new_preset(workbench, monkeypatch):
    factory, user_id, *_ = workbench
    def fail(*args, **kwargs):
        raise RuntimeError("version insert failed")
    monkeypatch.setattr(prompt_service.prompt_repo, "add_version", fail)
    with factory() as db:
        with pytest.raises(RuntimeError, match="version insert failed"):
            prompt_service.create_prompt(db, kind="rewrite", name="测试", content="规则", user_id=user_id)
        assert db.query(AgentPromptPreset).count() == 0
        assert db.query(AgentPromptVersion).count() == 0


def test_prompt_service_ownership_and_override_restriction(workbench):
    factory, user_id, *_ = workbench
    with factory() as db:
        prompt = prompt_service.create_prompt(db, kind="rewrite", name="改写", content="规则", user_id=user_id)
        with pytest.raises(NotFoundError, match="找不到自定义提示词"):
            prompt_service.create_version(db, prompt_id=prompt["promptId"], content="越权", user_id="stranger")
        with pytest.raises(ValidationError, match="只有生图风格提示词支持覆盖保存"):
            prompt_service.update_version(db, prompt_id=prompt["promptId"], version_id=prompt["versionId"],
                                          content="修改", user_id=user_id)
        versions = prompt_service.list_prompts(db, user_id=user_id)["rewrite"][-1]["versions"]
        assert versions == [{"versionId": prompt["versionId"], "version": 1, "content": "规则"}]
