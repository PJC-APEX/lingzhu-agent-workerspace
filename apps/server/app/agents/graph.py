from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.prompts.agent_prompt.chat import build_agent_chat_system_prompt
from app.services.model_gateway import ModelUnavailable, complete_chat, model_configured

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    messages: list[dict]
    context_text: str
    reply: str
    mode: str


def _demo_reply(messages: list[dict], context_text: str) -> str:
    last = messages[-1]["content"] if messages else ""
    if context_text and any(word in last for word in ("改写", "润色", "优化", "重写")):
        sentences = [item.strip() for item in context_text.splitlines() if item.strip()]
        return "我先按保留原意、表达更自然的方向整理了一版：\n\n" + "\n\n".join(sentences)
    return "收到。我会在这个对话里继续处理，不会修改中间的改写稿。当前是体验模式；配置模型密钥后可以切换为真实模型回答。"


def _respond(state: AgentState) -> AgentState:
    messages = state["messages"][-12:]
    context_text = state.get("context_text", "")
    prompt_messages = [{"role": "system", "content": build_agent_chat_system_prompt(context_text)}]
    prompt_messages.extend(messages)
    try:
        if model_configured():
            result = complete_chat(prompt_messages)
            return {**state, "reply": result.text, "mode": result.mode}
    except ModelUnavailable:
        pass
    except Exception:
        logger.exception("智能体模型调用失败")
        raise
    return {**state, "reply": _demo_reply(messages, context_text), "mode": "demo"}


def build_agent_graph():
    """以 LangGraph 保存独立的智能体对话处理入口。"""
    graph = StateGraph(AgentState)
    graph.add_node("respond", _respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)
    return graph.compile()


agent_graph = build_agent_graph()

