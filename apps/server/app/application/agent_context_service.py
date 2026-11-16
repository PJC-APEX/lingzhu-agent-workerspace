"""Load conversation history and pass a detached snapshot to rewrite capability."""
from app.core.container import get_session_factory
from app.repositories import agent_repo
from app.services.agent_rewrite.service import prepare_agent_rewrite as prepare_rewrite


def prepare_agent_rewrite(thread_id, message, context_text, model_id, **options):
    with get_session_factory()() as db:
        thread = agent_repo.get_thread(db, thread_id)
        summary = thread.summary_text or "" if thread else ""
        rows = agent_repo.text_history(db, thread_id, options.get("current_message_id"))
        if thread and thread.summary_through_message_id:
            marker = next((i for i, item in enumerate(rows) if item.id == thread.summary_through_message_id), -1)
            if marker >= 0:
                rows = rows[marker + 1:]
        history = [{"role": item.role, "content": item.content, "metadata": dict(item.metadata_json or {})}
                   for item in rows]
    return prepare_rewrite(
        thread_id, message, context_text, model_id,
        history=history, summary=summary, **options,
    )
