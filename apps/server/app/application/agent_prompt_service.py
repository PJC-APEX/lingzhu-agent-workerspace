"""智能体提示词版本的解析。

职责边界：
- 把一个版本号解析成可用的提示词（内容 + id + 版本号），并校验归属与类型
- 调用方传入会话，事务边界由调用方掌握

不负责：拼提示词、调模型。

关于失败处理（重要）：
- 只有「版本找不到 / 类型不匹配 / 不属于当前用户」这类 **ValueError** 才降级为默认提示词。
  这几种情况下继续跑是对的：用户选的那版没了，总不能让他连话都说不出。
- 数据库故障、连接中断等异常 **必须继续往上抛**。原实现就是这样，
  上层（任务重试机制）靠这个把可恢复故障变成「稍后重试」，而不是静默产出一版错内容。
- 降级时 version_id 会一并改回默认版本号。否则前端/日志显示「用的是 v7」，
  实际跑的是默认提示词，记录在说谎，事后无法排查。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.prompts.agent_prompt.rewrite import DEFAULT_REWRITE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.storyboard import DEFAULT_STORYBOARD_PROMPT_VERSION_ID
from app.application import prompt_service

logger = logging.getLogger(__name__)

__all__ = [
    "AgentPrompt",
    "default_rewrite_prompt",
    "resolve_prompt",
    "resolve_rewrite_prompt",
    "resolve_storyboard_prompt",
]

# 解析不到时使用的稳定占位 id，保证下游拼提示词逻辑不受影响
_FALLBACK_PROMPT_IDS = {
    "rewrite": "agent_rewrite_default_v1",
    "storyboard": "agent_storyboard_default_v1",
}

_DEFAULT_VERSION_IDS = {
    "rewrite": DEFAULT_REWRITE_PROMPT_VERSION_ID,
    "storyboard": DEFAULT_STORYBOARD_PROMPT_VERSION_ID,
}


class AgentPrompt:
    """提示词解析结果的只读视图。

    字段与原先 `app.services.agent_prompt_library` 返回对象一致，
    只是不再把 ORM 对象泄漏到上层。
    """

    __slots__ = ("prompt_id", "version_id", "version", "content")

    def __init__(self, prompt_id: str, version_id: str, version: int, content: str = "") -> None:
        self.prompt_id = prompt_id
        self.version_id = version_id
        self.version = version
        self.content = content


def _fallback(kind: str, version_id: str | None = None) -> AgentPrompt:
    """默认版本或降级时的返回值：内容留空，由下游拼默认提示词。

    version_id 一律取该类型的默认版本号——降级之后「实际用的版本」
    就和这里声明的保持一致。
    """
    return AgentPrompt(
        prompt_id=_FALLBACK_PROMPT_IDS.get(kind, f"agent_{kind}_default_v1"),
        version_id=_DEFAULT_VERSION_IDS.get(kind, version_id or ""),
        version=1,
        content="",
    )


def _resolve(db: Session, version_id: str, kind: str, user_id: str | None) -> AgentPrompt:
    try:
        resolved = prompt_service.resolve_agent_prompt_version(db, version_id, kind, user_id)
    except ValueError:
        # 只兜「找不到 / 类型不符 / 归属不符」：这类是业务上的不可用，降级继续
        logger.warning(
            "event=agent_prompt.resolve_degraded kind=%s requested_version_id=%s",
            kind,
            version_id,
            exc_info=True,
        )
        return _fallback(kind, version_id)
    # 其余异常（数据库故障、连接中断等）不捕获，原样上抛交给上层重试
    return AgentPrompt(
        prompt_id=resolved.prompt_id,
        version_id=resolved.version_id,
        version=resolved.version,
        content=resolved.content,
    )


def resolve_prompt(db: Session, version_id: str, kind: str, user_id: str | None) -> AgentPrompt:
    """按类型解析提示词版本。

    传入默认版本号时不需要打库，直接返回内容为空的兜底，
    由下游拼接内置提示词。
    """
    default_version_id = _DEFAULT_VERSION_IDS.get(kind, DEFAULT_REWRITE_PROMPT_VERSION_ID)
    if version_id == default_version_id:
        return _fallback(kind)
    return _resolve(db, version_id, kind, user_id)


def resolve_rewrite_prompt(db: Session, version_id: str, user_id: str | None) -> AgentPrompt:
    if version_id == DEFAULT_REWRITE_PROMPT_VERSION_ID:
        return _fallback("rewrite")
    return _resolve(db, version_id, "rewrite", user_id)


def resolve_storyboard_prompt(db: Session, version_id: str, user_id: str | None) -> AgentPrompt:
    if version_id == DEFAULT_STORYBOARD_PROMPT_VERSION_ID:
        return _fallback("storyboard")
    return _resolve(db, version_id, "storyboard", user_id)


def default_rewrite_prompt() -> AgentPrompt:
    return _fallback("rewrite")


