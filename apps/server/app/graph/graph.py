from __future__ import annotations

import logging
from collections.abc import Iterator

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graph.checkpointer import open_checkpointer
from app.graph.nodes import (
    confirm_final_node,
    confirm_images_node,
    confirm_storyboard_node,
    assemble_jianying_draft_node,
    intent_node,
    launch_images_node,
    launch_image_preview_node,
    prepare_final_node,
    revise_images_node,
    revise_storyboard_node,
    route_turn,
    rewrite_node,
    storyboard_node,
    storyboard_confirmation_help_node,
    wait_image_job_node,
    wait_image_preview_node,
    confirm_image_style_node,
    wait_srt_node,
)
from app.graph.state import AgentGraphState

logger = logging.getLogger(__name__)


def build_agent_graph(checkpointer=None):
    builder = StateGraph(AgentGraphState)
    builder.add_node("rewrite", rewrite_node)
    builder.add_node("intent", intent_node)
    builder.add_node("prepare_final", prepare_final_node)
    builder.add_node("confirm_final", confirm_final_node)
    builder.add_node("storyboard", storyboard_node)
    builder.add_node("confirm_storyboard", confirm_storyboard_node)
    builder.add_node("storyboard_confirmation_help", storyboard_confirmation_help_node)
    builder.add_node("revise_storyboard", revise_storyboard_node)
    builder.add_node("launch_images", launch_images_node)
    builder.add_node("launch_image_preview", launch_image_preview_node)
    builder.add_node("wait_image_preview", wait_image_preview_node)
    builder.add_node("confirm_image_style", confirm_image_style_node)
    builder.add_node("wait_image_job", wait_image_job_node)
    builder.add_node("confirm_images", confirm_images_node)
    builder.add_node("revise_images", revise_images_node)
    builder.add_node("wait_srt", wait_srt_node)
    builder.add_node("assemble_jianying_draft", assemble_jianying_draft_node)

    builder.add_edge(START, "intent")
    builder.add_conditional_edges("intent", route_turn, {
        "rewrite": "rewrite",
        "storyboard_confirmation_help": "storyboard_confirmation_help",
        "prepare_final": "prepare_final",
        "storyboard": "storyboard",
        "revise_storyboard": "revise_storyboard",
        "revise_images": "revise_images",
        "launch_images": "launch_images",
        "wait_srt": "wait_srt",
        "export_timeline": "assemble_jianying_draft",
    })
    builder.add_edge("rewrite", END)
    builder.add_edge("storyboard_confirmation_help", END)
    builder.add_conditional_edges("prepare_final", lambda state: "confirm" if state.get("stage") == "confirm_final" else "end", {
        "confirm": "confirm_final",
        "end": END,
    })
    builder.add_conditional_edges("confirm_final", lambda state: "storyboard" if state.get("stage") == "storyboard" else "end", {
        "storyboard": "storyboard",
        "end": END,
    })
    builder.add_conditional_edges("storyboard", lambda state: "confirm" if state.get("stage") == "confirm_storyboard" else "end", {
        "confirm": "confirm_storyboard",
        "end": END,
    })
    builder.add_conditional_edges("confirm_storyboard", lambda state: "preview" if state.get("stage") == "image_preview_generation" else "images" if state.get("stage") == "image_generation" else "end", {
        "preview": "launch_image_preview",
        "images": "launch_images",
        "end": END,
    })
    builder.add_conditional_edges("launch_image_preview", lambda state: "wait" if state.get("stage") == "waiting_image_preview" else "end", {
        "wait": "wait_image_preview",
        "end": END,
    })
    builder.add_conditional_edges("wait_image_preview", lambda state: "confirm" if state.get("stage") == "confirm_image_style" else "end", {
        "confirm": "confirm_image_style",
        "end": END,
    })
    builder.add_conditional_edges("confirm_image_style", lambda state: "images" if state.get("stage") == "image_generation" else "end", {
        "images": "launch_images",
        "end": END,
    })
    builder.add_conditional_edges("revise_storyboard", lambda state: "confirm" if state.get("stage") == "confirm_storyboard" else "end", {
        "confirm": "confirm_storyboard",
        "end": END,
    })
    builder.add_conditional_edges("launch_images", lambda state: "wait" if state.get("stage") == "waiting_image_job" else "end", {
        "wait": "wait_image_job",
        "end": END,
    })
    builder.add_conditional_edges("wait_image_job", lambda state: "review" if state.get("stage") == "confirm_images" else "end", {
        "review": "confirm_images",
        "end": END,
    })
    builder.add_conditional_edges("confirm_images", lambda state: state.get("stage", "end"), {
        "waiting_srt": "wait_srt",
        "revise_images": END,
        "end": END,
    })
    builder.add_edge("revise_images", "launch_images")
    builder.add_conditional_edges("wait_srt", lambda state: "export" if state.get("stage") == "timeline_export" else "end", {
        "export": "assemble_jianying_draft",
        "end": END,
    })
    builder.add_edge("assemble_jianying_draft", END)
    return builder.compile(checkpointer=checkpointer)


