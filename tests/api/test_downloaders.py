"""下载器配置管理接口的端到端测试。

覆盖：增删改查校验、保存后异步连接测试的状态流转、密码脱敏与落库加密、
启用停用、更新重测。真实的 create_downloader 被替换为假下载器，
不发真实请求，使状态流转可确定性断言。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

import movieclaw_api.services.downloader_config as downloader_service
from movieclaw_api.core.config import get_settings
from movieclaw_downloader import DownloaderConnectError, DownloaderInfo, TorrentBrief
from movieclaw_downloader.models import DownloaderConfig

# 假下载器行为开关：url 含 "unreachable" 时模拟连不上
_UNREACHABLE_MARK = "unreachable"

# 每次连接测试收到的 DownloaderConfig，供断言"传给适配器的密码已解密"
_captured_configs: list[DownloaderConfig] = []
_fake_torrents: list[TorrentBrief] = []


class _FakeDownloader:
    """假适配器：跳过真实网络，按 url 决定测试结果。"""

    def __init__(self, config: DownloaderConfig) -> None:
        self.config = config

    async def test_connection(self) -> DownloaderInfo:
        _captured_configs.append(self.config)
        if _UNREACHABLE_MARK in self.config.url:
            raise DownloaderConnectError("无法连接到 qBittorrent，请检查 WebUI 地址和端口")
        return DownloaderInfo(type=self.config.type, version="v5.0.2")

    async def list_torrents(self) -> list[TorrentBrief]:
        if "task-error" in self.config.url:
            raise DownloaderConnectError("读取下载任务失败")
        return list(_fake_torrents)

    async def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 每个测试用独立临时 SQLite 库与密钥文件，保证隔离
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()

    # 用假适配器替换真实下载器客户端
    _captured_configs.clear()
    _fake_torrents.clear()
    monkeypatch.setattr(downloader_service, "create_downloader", _FakeDownloader)
    import movieclaw_api.services.download_tasks as download_tasks_service

    monkeypatch.setattr(download_tasks_service, "create_downloader", _FakeDownloader)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    # 本文件只测下载器配置业务，登录鉴权用依赖覆盖绕过（鉴权本身在 test_auth 覆盖）
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:  # with 块内触发 lifespan：建库、迁移、初始化加密器
        yield c, db_file
    get_settings.cache_clear()


_PAYLOAD = {
    "name": "家里的 qBittorrent",
    "client_type": "qbittorrent",
    "url": "http://192.168.1.10:8080",
    "username": "admin",
    "password": "s3cret",
    "save_path": "/downloads",
}


def test_create_then_async_verify_active_and_desensitized(client) -> None:
    c, _ = client
    r = c.post("/api/v1/downloaders", json=_PAYLOAD)
    assert r.status_code == 200
    data = r.json()["data"]
    # 接口立即返回 verifying（同步占位），绝不回传密码
    assert data["status"] == "verifying"
    assert "password" not in data

    # TestClient 的 BackgroundTasks 在响应后同步执行完毕 → 再查已是终态
    detail = c.get(f"/api/v1/downloaders/{data['id']}").json()["data"]
    assert detail["status"] == "active"
    assert detail["usable"] is True
    assert detail["version"] == "v5.0.2"
    assert detail["last_error"] is None
    assert detail["last_checked_at"] is not None

    # 传给适配器的密码是解密后的明文（证明加密→解密链路正确）
    assert _captured_configs[-1].password == "s3cret"
    assert _captured_configs[-1].username == "admin"


def test_password_encrypted_at_rest(client) -> None:
    c, db_file = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)

    # 直接读 SQLite 文件核实落库形态：密文带 enc:: 前缀，不含明文
    row = sqlite3.connect(db_file).execute("SELECT password FROM downloader_client").fetchone()
    assert row[0].startswith("enc::")
    assert "s3cret" not in row[0]


def test_unreachable_downloader_marked_failed(client) -> None:
    c, _ = client
    payload = {**_PAYLOAD, "url": "http://unreachable:8080"}
    r = c.post("/api/v1/downloaders", json=payload)
    detail = c.get(f"/api/v1/downloaders/{r.json()['data']['id']}").json()["data"]
    assert detail["status"] == "failed"
    assert detail["usable"] is False
    assert "无法连接" in detail["last_error"]


def test_create_rejects_invalid_url(client) -> None:
    c, _ = client
    r = c.post("/api/v1/downloaders", json={**_PAYLOAD, "url": "192.168.1.10:8080"})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_create_rejects_duplicate_name(client) -> None:
    c, _ = client
    assert c.post("/api/v1/downloaders", json=_PAYLOAD).status_code == 200
    r = c.post("/api/v1/downloaders", json={**_PAYLOAD, "url": "http://other:8080"})
    assert r.status_code == 409
    assert "已被使用" in r.json()["message"]


def test_get_unknown_returns_404(client) -> None:
    c, _ = client
    assert c.get("/api/v1/downloaders/999").status_code == 404


def test_list_returns_all(client) -> None:
    c, _ = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)
    c.post(
        "/api/v1/downloaders",
        json={
            "name": "NAS 的 Transmission",
            "client_type": "transmission",
            "url": "http://192.168.1.20:9091",
        },
    )
    data = c.get("/api/v1/downloaders").json()["data"]
    assert [d["name"] for d in data] == ["家里的 qBittorrent", "NAS 的 Transmission"]
    # 未填用户名密码的下载器同样能通过测试（未开鉴权场景）
    assert data[1]["username"] is None
    assert data[1]["status"] == "active"


def test_task_center_aggregates_live_downloads_and_subscription_context(client) -> None:
    """只返回活跃/仍待入库任务，并为订阅任务补齐媒体身份、海报与季集上下文。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import MediaItem, RuleSet, Subscription, WantedItem, WantedStatus

    c, _ = client
    downloader_id = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    linked_hash = "a" * 40
    external_hash = "b" * 40
    old_seed_hash = "c" * 40
    missing_hash = "d" * 40
    media_identity: dict[str, int] = {}
    _fake_torrents.extend(
        [
            TorrentBrief(
                name="Subscription.Show.S01",
                content_name="Subscription.Show.S01",
                completed=True,
                info_hash=linked_hash,
                progress=1,
                size_bytes=1024,
                state="completed",
            ),
            TorrentBrief(
                name="External.Movie",
                content_name="External.Movie",
                completed=False,
                info_hash=external_hash,
                progress=0.42,
                dlspeed_bytes=2048,
                state="downloading",
            ),
            TorrentBrief(
                name="Old.Seed",
                content_name="Old.Seed",
                completed=True,
                info_hash=old_seed_hash,
                progress=1,
                state="completed",
            ),
        ]
    )

    async def seed_subscription() -> None:
        db = get_database()
        async with db.session() as session:
            rule = RuleSet(name="任务中心测试规则", is_default=True)
            media = MediaItem(
                kind="tv",
                tmdb_id=98765,
                title="聚合测试剧",
                original_title="Task Center Show",
                poster_path="/task-center-show.jpg",
            )
            session.add(rule)
            session.add(media)
            await session.flush()
            assert rule.id is not None and media.id is not None
            media_identity["id"] = media.id
            subscription = Subscription(
                media_item_id=media.id,
                kind="tv",
                selected_seasons=[1],
                rule_set_id=rule.id,
            )
            session.add(subscription)
            await session.flush()
            assert subscription.id is not None
            session.add_all(
                [
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=1,
                        status=WantedStatus.GRABBED,
                        info_hash=linked_hash,
                    ),
                    WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=media.id,
                        season_number=1,
                        episode_number=2,
                        status=WantedStatus.GRABBED,
                        info_hash=missing_hash,
                    ),
                ]
            )
            await session.commit()

    asyncio.run(seed_subscription())

    response = c.get("/api/v1/downloaders/tasks")
    assert response.status_code == 200
    payload = response.json()["data"]
    by_hash = {item["info_hash"]: item for item in payload["items"]}
    assert set(by_hash) == {linked_hash, external_hash, missing_hash}
    assert by_hash[linked_hash]["source"] == "subscription"
    assert by_hash[linked_hash]["state"] == "completed"
    assert by_hash[linked_hash]["media_item_id"] == media_identity["id"]
    assert by_hash[linked_hash]["media_title"] == "聚合测试剧"
    assert by_hash[linked_hash]["poster_url"].endswith("/w342/task-center-show.jpg")
    assert by_hash[linked_hash]["subscriptions"][0]["media_item_id"] == media_identity["id"]
    assert (
        by_hash[linked_hash]["subscriptions"][0]["poster_url"] == by_hash[linked_hash]["poster_url"]
    )
    assert by_hash[linked_hash]["subscriptions"][0]["units"] == [
        {"season_number": 1, "episode_number": 1}
    ]
    assert by_hash[external_hash]["source"] == "external"
    assert by_hash[external_hash]["progress"] == 0.42
    assert by_hash[missing_hash]["state"] == "missing"
    assert by_hash[missing_hash]["downloader_id"] is None
    # 同一剧集的多个资源带回同一个稳定媒体 ID，前端据此安全合并；不按标题猜测。
    assert by_hash[missing_hash]["media_item_id"] == by_hash[linked_hash]["media_item_id"]
    assert payload["sources"] == [
        {
            "id": downloader_id,
            "name": _PAYLOAD["name"],
            "client_type": "qbittorrent",
            "status": "active",
            "message": None,
            "task_count": 2,
        }
    ]


