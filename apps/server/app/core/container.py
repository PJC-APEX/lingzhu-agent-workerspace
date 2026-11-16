"""应用级依赖容器。

目的：让基础设施层、图执行层不再反向 `from app.main import app` 读取运行状态。
由组合根（`app.main`）在启动时装配，测试在 `conftest` 中装配测试用实现。

使用方式：

    from app.core.container import get_container, get_session_factory

    factory = get_session_factory()      # 业务/worker 代码取会话工厂
    with factory() as db: ...

禁止：
- 任何模块 import `app.main`
- 任何模块在 import 期读取容器（容器在 lifespan 装配后才可用）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "Container",
    "configure_container",
    "get_container",
    "get_session_factory",
    "is_configured",
    "reset_container",
]


@dataclass
class Container:
    """运行期依赖。字段为 None 表示尚未装配。"""

    session_factory: Callable[..., Any] | None = None
    checkpointer: Any | None = None
    testing: bool = False


_container = Container()


def configure_container(
    *,
    session_factory: Callable[..., Any] | None = None,
    checkpointer: Any | None = None,
    testing: bool | None = None,
) -> Container:
    """装配容器。只传需要覆盖的字段，其余保持不变。"""
    global _container
    if session_factory is not None:
        _container.session_factory = session_factory
    if checkpointer is not None:
        _container.checkpointer = checkpointer
    if testing is not None:
        _container.testing = testing
    return _container


def reset_container() -> None:
    """清空容器，供测试隔离使用。"""
    global _container
    _container = Container()


def get_container() -> Container:
    return _container


def is_configured() -> bool:
    return _container.session_factory is not None


def get_session_factory() -> Callable[..., Any]:
    """返回会话工厂。

    尚未装配时退化到数据库模块的默认工厂，保证脚本、worker 独立入口
    在没有走 FastAPI lifespan 时也能工作。
    """
    if _container.session_factory is not None:
        return _container.session_factory
    from app.infrastructure.database import SessionLocal

    return SessionLocal