def stream_detached_chat(input_state: dict, request_id: str | None = None) -> Iterator[dict]:
    """Answer a chat turn without resuming or changing an interrupted workflow."""
    builder = StateGraph(AgentGraphState)
    builder.add_node("rewrite", rewrite_node)
    builder.add_edge(START, "rewrite")
    builder.add_edge("rewrite", END)
    graph = builder.compile()
    state: dict = {}
    for mode, chunk in graph.stream(input_state, stream_mode=["custom", "values"]):
        if mode == "custom" and isinstance(chunk, dict) and chunk.get("type") == "message.delta":
            yield {"kind": "delta", "delta": chunk["delta"]}
        elif mode == "values":
            state = dict(chunk)
    logger.info(
        "event=agent_graph.detached_chat_completed request_id=%s thread_id=%s chars=%s",
        request_id or "-",
        input_state.get("thread_id", "unknown"),
        len(state.get("reply", "")),
    )
    yield {"kind": "finished", "state": state, "interrupts": []}


def _interrupt_values(snapshot) -> list[dict]:
    pending = []
    for task in getattr(snapshot, "tasks", ()):
        for item in getattr(task, "interrupts", ()):
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                pending.append(value)
    return pending


def stream_agent_graph(
    thread_id: str,
    input_state: dict | None = None,
    resume: dict | None = None,
    resume_state_update: dict | None = None,
    request_id: str | None = None,
) -> Iterator[dict]:
    """运行一轮图流程；SSE 层订阅改写增量，完成后读取持久化状态。"""
    with open_checkpointer() as checkpointer:
        graph = build_agent_graph(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        graph_input = (
            Command(resume=resume, update=resume_state_update or {})
            if resume is not None
            else (input_state or {})
        )
        logger.info(
            "event=agent_graph.started request_id=%s thread_id=%s operation=%s input_stage=%s resume_decision=%s",
            request_id or "-",
            thread_id,
            "resume" if resume is not None else "turn",
            (input_state or {}).get("stage", "unknown"),
            (resume or {}).get("decision", "-"),
        )
        for mode, chunk in graph.stream(graph_input, config=config, stream_mode=["custom", "values"]):
            if mode == "custom" and isinstance(chunk, dict) and chunk.get("type") == "message.delta":
                yield {"kind": "delta", "delta": chunk["delta"]}
        snapshot = graph.get_state(config)
        state = dict(snapshot.values or {})
        pending = _interrupt_values(snapshot)
        logger.info(
            "event=agent_graph.paused_or_completed request_id=%s thread_id=%s stage=%s pending=%s pending_count=%s",
            request_id or "-",
            thread_id,
            state.get("stage", "unknown"),
            pending[0].get("kind") if pending else "none",
            len(pending),
        )
        yield {"kind": "finished", "state": state, "interrupts": pending}


def get_agent_graph_state(thread_id: str) -> dict:
    with open_checkpointer() as checkpointer:
        graph = build_agent_graph(checkpointer)
        snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        return {"state": dict(snapshot.values or {}), "interrupts": _interrupt_values(snapshot)}