def test_task_center_degrades_single_downloader_failure(client) -> None:
    """一台下载器读取失败只标记来源异常，不能拖垮其余下载器任务。"""
    c, _ = client
    c.post("/api/v1/downloaders", json=_PAYLOAD)
    failed = c.post(
        "/api/v1/downloaders",
        json={
            "name": "故障下载器",
            "client_type": "transmission",
            "url": "http://task-error:9091",
        },
    ).json()["data"]
    _fake_torrents.append(
        TorrentBrief(
            name="Still.Visible",
            content_name="Still.Visible",
            completed=False,
            info_hash="e" * 40,
            progress=0.2,
            state="downloading",
        )
    )

    response = c.get("/api/v1/downloaders/tasks")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert [item["name"] for item in payload["items"]] == ["Still.Visible"]
    sources = {source["id"]: source for source in payload["sources"]}
    assert sources[failed["id"]]["status"] == "error"
    assert "检查下载器连接" in sources[failed["id"]]["message"]


def test_enable_disable_toggles_usable(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]

    r = c.patch(f"/api/v1/downloaders/{did}/status", json={"enabled": False})
    assert r.json()["data"]["enabled"] is False
    assert r.json()["data"]["usable"] is False  # 已停用即不可用，与验证状态无关

    r = c.patch(f"/api/v1/downloaders/{did}/status", json={"enabled": True})
    assert r.json()["data"]["usable"] is True


