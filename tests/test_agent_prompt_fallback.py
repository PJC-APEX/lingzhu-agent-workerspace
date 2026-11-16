"""提示词解析的失败处理约束。

对应一次评审指出的问题：重构时把「解析失败」一刀切成"降级继续"，
结果是数据库故障也被吞掉，本该重试的任务变成静默产出错内容。
这里把边界钉死：

1. 业务上的不可用（ValueError：找不到 / 类型不符 / 归属不符）→ 降级为默认提示词
2. 降级后 version_id 必须是默认版本号，不能让记录说谎
3. 数据库故障等基础设施异常 → 必须上抛，交给任务重试
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.application import agent_prompt_service
from app.prompts.agent_prompt.rewrite import DEFAULT_REWRITE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.storyboard import DEFAULT_STORYBOARD_PROMPT_VERSION_ID


class _ExplodingSession:
    """模拟一个已经不可用的数据库会话。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def query(self, *args, **kwargs):
        raise self._error


def test_business_value_error_degrades_to_default_prompt(monkeypatch):
    """版本找不到时降级，而不是让整轮对话失败。"""

    def raise_value_error(db, version_id, kind, user_id=None):
        raise ValueError("找不到对应类型的提示词版本")

    monkeypatch.setattr(agent_prompt_service.prompt_service, "resolve_agent_prompt_version", raise_value_error)

    resolved = agent_prompt_service.resolve_prompt(object(), "user-version-7", "rewrite", "user-1")

    assert resolved.content == "", "降级后内容为空，由下游拼默认提示词"
    assert resolved.version_id == DEFAULT_REWRITE_PROMPT_VERSION_ID
    assert resolved.version == 1


def test_degraded_prompt_does_not_keep_requested_version_id(monkeypatch):
    """降级时不能保留用户请求的版本号——否则前端显示的和实际跑的不是一回事。"""
    requested = "user-version-7"

    def raise_value_error(db, version_id, kind, user_id=None):
        raise ValueError("找不到对应类型的提示词版本")

    monkeypatch.setattr(
        agent_prompt_service.prompt_service,
        "resolve_agent_prompt_version",
        raise_value_error,
    )

    resolved = agent_prompt_service.resolve_prompt(object(), requested, "storyboard", "user-1")

    assert resolved.version_id != requested
    assert resolved.version_id == DEFAULT_STORYBOARD_PROMPT_VERSION_ID


def test_database_error_propagates_instead_of_degrading(monkeypatch):
    """数据库故障必须上抛。吞掉它会让本该重试的任务静默产出一版错内容。"""

    def raise_operational_error(db, version_id, kind, user_id=None):
        raise OperationalError("SELECT ...", {}, Exception("connection reset"))

    monkeypatch.setattr(
        agent_prompt_service.prompt_service,
        "resolve_agent_prompt_version",
        raise_operational_error,
    )

    with pytest.raises(OperationalError):
        agent_prompt_service.resolve_prompt(object(), "user-version-7", "rewrite", "user-1")


def test_default_version_short_circuits_without_touching_database():
    """默认版本不应该打库——传一个会爆炸的 session 也能正常返回。"""
    exploding = _ExplodingSession(RuntimeError("不该被调用"))

    resolved = agent_prompt_service.resolve_prompt(exploding, DEFAULT_REWRITE_PROMPT_VERSION_ID, "rewrite", None)

    assert resolved.version_id == DEFAULT_REWRITE_PROMPT_VERSION_ID
    assert resolved.content == ""


def test_successful_resolution_passes_through_fields(monkeypatch):
    """正常解析时字段原样透传，不做任何改写。"""
    from app.services.agent_prompt_library import ResolvedAgentPrompt

    def resolve(db, version_id, kind, user_id=None):
        return ResolvedAgentPrompt(
            prompt_id="preset-1",
            version_id=version_id,
            kind=kind,
            name="我的改写提示词",
            version=3,
            content="请用古风改写。",
            is_default=False,
        )

    monkeypatch.setattr(agent_prompt_service.prompt_service, "resolve_agent_prompt_version", resolve)

    resolved = agent_prompt_service.resolve_prompt(object(), "user-version-7", "rewrite", "user-1")

    assert resolved.prompt_id == "preset-1"
    assert resolved.version_id == "user-version-7"
    assert resolved.version == 3
    assert resolved.content == "请用古风改写。"
