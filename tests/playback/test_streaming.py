from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from movieclaw_playback.streaming import (
    DisconnectAwareFileResponse,
    register_device_stream,
    stop_device_streams,
    unregister_device_stream,
)


def _scope(
    *, method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "headers": headers or [],
        "extensions": {},
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _sender(messages: list[dict[str, Any]]):
    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    return send


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_skips_open_when_client_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 128)
    messages: list[dict[str, Any]] = []

    async def disconnected() -> bool:
        return True

    async def should_not_open(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("断开的客户端不应继续打开媒体文件")

    monkeypatch.setattr("movieclaw_playback.streaming.anyio.open_file", should_not_open)
    response = DisconnectAwareFileResponse(video, is_disconnected=disconnected)
    await response(_scope(), _receive, _sender(messages))

    assert messages[0]["type"] == "http.response.start"
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_disables_pathsend(
    tmp_path: Path,
) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 128)
    messages: list[dict[str, Any]] = []

    async def connected() -> bool:
        return False

    response = DisconnectAwareFileResponse(video, is_disconnected=connected)
    await response(
        {**_scope(), "extensions": {"http.response.pathsend": {}}},
        _receive,
        _sender(messages),
    )

    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert b"".join(message.get("body", b"") for message in messages) == video.read_bytes()
    assert all(message["type"] != "http.response.pathsend" for message in messages)


@pytest.mark.asyncio
async def test_stopped_playback_signal_skips_opening_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """播放器已上报 Stopped 时，即使 TCP 尚未断开也不能继续预读。"""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 128)
    messages: list[dict[str, Any]] = []
    stopped = asyncio.Event()
    stopped.set()

    async def connected() -> bool:
        return False

    async def should_not_open(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Stopped 后不应继续打开媒体文件")

    monkeypatch.setattr("movieclaw_playback.streaming.anyio.open_file", should_not_open)
    response = DisconnectAwareFileResponse(
        video,
        is_disconnected=connected,
        session_stopped=stopped,
    )
    await response(_scope(), _receive, _sender(messages))

    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert messages[-1]["more_body"] is False


def test_stop_device_streams_sets_all_active_streams() -> None:
    """一个设备的停止上报要收口它并发建立的全部 Range 请求。"""
    first = register_device_stream("device-a")
    second = register_device_stream("device-a")
    try:
        assert stop_device_streams("device-a") == 2
        assert first.is_set() and second.is_set()
    finally:
        unregister_device_stream("device-a", first)
        unregister_device_stream("device-a", second)


@pytest.mark.asyncio
async def test_stopped_playback_signal_stops_active_stream_before_next_chunk(
    tmp_path: Path,
) -> None:
    """Stopped 到达正在发送的流后，下一块读取前必须结束响应。"""
    video = tmp_path / "movie.mkv"
    content = b"x" * (DisconnectAwareFileResponse.chunk_size * 2)
    video.write_bytes(content)
    messages: list[dict[str, Any]] = []
    stopped = asyncio.Event()

    async def connected() -> bool:
        return False

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)
        if message.get("body") == content[: DisconnectAwareFileResponse.chunk_size]:
            stopped.set()

    response = DisconnectAwareFileResponse(
        video,
        is_disconnected=connected,
        session_stopped=stopped,
    )
    await response(_scope(), _receive, send)

    body = b"".join(message.get("body", b"") for message in messages)
    assert body == content[: DisconnectAwareFileResponse.chunk_size]
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_stops_before_next_chunk(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    content = b"x" * (DisconnectAwareFileResponse.chunk_size * 2)
    video.write_bytes(content)
    messages: list[dict[str, Any]] = []
    checks = iter((False, False, False, True))

    async def disconnected() -> bool:
        return next(checks)

    response = DisconnectAwareFileResponse(video, is_disconnected=disconnected)
    await response(_scope(), _receive, _sender(messages))

    body = b"".join(message.get("body", b"") for message in messages)
    assert body == content[: DisconnectAwareFileResponse.chunk_size]
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_keeps_range_response(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    content = b"0123456789" * 20_000
    video.write_bytes(content)
    messages: list[dict[str, Any]] = []

    async def connected() -> bool:
        return False

    response = DisconnectAwareFileResponse(video, is_disconnected=connected)
    await response(
        _scope(headers=[(b"range", b"bytes=100-69999")]),
        _receive,
        _sender(messages),
    )

    start = messages[0]
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in messages[1:])
    assert start["status"] == 206
    assert headers["content-range"] == f"bytes 100-69999/{len(content)}"
    assert "content-length" not in headers
    assert body == content[100:70000]


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_keeps_head_response(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 128)
    messages: list[dict[str, Any]] = []

    async def connected() -> bool:
        return False

    response = DisconnectAwareFileResponse(video, is_disconnected=connected)
    await response(_scope(method="HEAD"), _receive, _sender(messages))

    headers = {key.decode(): value.decode() for key, value in messages[0]["headers"]}
    assert headers["content-length"] == "128"
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_keeps_multiple_range_response(
    tmp_path: Path,
) -> None:
    video = tmp_path / "movie.mkv"
    content = b"0123456789" * 20
    video.write_bytes(content)
    messages: list[dict[str, Any]] = []

    async def connected() -> bool:
        return False

    response = DisconnectAwareFileResponse(video, is_disconnected=connected)
    await response(
        _scope(headers=[(b"range", b"bytes=0-9,100-109")]),
        _receive,
        _sender(messages),
    )

    headers = {key.decode(): value.decode() for key, value in messages[0]["headers"]}
    body = b"".join(message.get("body", b"") for message in messages[1:])
    assert messages[0]["status"] == 206
    assert headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert "content-length" not in headers
    assert b"Content-Range: bytes 0-9/200" in body
    assert b"Content-Range: bytes 100-109/200" in body
    assert content[:10] in body and content[100:110] in body
    assert messages[-1]["more_body"] is False


@pytest.mark.asyncio
async def test_disconnect_aware_file_response_keeps_head_range_response(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 128)
    messages: list[dict[str, Any]] = []

    async def connected() -> bool:
        return False

    response = DisconnectAwareFileResponse(video, is_disconnected=connected)
    await response(
        _scope(method="HEAD", headers=[(b"range", b"bytes=10-29")]),
        _receive,
        _sender(messages),
    )

    headers = {key.decode(): value.decode() for key, value in messages[0]["headers"]}
    assert messages[0]["status"] == 206
    assert headers["content-range"] == "bytes 10-29/128"
    assert headers["content-length"] == "20"
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_stopped_playback_ends_multiple_range_response(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 128)
    messages: list[dict[str, Any]] = []
    stopped = asyncio.Event()
    stopped.set()

    async def connected() -> bool:
        return False

    response = DisconnectAwareFileResponse(
        video,
        is_disconnected=connected,
        session_stopped=stopped,
    )
    await response(
        _scope(headers=[(b"range", b"bytes=0-9,100-109")]),
        _receive,
        _sender(messages),
    )

    assert messages[0]["status"] == 206
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
