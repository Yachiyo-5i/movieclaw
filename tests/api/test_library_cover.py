"""库封面拼贴的并发去重回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from movieclaw_api.services.library import cover


async def test_concurrent_cover_requests_share_selection_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一库的并发请求只能执行一次素材选择和拼贴渲染。"""
    selected = 0
    rendered = 0
    selecting = asyncio.Event()
    release = asyncio.Event()
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"poster")

    async def select_once(_library_id: int) -> list[Path]:
        nonlocal selected
        selected += 1
        selecting.set()
        await release.wait()
        return [poster]

    def render_once(_posters: list[Path], output: Path) -> None:
        nonlocal rendered
        rendered += 1
        output.write_bytes(b"cover")

    monkeypatch.setattr(cover, "select_cover_posters", select_once)
    monkeypatch.setattr(cover, "_cover_key", lambda _paths: "cover-key")
    monkeypatch.setattr(cover, "covers_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(cover, "render_shelf_collage", render_once)

    tasks = [asyncio.create_task(cover.ensure_library_cover(42)) for _ in range(5)]
    await asyncio.wait_for(selecting.wait(), timeout=1)
    assert selected == 1
    release.set()

    results = await asyncio.gather(*tasks)
    assert results == [(tmp_path / "covers" / "42-cover-key.jpg", "cover-key")] * 5
    assert rendered == 1
    await asyncio.sleep(0)
    assert 42 not in cover._cover_tasks


async def test_no_poster_result_does_not_leave_cover_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无可用海报时任务会清理，之后的调用能够重新选择素材。"""
    selected = 0

    async def no_posters(_library_id: int) -> list[Path]:
        nonlocal selected
        selected += 1
        return []

    monkeypatch.setattr(cover, "select_cover_posters", no_posters)

    assert await cover.ensure_library_cover(43) is None
    await asyncio.sleep(0)
    assert 43 not in cover._cover_tasks
    assert await cover.ensure_library_cover(43) is None
    assert selected == 2


async def test_selection_error_does_not_leave_cover_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """选择素材异常仍会回收任务，避免该库后续请求永久复用失败任务。"""
    calls = 0

    async def fail_selection(_library_id: int) -> list[Path]:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(cover, "select_cover_posters", fail_selection)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await cover.ensure_library_cover(44)
    await asyncio.sleep(0)
    assert 44 not in cover._cover_tasks

    with pytest.raises(RuntimeError, match="database unavailable"):
        await cover.ensure_library_cover(44)
    assert calls == 2
