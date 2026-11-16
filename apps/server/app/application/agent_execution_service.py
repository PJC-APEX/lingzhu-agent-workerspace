"""Agent use-case coordination; emits plain events and never HTTP/SSE bytes.

Graph execution and legacy non-stream behavior are preserved independently.
HTTP callers pass an explicit user identity; worker recovery uses its own lease.
"""
from __future__ import annotations
from collections.abc import Iterator
import logging
import uuid
from time import perf_counter
from sqlalchemy.orm import Session
from app.agents.graph import agent_graph
from app.agents.memory_tasks import maybe_schedule_summary
from app.application import agent_service, agent_action_service
from app.application.agent_commands import MessageCommand, ActionCommand
from app.application.agent_result_service import persist_agent_graph_result
from app.application.workspace_service import require_workspace
from app.application.transactions import rollback_on_error
from app.domain.exceptions import AppError, ConflictError, InternalError
from app.domain.models import AgentAction, AgentThread, AgentTurnRequest, new_id
from app.graph.graph import get_agent_graph_state, stream_agent_graph, stream_detached_chat
from app.graph.nodes import classify_pending_confirmation
from app.infrastructure.queue import publish_job as launch_job
from app.repositories import agent_repo, subtitle_repo
from app.application.resource_presenter import present_resources
from app.services.model_gateway import ModelCallError
logger = logging.getLogger(__name__)





def replay_turn_request(turn: AgentTurnRequest, request_id: str, db: Session, user_id: str):
    if turn.status == 'processing':
        raise ConflictError('该请求正在处理中，请稍后查询或重试')
    response_message = agent_repo.get_message(db, turn.response_message_id) if turn.response_message_id else None

    def replay_events():
        yield {'type': 'message.started', 'messageId': response_message.id if response_message else new_id(), 'userMessageId': turn.user_message_id, 'requestId': request_id}
        if turn.status == 'failed':
            model_failure = bool(turn.error_category and turn.error_category.startswith('model:'))
            yield {'type': 'message.error', 'message': turn.error_message or '本次请求未能完成，请重试或切换模型后再试。', 'category': turn.error_category.split(':', 1)[1] if model_failure else turn.error_category or 'request', 'modelFailure': model_failure, 'userMessageId': turn.user_message_id, 'requestId': request_id}
        elif response_message:
            yield {'type': 'message.completed', 'message': {'id': response_message.id, 'role': response_message.role, 'content': response_message.content, 'type': response_message.message_type, 'metadata': present_resources(db, response_message.metadata_json or {}, user_id)}}
        yield None
    return replay_events()


def _resolved_action_response(action: AgentAction, decision: str) -> dict:
    """响应格式在这里定，判断规则在 application/agent_action_service.py。"""
    result = agent_action_service.already_processed_response(action, decision)
    return {'status': result.status, 'jobId': result.job_id}


@rollback_on_error
def create_thread(db: Session, workspace_id: str, user_id: str) -> dict:
    require_workspace(db, workspace_id, user_id)
    thread = AgentThread(workspace_id=workspace_id)
    agent_repo.add_record(db, thread)
    db.commit()
    db.refresh(thread)
    logger.info('智能体会话已创建，thread_id=%s', thread.id)
    return {'id': thread.id, 'title': thread.title}


def list_agent_threads(db: Session, workspace_id: str, user_id: str, q: str = "", offset: int = 0, limit: int = 40) -> dict:
    require_workspace(db, workspace_id, user_id)
    rows = agent_repo.list_thread_rows(db, workspace_id, q, offset, limit)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for thread, preview, message_count, last_activity_at in rows:
        items.append({'id': thread.id, 'title': thread.title or '新对话', 'preview': ' '.join((preview or '').split())[:180], 'messageCount': int(message_count), 'createdAt': thread.created_at.isoformat() if thread.created_at else '', 'lastActivityAt': last_activity_at.isoformat() if last_activity_at else ''})
    return {'items': items, 'hasMore': has_more, 'nextOffset': offset + len(items)}


