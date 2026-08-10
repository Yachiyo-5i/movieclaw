"""媒体库实时监控（L4）：watchdog 文件事件 → 去抖批处理 → 增量扫描。

设计（吸收 moviebot 的三个实现细节，见设计文档第 1 节核实备注）：
- **事件只进队列**：watchdog 的回调跑在它自己的观察者线程里，绝不做任何
  IO/识别，只把"哪个库有动静"投进 asyncio 队列（线程安全经
  call_soon_threadsafe 桥接）；
- **去抖批处理**：下载器/整理器落盘会在短时间产生大量事件，消费者收到
  首个事件后等待安静窗口（3s，最长 30s 兜底）再触发一次增量扫描——
  scan_library 本身对已知路径秒过，扫描即是最好的"批处理"；
- **写入完成检测**：不追踪单文件的写入进度（moviebot 在事件线程里
  sleep 轮询是反面教训）——去抖窗口天然给了写入落定的时间，且增量扫描
  遇到仍在写入的文件下轮对账会再补。

生命周期：应用启动时 start（库根路径可能不存在则跳过并告警），库增删改
后调用 ``refresh_watches`` 重建监听；关闭时 stop。watchdog 缺失或平台
不支持时优雅降级——只靠 6 小时对账任务兜底，功能不缺失只是不实时。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("movieclaw_api.library_watch")

# 去抖参数：首个事件后等安静 3 秒；持续有事件时最长 30 秒必触发一次
_QUIET_SECONDS = 3.0
_MAX_WAIT_SECONDS = 30.0


def _is_relevant_event(event) -> bool:  # noqa: ANN001
    """该文件事件是否值得触发扫描（观察者线程调用，只做纯判定）。

    两层过滤，都是防「系统自己产生的事件又触发扫描」的自激：

    1. 只读事件（打开 / 未写关闭）忽略：Linux inotify 连"读文件"都会
       上报，而扫描的补探阶段正是用 ffprobe **读**库里的文件。不滤掉就是
       「扫描 → 读文件事件 → 再扫描」的循环——探测失败的行每轮保持
       NULL 重试，循环永不收敛（实测线上每 ~10 秒一轮无限扫描，
       扫描进行中的状态在前端时隐时现）；
    2. 视频/strm/**字幕**之外的文件变动忽略：刮削/整库刷新写进库目录的
       NFO 和图片、下载器的 .!qB 半成品，都不是扫描的盘点对象，触发扫描
       只会让「正在扫描」在刷新/下载期间反复闪现。下载完成时改名缀上
       视频扩展名，那一刻自然收到 moved/created 事件。字幕文件自
       外挂字幕台账（jellyfin-subtitle.md §2.1）起是盘点对象——用户把
       .srt 丢进目录要即时可见；秒过时的重发现只做内存匹配 + 对少数字幕
       文件 stat，且服务端只 stat 不 open，不构成读事件自激。

    目录事件只留移动/删除——整个条目目录被挪走/删掉时收不到目录内
    逐文件的事件；目录的新建/修改则必有后续的文件事件跟进，不必抢跑。
    """
    from watchdog.events import EVENT_TYPE_CLOSED_NO_WRITE, EVENT_TYPE_OPENED

    from movieclaw_api.services.library.layout import SCAN_VIDEO_EXTS, SUBTITLE_EXTS

    if event.event_type in (EVENT_TYPE_OPENED, EVENT_TYPE_CLOSED_NO_WRITE):
        return False
    if event.is_directory:
        return event.event_type in ("moved", "deleted")
    # moved 事件的语义看终点：改名成视频（下载完成）要触发，视频被改走
    # （旧路径消失）同样要触发——起点终点任一是视频扩展名即算数。
    # strm 与视频同权：网盘工具重新生成 strm 树时台账要跟着对齐
    watched = SCAN_VIDEO_EXTS | SUBTITLE_EXTS
    paths = (getattr(event, "dest_path", "") or "", event.src_path or "")
    return any(Path(os.fsdecode(p)).suffix.lower() in watched for p in paths if p)


def _modified_video_path(event) -> str | None:  # noqa: ANN001
    """视频文件的 modified 事件返回其路径（内容变化重探的点名依据，
    jellyfin-subtitle.md §2.4）；其余事件返回 None。

    只认 modified：created/moved 走正常入账（本就会探测），deleted 走
    丢失标记。strm 不点名——它无媒体流，重探无意义。
    """
    from movieclaw_api.services.library.layout import VIDEO_EXTS

    if event.is_directory or event.event_type != "modified":
        return None
    path = os.fsdecode(event.src_path or "")
    if path and Path(path).suffix.lower() in VIDEO_EXTS:
        return path
    return None


class LibraryWatcher:
    """库根路径的文件事件监听器（进程级单例，见 init_library_watcher）。"""

    def __init__(self) -> None:
        self._observer = None
        # 队列元素 (library_id, modified_video_path|None)：后者是"该视频
        # 内容被修改过"的点名（去抖后汇总传给扫描做变化重探）
        self._queue: asyncio.Queue[tuple[int, str | None]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer: asyncio.Task | None = None
        self._available = True
        # 串行化重建：连续两次库编辑各自触发 refresh，并发重建会互踩 _observer
        self._refresh_lock = asyncio.Lock()

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._consumer = asyncio.create_task(self._consume())
        await self.refresh_watches()

    async def stop(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            self._consumer = None
        # 与后台重建互斥：重建正在工作线程里装配新观察者时直接停旧引用，
        # 会漏掉刚装好的那个（关停竞态）；拿到锁再停则必停到最终的观察者
        async with self._refresh_lock:
            self._stop_observer()

    def _stop_observer(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    async def refresh_watches(self) -> None:
        """按当前库配置重建监听（库增删改根路径后调用）。

        重建的磁盘部分（停旧观察者 join、recursive 监听逐目录建 watch、
        网络挂载上的 is_dir）都是阻塞调用，放线程池执行——inotify 对大库
        的递归建 watch 可达数秒到数十秒，在事件循环里跑会冻住所有请求。
        """
        if not self._available:
            return
        try:
            import watchdog  # noqa: F401 -- 仅探测可用性，实际使用在 _rebuild_observer
        except ImportError:
            self._available = False
            logger.warning("未安装 watchdog，媒体库实时监控不可用——仅靠定期对账发现新文件")
            return

        from sqlmodel import select

        from movieclaw_db.engine import get_database
        from movieclaw_db.models import Library

        db = get_database()
        async with db.session() as session:
            libraries = list((await session.execute(select(Library))).scalars().all())
        roots = [
            (library.id, root)
            for library in libraries
            if library.id is not None
            for root in library.root_paths
        ]
        async with self._refresh_lock:
            await asyncio.to_thread(self._rebuild_observer, roots)

    def _rebuild_observer(self, roots: list[tuple[int, str]]) -> None:
        """（工作线程）停掉旧观察者并按根路径清单重建。"""
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            """事件回调（观察者线程）：只投递库 id，不做任何业务。"""

            def __init__(self, library_id: int) -> None:
                self._library_id = library_id

            def on_any_event(self, event) -> None:  # noqa: ANN001
                if _is_relevant_event(event):
                    watcher._enqueue_threadsafe(
                        self._library_id, _modified_video_path(event)
                    )

        self._stop_observer()
        observer = Observer()
        watched = 0
        for library_id, root in roots:
            path = Path(root)
            if not path.is_dir():
                continue  # 根路径未就绪：不告警刷屏，对账任务会持续兜底
            try:
                observer.schedule(_Handler(library_id), str(path), recursive=True)
                watched += 1
            except OSError as exc:
                logger.warning("监听根路径失败（%s）：%s", root, exc)
        if watched:
            observer.daemon = True
            observer.start()
            self._observer = observer
            logger.info("媒体库实时监控已启动：监听 %d 个根路径", watched)
        else:
            logger.info("没有可监听的库根路径，实时监控待命（对账任务兜底）")

    # -- 事件通道 ----------------------------------------------------------

    def _enqueue_threadsafe(self, library_id: int, modified_path: str | None) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, (library_id, modified_path))

    async def _consume(self) -> None:
        """去抖消费：首事件后等安静窗口，汇总本批涉及的库做增量扫描。"""
        from movieclaw_api.services.library.scan import is_scanning, scan_library

        while True:
            library_id, modified_path = await self._queue.get()
            # 库 id → 点名重探的视频路径集（modified 事件携带）
            pending: dict[int, set[str]] = {library_id: set()}
            if modified_path is not None:
                pending[library_id].add(modified_path)
            deadline = asyncio.get_running_loop().time() + _MAX_WAIT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                timeout = min(_QUIET_SECONDS, max(remaining, 0))
                try:
                    library_id, modified_path = await asyncio.wait_for(
                        self._queue.get(), timeout
                    )
                except TimeoutError:
                    break  # 安静窗口达成
                paths = pending.setdefault(library_id, set())
                if modified_path is not None:
                    paths.add(modified_path)
                if asyncio.get_running_loop().time() >= deadline:
                    break  # 兜底：持续有事件也要触发
            for library_id in sorted(pending):
                if is_scanning(library_id):
                    continue  # 扫描中产生的事件（自己写台账不产生文件事件，
                    # 但入库硬链会）——正在扫就不叠加
                logger.info("检测到媒体库 #%s 根路径变更，触发增量扫描", library_id)
                try:
                    # 文件事件只需同步台账；历史规格补探由用户手动扫描触发，
                    # 不让一次播放/下载相关的目录事件变成整库 ffprobe。
                    # 本批 modified 点名的视频例外：事件本身就是变更信号，
                    # 扫描会对它们 stat 确认后重探（jellyfin-subtitle.md §2.4）
                    await scan_library(
                        library_id,
                        backfill_existing_specs=False,
                        reprobe_paths=pending[library_id] or None,
                    )
                except Exception:  # noqa: BLE001 -- 监控消费绝不崩
                    logger.exception("实时监控触发的扫描失败：库 #%s", library_id)


_watcher: LibraryWatcher | None = None


def get_library_watcher() -> LibraryWatcher | None:
    return _watcher


async def init_library_watcher() -> None:
    """启动进程级监听单例（lifespan 调用）。"""
    global _watcher
    _watcher = LibraryWatcher()
    await _watcher.start()


async def close_library_watcher() -> None:
    global _watcher
    if _watcher is not None:
        await _watcher.stop()
        _watcher = None
