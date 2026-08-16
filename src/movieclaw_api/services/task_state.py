"""进程内后台任务状态三件套的通用容器：单飞 / 实时状态 / 停止请求。

库扫描、整库元数据刷新、文件整理这类"每库至多一个在跑"的后台任务，都需要
同一组进程内状态：进行中集合（单飞互斥）、实时状态对象（进度面板数据源）、
停止请求标志（协作式取消）、最近一次结论（"上次做了什么"反馈）。此前三个
模块各自维护三四个模块级容器，本类把它们收拢为一个对象。

单进程假设：与调用方一致，无锁——所有读写都发生在同一个事件循环里。
"""

from __future__ import annotations

from typing import Generic, TypeVar

S = TypeVar("S")


class TaskState(Generic[S]):
    """按整数键（一般是库 id）管理一类后台任务的进程内状态。"""

    def __init__(self) -> None:
        self._states: dict[int, S] = {}  # 进行中即有键；值是任务自定义的状态对象
        self._stops: set[int] = set()  # 已请求停止的键（协作式，任务自查）
        self._lasts: dict[int, object] = {}  # 最近一次完成的结论

    def try_start(self, key: int, state: S) -> bool:
        """单飞闸门：已在跑返回 False，否则登记状态。

        不清停止标志——标志的生命周期由 ``finish`` 统一收口（开始后立刻
        请求停止的窗口里，标志必须能被任务的第一个检查点看到）。
        """
        if key in self._states:
            return False
        self._states[key] = state
        return True

    def running(self, key: int) -> bool:
        return key in self._states

    def state_of(self, key: int) -> S | None:
        """进行中任务的实时状态；没在跑返回 None。"""
        return self._states.get(key)

    def update(self, key: int, state: S) -> None:
        """整体替换进行中任务的状态（不可变状态如进度元组用；没在跑则忽略）。"""
        if key in self._states:
            self._states[key] = state

    def request_stop(self, key: int) -> bool:
        """请求停止；没在跑返回 False。任务在安全点自查 stop_requested。"""
        if key not in self._states:
            return False
        self._stops.add(key)
        return True

    def stop_requested(self, key: int) -> bool:
        return key in self._stops

    def finish(self, key: int, result: object | None = None) -> None:
        """任务收尾：清状态与停止标志；给了 result 则记为最近一次结论。"""
        self._states.pop(key, None)
        self._stops.discard(key)
        if result is not None:
            self._lasts[key] = result

    def last(self, key: int) -> object | None:
        """最近一次完成的结论；从未完成过返回 None。"""
        return self._lasts.get(key)

    def discard(self, key: int) -> None:
        """丢弃一个已永久删除对象的残留状态与历史结论。"""
        self._states.pop(key, None)
        self._stops.discard(key)
        self._lasts.pop(key, None)