def get_thread_messages(db: Session, thread_id: str, user_id: str) -> list[dict]:
    agent_service.get_thread(db, thread_id, user_id)
    messages = agent_repo.list_messages(db, thread_id)
    result = []
    for item in messages:
        metadata = dict(item.metadata_json or {})
        metadata = present_resources(db, metadata, user_id)
        if item.message_type == 'confirmation' and metadata.get('actionId'):
            action = agent_repo.get_action(db, metadata['actionId'])
            metadata['actionStatus'] = action.status if action else 'unknown'
        result.append({'id': item.id, 'role': item.role, 'content': item.content, 'type': item.message_type, 'metadata': metadata})
    return result


@rollback_on_error
def send_agent_message(db: Session, thread_id: str, body: MessageCommand, user_id: str) -> dict:
    thread = agent_service.get_thread(db, thread_id, user_id)
    agent_service.save_agent_message(db, thread.id, 'user', body.message, metadata={'hasContext': bool(body.contextText), 'subtitleFileId': body.subtitleFileId})
    wants_images = any((word in body.message for word in ('生成图片', '生成图', '生图', '分镜')))
    wants_video = any((word in body.message for word in ('生成视频', '视频草稿', '剪映', '配视频')))
    response_type = 'text'
    metadata = {}
    if wants_images:
        context = (body.contextText or '').strip()
        if len(context) < 8:
            response = '请先在输入框中点“附带中间文案”，或把最终文案发到对话里。我会先展示将用于分镜和生图的版本，等你确认后再开始。'
        else:
            action = AgentAction(thread_id=thread.id, action_type='generate_images', payload={'finalText': context})
            agent_repo.add_record(db, action)
            agent_repo.flush(db)
            response = '请确认下面这版文案用于分镜和生图。确认后会生成演示素材，并进入素材审核。'
            response_type = 'confirmation'
            metadata = {'actionId': action.id, 'actionType': action.action_type, 'previewText': context}
    elif wants_video:
        subtitle = subtitle_repo.get(db, body.subtitleFileId) if body.subtitleFileId else None
        image_job, image_error = agent_action_service.image_batch_readiness(db, thread)
        if not subtitle or subtitle.workspace_id != thread.workspace_id:
            response = '请先上传当前工作区的配音 SRT 字幕。'
        elif image_error:
            response = f'{image_error}。处理完成后再告诉我生成视频。'
        else:
            action = AgentAction(thread_id=thread.id, action_type='generate_video', payload={'subtitleFileId': subtitle.id, 'imageJobId': image_job.id})
            agent_repo.add_record(db, action)
            agent_repo.flush(db)
            response = '素材和字幕已就绪。确认后会按字幕时间匹配分镜图片，并在本机剪映草稿目录生成可继续编辑的草稿。'
            response_type = 'confirmation'
            metadata = {'actionId': action.id, 'actionType': action.action_type}
    else:
        history = agent_repo.recent_text_messages(db, thread.id)
        context_text = body.contextText or ''
        state = agent_graph.invoke({'messages': [{'role': item.role, 'content': item.content} for item in history if item.message_type == 'text'], 'context_text': context_text, 'reply': '', 'mode': 'demo'})
        response = state['reply']
        metadata = {'mode': state['mode']}
    assistant_message = agent_service.save_agent_message(db, thread.id, 'assistant', response, response_type, metadata)
    if thread.title == '新对话':
        thread.title = body.message[:40]
    db.commit()
    db.refresh(assistant_message)
    logger.info('智能体消息已处理，thread_id=%s, type=%s, mode=%s', thread.id, response_type, metadata.get('mode', 'tool' if response_type == 'confirmation' else 'demo'))
    return {'message': {'id': assistant_message.id, 'role': 'assistant', 'content': response, 'type': response_type, 'metadata': metadata}}


