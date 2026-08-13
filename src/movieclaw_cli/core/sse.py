"""SSE（Server-Sent Events）客户端（docs/design/cli.md §8.3）。

一次实现、两处复用：种子搜索流（/search/torrents/stream）与 Agent 运行流
（/agent/runs/{id}/stream）。与前端刻意不用 EventSource 的理由相同：
搜索流断线不应自动重放整次搜索，Agent 流要用 Last-Event-ID 精确续传。

帧协议：`event:` / `data:` / `id:` 行组成一帧，空行分帧；`:` 开头是
心跳注释，跳过不产出事件。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass
class SseEvent:
    """一帧 SSE 事件。data 为原始字符串（多行 data 按规范以换行拼接）。"""

    event: str
    data: str
    id: int | None = None


def parse_sse_lines(lines: Iterable[str]) -> Iterator[SseEvent]:
    """把逐行文本流解析为事件流。容忍字段缺失（event 缺省为 'message'）。"""
    event: str | None = None
    data_parts: list[str] = []
    event_id: int | None = None
    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            if data_parts or event:
                yield SseEvent(event=event or "message", data="\n".join(data_parts), id=event_id)
            event, data_parts, event_id = None, [], None
            continue
        if line.startswith(":"):
            continue  # 心跳注释帧
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data_parts.append(value)
        elif field == "id":
            try:
                event_id = int(value)
            except ValueError:
                event_id = None
    if data_parts or event:
        yield SseEvent(event=event or "message", data="\n".join(data_parts), id=event_id)
