"""智能体图（LangGraph 节点）用到的会话级业务操作。

这些操作都很薄，但把它们放在这里，是为了让 `graph/nodes.py` 不再直接
`with get_session_factory()() as db:` —— 也就是不再自己开库、自己提交。

职责边界：
- 打开会话、控制事务边界
- 业务判断（归属校验、找不到就报错）
- 返回图节点真正需要的纯数据

不负责：图的状态流转、提示词拼接、外部模型调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.repositories import job_repo

logger = logging.getLogger(__name__)

__all__ = ["SubtitleSnapshot", "get_subtitle_snapshot", "reject_assets"]


@dataclass(frozen=True)
class SubtitleSnapshot:
    """SRT 文件的只读快照。

    返回快照而不是 ORM 对象，是为了让会话关闭后调用方仍能安全读字段。
    """

    id: str
    workspace_id: str
    file_name: str
    object_path: str
    audio_file_name: str | None
    audio_object_path: str | None
    cues: list


def reject_assets(asset_ids: list[str]) -> int:
    """把仍在库里、且 id 命中的素材标为已驳回，返回实际改动条数。"""
    if not asset_ids:
        return 0
    from app.core.container import get_session_factory

    with get_session_factory()() as db:
        try:
            # 过滤掉已经不存在的 id，避免整批因一条脏数据而失败
            existing = job_repo.list_assets(db, list(asset_ids))
            job_repo.mark_assets_rejected(db, existing)
            db.commit()
            return len(existing)
        except Exception:
            db.rollback()
            raise


def get_subtitle_snapshot(subtitle_file_id: str, workspace_id: str) -> SubtitleSnapshot:
    """取当前工作区的 SRT 文件记录。

    找不到或跨工作区时抛 ValueError，由节点转成用户可读提示。
    """
    from app.core.container import get_session_factory
    from app.domain.models import SubtitleFile

    with get_session_factory()() as db:
        subtitle = db.get(SubtitleFile, subtitle_file_id)
        if not subtitle or subtitle.workspace_id != workspace_id:
            raise ValueError("找不到当前工作区的 SRT 文件，请重新上传")
        # 在会话内把字段读成普通 Python 值，避免会话关闭后再触达 ORM
        return SubtitleSnapshot(
            id=subtitle.id,
            workspace_id=subtitle.workspace_id,
            file_name=subtitle.file_name,
            object_path=subtitle.object_path,
            audio_file_name=subtitle.audio_file_name,
            audio_object_path=subtitle.audio_object_path,
            cues=list(subtitle.cues or []),
        )