def stream_agent_message(db: Session, thread_id: str, body: MessageCommand, user_id: str) -> Iterator[dict | None]:
    """通过可暂停的 LangGraph 流程处理智能体对话。"""
    thread = agent_service.get_thread(db, thread_id, user_id)
    thread_id_value = thread.id
    request_hash = agent_service.fingerprint_request(body)
    request_id = body.requestId or str(uuid.uuid4())
    existing_turn = agent_service.find_existing_turn(db, thread.id, request_id, request_hash)
    if existing_turn:
        return replay_turn_request(existing_turn, request_id, db, user_id)
    try:
        ctx = agent_service.prepare_turn(db, thread, body, user_id, request_id=request_id)
    except agent_service._ReplayRequired as replay:
        return replay_turn_request(replay.turn, request_id, db, user_id)
    thread_id_value = ctx.thread_id
    attached_text = ctx.attached_text
    user_message = ctx.user_message
    turn_request = ctx.turn_request

    def model_events():
        stream_message_id = new_id()
        started_at = perf_counter()
        graph_values = {}
        active_interrupts = []
        yield {'type': 'message.started', 'messageId': stream_message_id, 'userMessageId': user_message.id, 'requestId': request_id}
        try:
            graph_state = get_agent_graph_state(thread_id_value)
            active_interrupts = graph_state.get('interrupts', [])
            graph_values = graph_state.get('state', {})
            logger.info('event=agent.graph_state_loaded request_id=%s thread_id=%s stage=%s pending_kind=%s pending_count=%s message_len=%s has_attachment=%s', request_id, thread_id_value, graph_values.get('stage', 'unknown'), active_interrupts[0].get('kind', 'unknown') if active_interrupts else 'none', len(active_interrupts), len(body.message), bool(attached_text))
            plan = agent_service.plan_resume(db, thread, body, ctx, active_interrupts, classify_pending_confirmation)
            if active_interrupts and (active_interrupts[0].get('kind') or '').startswith('confirm_'):
                pending_kind = active_interrupts[0].get('kind')
                if plan.resume is None:
                    logger.info('event=agent.confirmation_kept_open request_id=%s thread_id=%s pending_kind=%s reason=chat', request_id, thread_id_value, pending_kind)
                else:
                    logger.info('event=agent.confirmation_resolved request_id=%s thread_id=%s pending_kind=%s decision=%s target=%s action_source=typed_message', request_id, thread_id_value, pending_kind, plan.resume['decision'], plan.resume_target)
            resume = plan.resume
            detached_chat = plan.detached_chat
            input_state = agent_service.build_input_state(ctx, body, user_id)
            resume_state_update = agent_service.build_resume_state_update(ctx, user_id)
            if detached_chat:
                input_state.update({'intent': 'chat', 'rewrite_source': 'none', 'rewrite_requested': False, 'context_text': ''})
                graph_events = stream_detached_chat(input_state, request_id=request_id)
            else:
                graph_events = stream_agent_graph(thread_id_value, input_state=input_state if resume is None else None, resume=resume, resume_state_update=resume_state_update if resume is not None else None, request_id=request_id)
            result = None
            for event in graph_events:
                if event.get('kind') == 'delta':
                    yield {'type': 'message.delta', 'delta': event['delta']}
                elif event.get('kind') == 'finished':
                    result = event
            completed_message = persist_agent_graph_result(thread_id_value, result or {})
            completed_message = present_resources(db, completed_message, user_id)
            if agent_service.persist_turn_result(db, turn_request, completed_message):
                yield {'type': 'message.completed', 'message': completed_message}
            else:
                yield {'type': 'message.error', 'message': turn_request.error_message, 'category': 'request', 'modelFailure': False, 'userMessageId': user_message.id, 'requestId': request_id}
            try:
                maybe_schedule_summary(thread_id_value)
            except Exception:
                logger.exception('event=agent.summary_schedule_failed thread_id=%s', thread_id_value)
            yield None
        except Exception as exc:
            if isinstance(exc, AppError):
                error_message = exc.message
                logger.warning('event=agent.stream_rejected request_id=%s thread_id=%s status=%s detail=%s stage=%s pending_kind=%s message_len=%s elapsed_ms=%s', request_id, thread_id_value, exc.status_code, exc.message, graph_values.get('stage', 'unknown'), active_interrupts[0].get('kind', 'unknown') if active_interrupts else 'none', len(body.message), round((perf_counter() - started_at) * 1000))
            else:
                logger.exception('event=agent.stream_failed request_id=%s thread_id=%s elapsed_ms=%s', request_id, thread_id_value, round((perf_counter() - started_at) * 1000))
                error_message = exc.message if isinstance(exc, ModelCallError) else '本次请求未能完成，请重试或切换模型后再试。'
            db.rollback()
            agent_service.persist_turn_failure(db, turn_request, error_message, f'model:{exc.category}' if isinstance(exc, ModelCallError) else 'request')
            yield {'type': 'message.error', 'message': error_message, 'category': exc.category if isinstance(exc, ModelCallError) else 'request', 'modelFailure': isinstance(exc, ModelCallError), 'userMessageId': user_message.id, 'requestId': request_id}
            yield None
    return model_events()


