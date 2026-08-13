"""Transmission 适配器。

基于 transmission-rpc 包（同步 requests 实现）访问 Transmission 的 RPC 接口，
所有阻塞调用通过 asyncio.to_thread 放入线程池执行。

与 qBittorrent 的能力差异：Transmission 没有"分类"概念，DownloadRequest
的 category 映射为第一个 label，tags 依次追加其后（labels 需要
Transmission 4.0+，旧版守护进程会忽略并告警）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from transmission_rpc import Client
from transmission_rpc.error import (
    TransmissionAuthError,
    TransmissionConnectError,
    TransmissionError,
)

from movieclaw_downloader.base import BaseDownloader
from movieclaw_downloader.exceptions import (
    DownloaderAuthError,
    DownloaderConnectError,
    DownloaderDeleteError,
    DownloaderSubmitError,
)
from movieclaw_downloader.models import (
    DownloaderInfo,
    DownloaderType,
    DownloadRequest,
    SubmitResult,
    TorrentBrief,
    TorrentFile,
    TorrentStatus,
)

logger = logging.getLogger("movieclaw_downloader.transmission")


def _normalize_state(torrent, completed: bool) -> str:  # noqa: ANN001 -- transmission_rpc.Torrent
    """把 Transmission 的任务状态收敛到 TorrentStatus.state 的统一词表。

    原始 status 词表（transmission-rpc）：stopped / check pending / checking /
    download pending / downloading / seed pending / seeding。
    """
    if completed:
        return "completed"
    if int(torrent.fields.get("error", 0)) != 0:
        return "error"
    status = str(torrent.fields.get("status", ""))
    # fields 里 status 是数字枚举：0=stopped 3=download pending 4=downloading
    if status == "4":
        # 有速度才算真在下，零速对齐 qBittorrent 的 stalled 语义
        return "downloading" if int(torrent.fields.get("rateDownload", 0)) > 0 else "stalled"
    if status == "3":
        return "stalled"
    if status == "0":
        return "paused"
    return "unknown"


@contextmanager
def _translate_errors(url: str, *, operation: str = "submit") -> Iterator[None]:
    """把 transmission-rpc 的异常翻译成本模块的统一异常。"""
    try:
        yield
    except TransmissionAuthError as exc:
        raise DownloaderAuthError(
            "Transmission 认证失败：用户名或密码错误", details={"url": url}
        ) from exc
    except TransmissionConnectError as exc:
        raise DownloaderConnectError(
            "无法连接到 Transmission，请检查 RPC 地址和端口",
            details={"url": url, "error": str(exc)},
        ) from exc
    except TransmissionError as exc:
        if operation == "delete":
            raise DownloaderDeleteError(
                "删除 Transmission 任务失败，请检查下载器状态",
                details={"url": url, "error": str(exc)},
            ) from exc
        raise DownloaderSubmitError(
            "Transmission 拒绝了该请求（种子无效或下载器返回错误）",
            details={"url": url, "error": str(exc)},
        ) from exc


class TransmissionDownloader(BaseDownloader):
    """Transmission 下载器适配器。"""

    _tr: Client | None = None

    def _client(self) -> Client:
        """惰性创建底层客户端。

        注意 transmission_rpc.Client 构造时就会发起 RPC 请求获取会话，
        因此本方法只能在线程池内调用，不能出现在事件循环里。
        """
        if self._tr is None:
            parts = urlsplit(self.config.url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise DownloaderConnectError(
                    "Transmission 地址格式错误，应形如 http://主机:9091",
                    details={"url": self.config.url},
                )
            with _translate_errors(self.config.url):
                self._tr = Client(
                    protocol=parts.scheme,
                    host=parts.hostname,
                    port=parts.port or 9091,
                    # 未写路径时补全官方默认 RPC 路径
                    path=parts.path if parts.path not in ("", "/") else "/transmission/rpc",
                    username=self.config.username,
                    password=self.config.password,
                    timeout=self.config.timeout,
                )
        return self._tr

    async def submit(self, request: DownloadRequest) -> SubmitResult:
        return await asyncio.to_thread(self._submit_sync, request)

    def _submit_sync(self, request: DownloadRequest) -> SubmitResult:
        client = self._client()
        info_hash = self._resolve_info_hash(request)

        # category + tags 统一压平成 Transmission 的 labels
        labels = [label for label in [request.category, *request.tags] if label]

        with _translate_errors(self.config.url):
            # 提交前按 infohash 去重：已存在直接幂等返回，不重复添加
            if info_hash:
                try:
                    existing = client.get_torrent(info_hash)
                except KeyError:
                    existing = None
                if existing is not None:
                    logger.info("种子已存在于 Transmission，跳过提交: %s", info_hash)
                    return SubmitResult(
                        info_hash=info_hash,
                        name=existing.name,
                        already_exists=True,
                    )

            torrent = client.add_torrent(
                request.torrent_bytes if request.torrent_bytes is not None else request.magnet,
                download_dir=request.save_path,
                paused=request.paused,
                labels=labels or None,
            )

        # 磁力链接解析不出 hash 时，退而使用 Transmission 返回的值
        result_hash = info_hash or torrent.hash_string
        logger.info("已提交种子到 Transmission: hash=%s name=%s", result_hash, torrent.name)
        return SubmitResult(info_hash=result_hash, name=torrent.name)

    async def get_torrent(
        self, info_hash: str, *, include_files: bool = True
    ) -> TorrentStatus | None:
        return await asyncio.to_thread(self._get_torrent_sync, info_hash, include_files)

    def _get_torrent_sync(self, info_hash: str, include_files: bool = True) -> TorrentStatus | None:
        client = self._client()
        with _translate_errors(self.config.url):
            try:
                torrent = client.get_torrent(info_hash)
            except KeyError:
                return None
        completed = float(torrent.percent_done) >= 1.0
        # 直接读原始字段：transmission-rpc 的 .eta 属性在无法估算时抛异常，
        # fields 里的原始值 -1（未知）/-2（不适用）直接判掉更稳
        eta = int(torrent.fields.get("eta", -1))
        return TorrentStatus(
            info_hash=info_hash,
            name=torrent.name,
            progress=float(torrent.percent_done),
            completed=completed,
            save_path=torrent.download_dir,
            files=(
                [
                    # file.name 是种子内相对路径（含顶层目录）
                    TorrentFile(path=file.name, size_bytes=int(file.size))
                    for file in torrent.get_files()
                ]
                # 进度快照类调用不需要文件清单，跳过逐文件的构造
                if include_files
                else []
            ),
            size_bytes=int(torrent.fields.get("sizeWhenDone", 0)) or None,
            dlspeed_bytes=int(torrent.fields.get("rateDownload", 0)),
            eta_seconds=eta if eta > 0 else None,
            state=_normalize_state(torrent, completed=completed),
        )

    async def list_torrents(self) -> list[TorrentBrief]:
        return await asyncio.to_thread(self._list_torrents_sync)

    def _list_torrents_sync(self) -> list[TorrentBrief]:
        client = self._client()
        with _translate_errors(self.config.url):
            torrents = client.get_torrents()
        # Transmission 的任务名即落盘根目录/文件名，无独立的 content_path
        briefs: list[TorrentBrief] = []
        for torrent in torrents:
            progress = float(torrent.percent_done)
            completed = progress >= 1.0
            eta = int(torrent.fields.get("eta", -1))
            briefs.append(
                TorrentBrief(
                    name=torrent.name,
                    content_name=torrent.name,
                    completed=completed,
                    info_hash=str(torrent.hash_string).lower(),
                    progress=progress,
                    size_bytes=int(torrent.fields.get("sizeWhenDone", 0)) or None,
                    dlspeed_bytes=int(torrent.fields.get("rateDownload", 0)),
                    eta_seconds=eta if eta > 0 else None,
                    state=_normalize_state(torrent, completed=completed),
                )
            )
        return briefs

    async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
        await asyncio.to_thread(self._delete_torrent_sync, info_hash, delete_files)

    def _delete_torrent_sync(self, info_hash: str, delete_files: bool) -> None:
        """按用户选择删除任务或连同数据文件。"""
        client = self._client()
        with _translate_errors(self.config.url, operation="delete"):
            client.remove_torrent(info_hash.lower(), delete_data=delete_files)
        logger.info(
            "已从 Transmission 删除任务%s: hash=%s",
            "并删除数据文件" if delete_files else "并保留数据文件",
            info_hash,
        )

    async def test_connection(self) -> DownloaderInfo:
        return await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> DownloaderInfo:
        client = self._client()
        with _translate_errors(self.config.url):
            session = client.get_session()
        return DownloaderInfo(type=DownloaderType.TRANSMISSION, version=session.version)

    async def close(self) -> None:
        # transmission-rpc 无显式登出/断开接口，丢弃引用即可
        self._tr = None
