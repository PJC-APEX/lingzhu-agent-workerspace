"""HTTP adapters for workspaces, rewrites and prompt management."""
from __future__ import annotations

from typing import Literal
from fastapi import Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fastapi import APIRouter
from app.api.v1.deps import require_user
from app.application import model_catalog, prompt_service, rewrite_service, workspace_service
from app.application.rewrite_commands import RewriteCommand
from app.infrastructure.database import get_db
from app.api.v1.deps import current_user_id


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


class WorkspaceCreate(BaseModel):
    name: str = Field(default="我的工作区", max_length=120)


class SessionCreate(BaseModel):
    documentId: str
    sourceRevisionId: str


class ComparisonBaselineUpdate(BaseModel):
    documentId: str
    revisionId: str


class SessionUpdate(BaseModel):
    baseWorkingRevisionId: str
    content: str


class RewriteRequest(BaseModel):
    baseWorkingRevisionId: str
    scope: dict = Field(default_factory=lambda: {"type": "document"})
    modelId: str = "deepseek-v4-flash"
    styleId: str = "humanize"
    prompt: str = ""
    strength: int = Field(default=55, ge=0, le=100)
    candidateCount: int = Field(default=1, ge=1, le=3)


class CandidateSelect(BaseModel):
    candidateIndex: int = Field(ge=0)


class AgentPromptCreate(BaseModel):
    kind: Literal["rewrite", "storyboard", "image_style"]
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=12_000)


class AgentPromptVersionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=12_000)


@router.get("/workspaces")
def get_workspaces(db: Session = Depends(get_db)):
    return workspace_service.list_workspaces(db, user_id=current_user_id() or "")


@router.post("/workspaces")
def create_workspace(body: WorkspaceCreate, db: Session = Depends(get_db)):
    return workspace_service.create_workspace(db, name=body.name, user_id=current_user_id() or "")


@router.post("/workspaces/{workspace_id}/rewrite-sessions")
def create_rewrite_session(workspace_id: str, body: SessionCreate, db: Session = Depends(get_db)):
    return rewrite_service.create_rewrite_session(
        db, workspace_id=workspace_id, document_id=body.documentId,
        source_revision_id=body.sourceRevisionId, user_id=current_user_id() or "",
    )


@router.put("/rewrite-sessions/{session_id}/comparison-baseline")
def update_comparison_baseline(session_id: str, body: ComparisonBaselineUpdate, db: Session = Depends(get_db)):
    return rewrite_service.update_comparison_baseline(
        db, session_id=session_id, document_id=body.documentId,
        revision_id=body.revisionId, user_id=current_user_id() or "",
    )


@router.get("/rewrite-sessions/{session_id}")
def get_rewrite_session(session_id: str, db: Session = Depends(get_db)):
    return rewrite_service.get_rewrite_session(db, session_id=session_id, user_id=current_user_id() or "")


@router.patch("/rewrite-sessions/{session_id}")
def update_rewrite_session(session_id: str, body: SessionUpdate, db: Session = Depends(get_db)):
    return rewrite_service.update_rewrite_session(
        db, session_id=session_id, base_revision_id=body.baseWorkingRevisionId,
        content=body.content, user_id=current_user_id() or "",
    )


@router.post("/rewrite-sessions/{session_id}/rewrite-runs")
def run_rewrite(session_id: str, body: RewriteRequest, db: Session = Depends(get_db)):
    command = RewriteCommand(
        base_revision_id=body.baseWorkingRevisionId, scope=body.scope,
        model_id=body.modelId, style_id=body.styleId, prompt=body.prompt,
        strength=body.strength, candidate_count=body.candidateCount,
    )
    return rewrite_service.run_rewrite(db, session_id=session_id, command=command, user_id=current_user_id() or "")


@router.post("/rewrite-runs/{run_id}/select")
def select_rewrite_candidate(run_id: str, body: CandidateSelect, db: Session = Depends(get_db)):
    return rewrite_service.select_rewrite_candidate(
        db, run_id=run_id, candidate_index=body.candidateIndex, user_id=current_user_id() or "",
    )


@router.get("/rewrite-sessions/{session_id}/comparison")
def compare_rewrite_session(session_id: str, db: Session = Depends(get_db)):
    return rewrite_service.get_rewrite_session(db, session_id=session_id, user_id=current_user_id() or "")


@router.get("/models")
def get_models(capability: str = "rewrite"):
    return model_catalog.get_models(capability)


@router.get("/rewrite-presets")
def get_rewrite_presets():
    return model_catalog.get_rewrite_presets()


@router.get("/agent/prompts")
def get_agent_prompts(db: Session = Depends(get_db)):
    return prompt_service.list_prompts(db, user_id=current_user_id() or "")


@router.post("/agent/prompts")
def create_agent_prompt(body: AgentPromptCreate, db: Session = Depends(get_db)):
    return prompt_service.create_prompt(
        db, kind=body.kind, name=body.name, content=body.content, user_id=current_user_id() or "",
    )


@router.post("/agent/prompts/{prompt_id}/versions")
def create_agent_prompt_revision(prompt_id: str, body: AgentPromptVersionCreate, db: Session = Depends(get_db)):
    return prompt_service.create_version(
        db, prompt_id=prompt_id, content=body.content, user_id=current_user_id() or "",
    )


@router.put("/agent/prompts/{prompt_id}/versions/{version_id}")
def update_agent_prompt_revision(prompt_id: str, version_id: str, body: AgentPromptVersionCreate, db: Session = Depends(get_db)):
    return prompt_service.update_version(
        db, prompt_id=prompt_id, version_id=version_id,
        content=body.content, user_id=current_user_id() or "",
    )