@rollback_on_error
def resolve_agent_action(db: Session, thread_id: str, action_id: str, body: ActionCommand, user_id: str) -> dict:
    request_id = new_id()
    started_at = perf_counter()
    agent_service.get_thread(db, thread_id, user_id)
    action = agent_action_service.get_action_for_thread(db, action_id, thread_id)
    if action.status != 'awaiting_confirmation':
        return _resolved_action_response(action, body.decision)
    selected_style_prompt = agent_action_service.resolve_retry_preview_style(db, action, body, user_id)
    agent_action_service.validate_selected_preview(action, body.decision, body.selectedPreviewIndex)
    if not agent_action_service.claim_action(db, action_id, thread_id, body.decision):
        db.refresh(action)
        return _resolved_action_response(action, body.decision)
    action.status = 'confirmed' if body.decision == 'approve' else 'rejected'
    if action.action_type.startswith('graph_confirm_'):
        graph_decision = 'revise' if body.decision in {'reject', 'retry_preview'} else 'approve'
        if graph_decision == 'approve':
            agent_action_service.apply_confirmation_side_effects(db, action, body.decision)
        db.commit()
        logger.info('event=agent.confirmation_resolve_started request_id=%s thread_id=%s action_id=%s action_type=%s decision=%s', request_id, thread_id, action_id, action.action_type, graph_decision)
        resume = {'decision': graph_decision}
        resume_state_update = {'user_id': user_id}
        if body.decision == 'approve' and action.action_type == 'graph_confirm_image_style':
            resume['selectedPreviewIndex'] = body.selectedPreviewIndex
        if body.decision == 'retry_preview':
            resume.update({'instruction': body.stylePrompt, 'target': 'style', 'replaceStyle': True})
            resume_state_update = {'user_id': user_id, 'image_style_prompt_id': selected_style_prompt.prompt_id, 'image_style_prompt_version_id': selected_style_prompt.version_id, 'image_style_prompt_version': selected_style_prompt.version, 'image_style_prompt': body.stylePrompt.strip()}
        result = None
        try:
            for event in stream_agent_graph(thread_id, resume=resume, resume_state_update=resume_state_update, request_id=request_id):
                if event.get('kind') == 'finished':
                    result = event
            assistant_message = persist_agent_graph_result(thread_id, result or {})
            assistant_message = present_resources(db, assistant_message, user_id)
        except Exception as exc:
            logger.exception('event=agent_graph.resume_failed request_id=%s thread_id=%s action_id=%s decision=%s elapsed_ms=%s', request_id, thread_id, action_id, graph_decision, round((perf_counter() - started_at) * 1000))
            raise InternalError('智能体流程恢复失败，请查看后端日志') from exc
        pending = (result or {}).get('interrupts', [])
        pending_job_id = next((item.get('jobId') for item in pending if item.get('kind') in {'image_job', 'image_preview_job'}), None)
        logger.info('event=agent_graph.resumed request_id=%s thread_id=%s action_id=%s decision=%s result_stage=%s pending_kind=%s elapsed_ms=%s', request_id, thread_id, action_id, graph_decision, (result or {}).get('state', {}).get('stage', 'unknown'), pending[0].get('kind') if pending else 'none', round((perf_counter() - started_at) * 1000))
        return {'status': 'queued' if pending_job_id else 'continued', 'jobId': pending_job_id, 'message': assistant_message}
    if body.decision == 'reject':
        agent_service.save_agent_message(db, thread_id, 'assistant', '已取消这次操作。')
        db.commit()
        logger.info('用户拒绝智能体操作，action_id=%s', action.id)
        return {'status': 'rejected'}
    thread = agent_service.get_thread(db, thread_id, user_id)
    creation = agent_action_service.create_job_for_action(db, thread, action, user_id)
    job = creation.job
    logger.info('已确认并创建后台任务，action_id=%s, job_id=%s, type=%s', action.id, job.id, creation.job_type)
    try:
        launch_job(job.id)
    except Exception as exc:
        logger.exception('后台任务无法投递到 Redis 队列，job_id=%s', job.id)
        job.message = '队列暂不可用，系统将自动重试投递'
        job.error = '等待任务队列恢复'
        db.commit()
    return {'status': 'queued', 'jobId': job.id}
