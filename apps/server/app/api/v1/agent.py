"""Agent HTTP adapters: input conversion, SSE encoding and responses only."""
from __future__ import annotations
import json
from fastapi import Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi import APIRouter
from app.api.v1.deps import require_user
from app.application import agent_execution_service as execution
from app.application.agent_commands import MessageCommand, ActionCommand
from app.infrastructure.database import get_db
from app.prompts.agent_prompt.image_style import DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.rewrite import DEFAULT_REWRITE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.storyboard import DEFAULT_STORYBOARD_PROMPT_VERSION_ID
from app.api.v1.deps import current_user_id
from app.api.v1.deps import require_thread_lease

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


class ThreadCreate(BaseModel):
    workspaceId: str

class AgentThreadListItem(BaseModel):
    id: str
    title: str
    preview: str
    messageCount: int
    createdAt: str
    lastActivityAt: str

class AgentMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=200000)
    modelId: str = Field(default='deepseek-v4-flash', pattern='^(deepseek-v4-flash|qwen3\\.7-plus|doubao-2\\.1-pro)$')
    contextText: str | None = Field(default=None, max_length=200000)
    subtitleFileId: str | None = None
    rewritePromptVersionId: str = DEFAULT_REWRITE_PROMPT_VERSION_ID
    storyboardPromptVersionId: str = DEFAULT_STORYBOARD_PROMPT_VERSION_ID
    imageStylePromptVersionId: str = DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
    retryMessageId: str | None = None
    requestId: str | None = Field(default=None, min_length=1, max_length=80)

class ActionResolve(BaseModel):
    decision: str = Field(pattern='^(approve|reject|retry_preview)$')
    selectedPreviewIndex: int | None = Field(default=None, ge=0, le=1)
    stylePrompt: str | None = Field(default=None, max_length=12000)
    imageStylePromptVersionId: str | None = None
def _sse_events(events):
    for event in events:
        if event is None:
            yield "data: [DONE]\n\n"
        else:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

@router.post('/agent/threads')
def create_thread(body: ThreadCreate, db: Session=Depends(get_db)):
    return execution.create_thread(db, body.workspaceId, current_user_id() or '')

@router.get('/workspaces/{workspace_id}/agent/threads')
def list_agent_threads(workspace_id: str, q: str=Query(default='', max_length=200), offset: int=Query(default=0, ge=0), limit: int=Query(default=40, ge=1, le=100), db: Session=Depends(get_db)):
    return execution.list_agent_threads(db, workspace_id, current_user_id() or '', q, offset, limit)

@router.get('/agent/threads/{thread_id}/messages')
def get_thread_messages(thread_id: str, db: Session=Depends(get_db)):
    return execution.get_thread_messages(db, thread_id, current_user_id() or '')

@router.post('/agent/threads/{thread_id}/messages')
def send_agent_message(thread_id: str, body: AgentMessageRequest, db: Session=Depends(get_db), _thread_lease=Depends(require_thread_lease)):
    return execution.send_agent_message(db, thread_id, MessageCommand(**body.model_dump()), current_user_id() or '')

@router.post('/agent/threads/{thread_id}/messages/stream')
def stream_agent_message(thread_id: str, body: AgentMessageRequest, db: Session=Depends(get_db), _thread_lease=Depends(require_thread_lease)):
    """通过可暂停的 LangGraph 流程处理智能体对话。"""
    events = execution.stream_agent_message(db, thread_id, MessageCommand(**body.model_dump()), current_user_id() or '')
    return StreamingResponse(_sse_events(events), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@router.post('/agent/threads/{thread_id}/actions/{action_id}/resolve')
def resolve_agent_action(thread_id: str, action_id: str, body: ActionResolve, db: Session=Depends(get_db), _thread_lease=Depends(require_thread_lease)):
    return execution.resolve_agent_action(db, thread_id, action_id, ActionCommand(**body.model_dump()), current_user_id() or '')
