"""OpenAPI 指纹跨 app 缓存的回归测试。"""

from __future__ import annotations

from fastapi import FastAPI

import movieclaw_api.spec_state as spec_state
from movieclaw_api.app import create_app


def test_equivalent_apps_share_spec_hash_generation(monkeypatch) -> None:
    """create_app 的同构实例只生成一次 OpenAPI，结果仍写回各自 app.state。"""
    spec_state._shared_spec_hashes.clear()
    calls = 0
    original = spec_state.spec_hash

    def counted(spec):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(spec)

    monkeypatch.setattr(spec_state, "spec_hash", counted)
    first = create_app()
    second = create_app()

    assert spec_state.get_spec_hash(first) == spec_state.get_spec_hash(second)
    assert calls == 1
    assert first.state.spec_hash == second.state.spec_hash


def test_different_route_tables_do_not_share_spec_hash(monkeypatch) -> None:
    """动态追加路由会改变签名，不能复用另一份 app 的指纹。"""
    spec_state._shared_spec_hashes.clear()
    calls = 0
    original = spec_state.spec_hash

    def counted(spec):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(spec)

    def shared_endpoint() -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(spec_state, "spec_hash", counted)
    first = FastAPI()
    first.add_api_route("/first", shared_endpoint)
    second = FastAPI()
    second.add_api_route("/second", shared_endpoint)

    assert spec_state.get_spec_hash(first) != spec_state.get_spec_hash(second)
    assert calls == 2