def test_update_overwrites_and_reverifies(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]

    updated = {
        "name": "改名后的 qB",
        "client_type": "qbittorrent",
        "url": "http://10.0.0.2:8080",
        # 不再填用户名密码 → 覆盖为 None（未开鉴权语义）
    }
    r = c.put(f"/api/v1/downloaders/{did}", json=updated)
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "verifying"

    detail = c.get(f"/api/v1/downloaders/{did}").json()["data"]
    assert detail["name"] == "改名后的 qB"
    assert detail["url"] == "http://10.0.0.2:8080"
    assert detail["username"] is None
    assert detail["status"] == "active"
    # 重测时传给适配器的凭证已被覆盖清空
    assert _captured_configs[-1].password is None


def test_reverify_endpoint(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    checks_before = len(_captured_configs)

    r = c.post(f"/api/v1/downloaders/{did}/verify")
    assert r.status_code == 200
    assert len(_captured_configs) == checks_before + 1
    assert c.get(f"/api/v1/downloaders/{did}").json()["data"]["status"] == "active"


def _create_two(c) -> tuple[int, int]:
    """建两台下载器，返回 (第一台 id, 第二台 id)。"""
    first = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]
    second = c.post(
        "/api/v1/downloaders",
        json={
            "name": "NAS 的 Transmission",
            "client_type": "transmission",
            "url": "http://192.168.1.20:9091",
        },
    ).json()["data"]
    return first["id"], second["id"]


def test_first_created_becomes_default(client) -> None:
    c, _ = client
    first_id, second_id = _create_two(c)
    data = {d["id"]: d for d in c.get("/api/v1/downloaders").json()["data"]}
    assert data[first_id]["is_default"] is True
    assert data[second_id]["is_default"] is False


def test_set_default_switches_exclusively(client) -> None:
    c, _ = client
    first_id, second_id = _create_two(c)

    r = c.post(f"/api/v1/downloaders/{second_id}/default")
    assert r.status_code == 200
    assert r.json()["data"]["is_default"] is True

    # 有且只有一个默认：原默认被清掉
    data = {d["id"]: d for d in c.get("/api/v1/downloaders").json()["data"]}
    assert data[first_id]["is_default"] is False
    assert data[second_id]["is_default"] is True

    assert c.post("/api/v1/downloaders/999/default").status_code == 404


def test_delete_default_promotes_remaining(client) -> None:
    c, _ = client
    first_id, second_id = _create_two(c)

    assert c.delete(f"/api/v1/downloaders/{first_id}").status_code == 200
    remaining = c.get("/api/v1/downloaders").json()["data"]
    assert [d["id"] for d in remaining] == [second_id]
    # 默认自动让位给剩下的一台，一键下载永远有目标
    assert remaining[0]["is_default"] is True

    # 删非默认不影响默认归属
    third = c.post(
        "/api/v1/downloaders",
        json={"name": "第三台", "client_type": "qbittorrent", "url": "http://x:8080"},
    ).json()["data"]
    assert third["is_default"] is False
    c.delete(f"/api/v1/downloaders/{third['id']}")
    data = c.get("/api/v1/downloaders").json()["data"]
    assert data[0]["id"] == second_id and data[0]["is_default"] is True


def test_delete_then_404(client) -> None:
    c, _ = client
    did = c.post("/api/v1/downloaders", json=_PAYLOAD).json()["data"]["id"]
    assert c.delete(f"/api/v1/downloaders/{did}").status_code == 200
    assert c.get(f"/api/v1/downloaders/{did}").status_code == 404
    assert c.delete(f"/api/v1/downloaders/{did}").status_code == 404
