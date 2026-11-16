"""按 token 预算组装普通对话历史；文案正文作为独立工作文档处理。"""

from __future__ import annotations

import math

from app.core.config import AGENT_INPUT_TOKEN_BUDGET


def estimate_tokens(text: str) -> int:
    """跨供应商的保守估算；后续可用供应商 usage 数据校准。"""
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return max(1, math.ceil(cjk * 1.5 + other / 3.5))


def select_recent_messages(messages: list[dict], summary: str, fixed_prompt: str, reserved_text: str = "") -> tuple[list[dict], int, int]:
    """从最新消息向前装入完整消息，文案和系统提示词占用先从预算中扣除。"""
    budget = AGENT_INPUT_TOKEN_BUDGET
    summary_tokens = estimate_tokens(summary) if summary else 0
    fixed_tokens = estimate_tokens(fixed_prompt + reserved_text)
    remaining = max(0, budget - fixed_tokens - summary_tokens)
    selected: list[dict] = []
    used_tokens = fixed_tokens + summary_tokens
    for message in reversed(messages):
        cost = estimate_tokens(message["content"]) + 6
        if cost > remaining:
            break
        selected.append(message)
        remaining -= cost
        used_tokens += cost
    selected.reverse()
    return selected, used_tokens, budget

