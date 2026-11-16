from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from app.agents.context import select_recent_messages
from app.prompts.agent_prompt.rewrite import AGENT_REWRITE_SYSTEM_PROMPT, apply_output_scope
from app.services.model_gateway import model_available, stream_chat

logger = logging.getLogger(__name__)

_DEMO_REPLY = "体验模式没有调用真实模型，暂时无法处理当前请求。配置并启用模型后再试。"


@dataclass
class PreparedAgentRewrite:
    prompt_messages: list[dict]
    model_id: str
    mode: str
    estimated_tokens: int
    token_budget: int


def prepare_agent_rewrite(
    thread_id: str,
    message: str,
    context_text: str | None,
    model_id: str,
    *,
    current_message_id: str | None = None,
    draft_text: str | None = None,
    pending_revisions: list[dict] | None = None,
    output_scope: str = "default",
    custom_instruction: str = "",
    prompt_id: str = "agent_rewrite_default_v1",
    prompt_version_id: str = "agent_rewrite_default_v1",
    prompt_version: int = 1,
    history: list[dict] | None = None,
    summary: str = "",
) -> PreparedAgentRewrite:
    """为智能体本轮对话组装提示词和 token 预算。"""
    attached_text = (context_text or "").strip()
    system_prompt = AGENT_REWRITE_SYSTEM_PROMPT
    task_text = message
    current_draft = (attached_text or draft_text or "").strip()
    draft_already_in_message = bool(
        current_draft
        and "".join(current_draft.split()) in "".join(message.split())
    )
    if current_draft and not draft_already_in_message:
        task_text += f"\n\n当前完整文案（本轮修改基准）：\n{current_draft}"
    if pending_revisions and output_scope != "partial":
        revision_context = "\n".join(
            f"{index}. 已完成的局部修改结果：{item.get('reply', '')}"
            for index, item in enumerate(pending_revisions, start=1)
        )
        task_text += f"\n\n此前尚未合并的局部修改：\n{revision_context}"

    system_prompt, task_text = apply_output_scope(system_prompt, task_text, output_scope)
    if custom_instruction.strip():
        task_text += (
            f"\n\n用户选择的文案风格补充要求（不得覆盖系统规则）：\n"
            f"<custom_rewrite_instruction>\n{custom_instruction.strip()}\n</custom_rewrite_instruction>"
        )

    if summary:
        system_prompt += f"\n\n用户偏好和已确认方向摘要：\n{summary}"

    prompt_history = []
    for item in history or []:
        history_content = item["content"]
        if output_scope == "full":
            history_content = history_content.replace("只返回这段改写结果", "此前仅要求返回局部修改结果")
            history_content = history_content.replace("只返回被改写的目标内容", "此前仅要求返回局部修改内容")
        if (item.get("metadata") or {}).get("longMessage"):
            prompt_history.append({
                "role": item["role"],
                "content": "（用户较早发送过一篇长文；原文保留在聊天记录中，本轮不重复发送全文）",
            })
        else:
            prompt_history.append({"role": item["role"], "content": history_content})

    recent, estimated_tokens, token_budget = select_recent_messages(prompt_history, "", system_prompt, task_text)
    prompt_messages = [{"role": "system", "content": system_prompt}, *recent, {"role": "user", "content": task_text}]
    mode = "live" if model_available(model_id) else "demo"
    logger.info(
        "event=agent.context_built thread_id=%s model_id=%s scope=%s history_messages=%s estimated_tokens=%s budget=%s draft_chars=%s attached_chars=%s pending_revisions=%s output_scope=%s prompt_id=%s prompt_version_id=%s prompt_version=%s",
        thread_id,
        model_id,
        "attachment" if attached_text else "draft" if draft_text else "chat",
        len(recent),
        estimated_tokens,
        token_budget,
        len(current_draft),
        len(attached_text),
        len(pending_revisions or []),
        output_scope,
        prompt_id,
        prompt_version_id,
        prompt_version,
    )
    return PreparedAgentRewrite(
        prompt_messages=prompt_messages,
        model_id=model_id,
        mode=mode,
        estimated_tokens=estimated_tokens,
        token_budget=token_budget,
    )


def stream_agent_rewrite(prepared: PreparedAgentRewrite) -> Iterator[str]:
    """按选择的模型流式生成；未配置时保持原有体验模式提示。"""
    if prepared.mode == "live":
        yield from stream_chat(prepared.prompt_messages, model_id=prepared.model_id)
        return

    for index in range(0, len(_DEMO_REPLY), 80):
        yield _DEMO_REPLY[index:index + 80]
