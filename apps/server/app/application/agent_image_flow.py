"""生图任务与智能体图流程之间的衔接。

生图是个异步任务：图流程在这里中断，等 worker 把图跑完，再从这里续上。
这段衔接属于业务编排——它决定「图跑完之后，对话该怎么往下走」，
所以放在 application 层，而不是 worker 里。

关键点：恢复之前必须先拿会话租约。同一会话可能正被另一个请求推动
（用户正在输入、或者双击了按钮），没有租约就恢复会出现两个推进者，
检查点状态会被覆盖成错的。拿不到就放弃这次恢复，等恢复扫描器下次再试。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["resume_agent_image_flow"]

# 这两种中断都表示「图流程正在等一张图」
_PENDING_INTERRUPT_KINDS = {"image_job", "image_preview_job"}


def resume_agent_image_flow(thread_id: str, job_id: str, assets: list[dict], error: str = "") -> None:
    """图（或预览图）任务结束后，把中断中的智能体流程续上。

    如果中断点已经不存在，说明图流程在 worker 断电前就已经推进过了，
    这时只需要把当前状态落库，不要重复恢复——重复恢复会让同一张图被消费两次。
    """
    from app.graph.graph import get_agent_graph_state, stream_agent_graph
    from app.application.agent_result_service import persist_agent_graph_result
    from app.application.thread_execution import acquire_thread_lease

    lease = acquire_thread_lease(thread_id)
    try:
        snapshot = get_agent_graph_state(thread_id)
        pending_job = next(
            (
                item
                for item in snapshot.get("interrupts", [])
                if item.get("kind") in _PENDING_INTERRUPT_KINDS and item.get("jobId") == job_id
            ),
            None,
        )
        if not pending_job:
            logger.info(
                "event=agent_graph.resume_skipped thread_id=%s job_id=%s reason=interrupt_gone",
                thread_id,
                job_id,
            )
            persist_agent_graph_result(thread_id, snapshot)
            return

        final = None
        for event in stream_agent_graph(
            thread_id, resume={"jobId": job_id, "assets": assets, "error": error}
        ):
            if event.get("kind") == "finished":
                final = event
        if final:
            persist_agent_graph_result(thread_id, final)
    finally:
        lease.release()
