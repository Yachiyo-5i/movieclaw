"""与 HTTP 响应解耦的进程内后台任务主管。

Starlette ``BackgroundTasks`` 属于响应生命周期：Uvicorn 优雅关闭时会等待
它们全部完成。分钟级字幕翻译若挂在这里，代码热重载就会先停止接收 API，
再等待整片翻译结束，前端因此长时间显示“正在连接服务”。

本主管持有 task 强引用，避免无人引用的任务被回收；应用关闭时主动取消并等待
协程清理。字幕任务另以小型意图文件记录“重启后仍应继续”，启动时重新入队；
需要跨主机、跨副本可靠投递的业务仍应使用持久队列，不能把本类当作消息队列。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("movieclaw_api.task_supervisor")


class TaskSupervisor:
    """管理不应绑定 HTTP 响应生命周期的进程内异步任务。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    def start(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        """登记并立即调度协程；调用方无需等待任务完成。"""
        if self._closing:
            # 调用方已经构造了协程对象；拒绝调度时必须显式关闭，否则解释器
            # 会报告“coroutine was never awaited”，掩盖真正的停机原因。
            coroutine.close()
            raise RuntimeError("后台任务主管正在关闭，无法创建新任务")
        try:
            task = asyncio.create_task(coroutine, name=name)
        except Exception:
            coroutine.close()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        logger.info("后台任务已创建：%s", name)
        return task

    async def close(self) -> None:
        """应用关闭时取消并回收全部任务，不等待业务自然跑完。"""
        self._closing = True
        running = [task for task in self._tasks if not task.done()]
        if running:
            logger.info("应用关闭，正在暂停 %d 个后台任务", len(running))
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._tasks.clear()
        logger.info("后台任务主管已关闭")

    @property
    def active_count(self) -> int:
        """当前尚未结束的任务数，供状态检查与测试使用。"""
        return sum(not task.done() for task in self._tasks)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            logger.info("后台任务已暂停：%s", task.get_name())
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "后台任务发生未处理异常：%s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )


_supervisor: TaskSupervisor | None = None


def init_task_supervisor() -> TaskSupervisor:
    """为当前 FastAPI 生命周期初始化唯一的任务主管。"""
    global _supervisor
    _supervisor = TaskSupervisor()
    return _supervisor


def get_task_supervisor() -> TaskSupervisor:
    """取得当前任务主管；仅供应用生命周期启动后的业务调用。"""
    if _supervisor is None:
        raise RuntimeError("后台任务主管尚未初始化")
    return _supervisor


async def close_task_supervisor() -> None:
    """关闭并清空当前生命周期的任务主管。"""
    global _supervisor
    if _supervisor is not None:
        await _supervisor.close()
        _supervisor = None
