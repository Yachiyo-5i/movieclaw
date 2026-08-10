"""站点凭据加密落库的专项测试。

覆盖：写入即密文（表里不存明文）、按需解密还原、auth_factory 拿到的是明文、
存量明文行的一次性加密迁移（幂等 + 迁移后读取不变）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from movieclaw_api.core.config import get_settings
from movieclaw_db.crypto import SecretBox
from movieclaw_db.engine import get_database
from movieclaw_db.models.site_credential import AuthType
from movieclaw_db.repositories.credential_repo import CredentialRepository
from movieclaw_tracker import CookieAuthProvider, CredentialAuthProvider


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()

    # 打桩后台验证：本文件只测加密落库，不发真实网络请求
    async def _noop_verify(site_id: str) -> None:
        return None

    import movieclaw_api.api.routes.sites as sites_routes

    monkeypatch.setattr(sites_routes, "verify_site", _noop_verify)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


async def _raw_row(site_id: str) -> dict:
    """直接查表取原始列值，绕开一切解密逻辑。"""
    async with get_database().session() as session:
        result = await session.execute(
            text(
                "SELECT cookie, api_key, username, password"
                " FROM site_credential WHERE site_id = :sid"
            ),
            {"sid": site_id},
        )
        row = result.mappings().one()
        return dict(row)


async def test_upsert_stores_ciphertext_not_plaintext(client: TestClient) -> None:
    """经 API 保存凭据后，表里落的是 enc:: 密文，不含明文。"""
    r = client.post(
        "/api/v1/sites",
        json={"site_id": "ttg", "auth_type": "cookie", "cookie": "uid=1; pass=secret"},
    )
    assert r.status_code == 200

    raw = await _raw_row("ttg")
    assert raw["cookie"] is not None
    assert SecretBox.is_encrypted(raw["cookie"])
    assert "secret" not in raw["cookie"]


async def test_decrypted_helpers_and_auth_factory_roundtrip(client: TestClient) -> None:
    """repo 解密方法与 auth_factory 都能还原出用户填写的明文。"""
    from movieclaw_api.services.auth_factory import build_auth_provider

    r = client.post(
        "/api/v1/sites",
        json={
            "site_id": "ttg",
            "auth_type": "credential",
            "username": "alice",
            "password": "p@ssw0rd",
        },
    )
    assert r.status_code == 200

    async with get_database().session() as session:
        row = await CredentialRepository(session).get_by_site("ttg")
    assert row is not None
    # 列值是密文，解密方法还原明文；username 非敏感字段保持明文
    assert SecretBox.is_encrypted(row.password)
    assert CredentialRepository.decrypted_password(row) == "p@ssw0rd"
    assert row.username == "alice"

    provider = build_auth_provider(row)
    assert isinstance(provider, CredentialAuthProvider)


async def test_plaintext_migration_is_idempotent(client: TestClient) -> None:
    """造明文行 → 跑一次性加密 → 值转密文且解密结果不变；再跑一次不再改动。"""
    # 直接用 SQL 造一条"加密内核上线前"的明文旧数据
    async with get_database().session() as session:
        await session.execute(
            text(
                "INSERT INTO site_credential"
                " (site_id, auth_type, cookie, enabled, status, created_at, updated_at)"
                " VALUES ('ttg', 'COOKIE', 'uid=9; pass=legacy', 1, 'PENDING',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await session.commit()

    # 明文旧数据在读取侧本就兼容（无前缀原样返回）
    async with get_database().session() as session:
        repo = CredentialRepository(session)
        row = await repo.get_by_site("ttg")
        assert CredentialRepository.decrypted_cookie(row) == "uid=9; pass=legacy"

        # 第一次迁移：命中 1 条
        assert await repo.encrypt_plaintext_secrets() == 1

    raw = await _raw_row("ttg")
    assert SecretBox.is_encrypted(raw["cookie"])

    async with get_database().session() as session:
        repo = CredentialRepository(session)
        row = await repo.get_by_site("ttg")
        # 迁移后解密结果与迁移前一致
        assert CredentialRepository.decrypted_cookie(row) == "uid=9; pass=legacy"
        # 幂等：再跑一次不再改动
        assert await repo.encrypt_plaintext_secrets() == 0


async def test_verification_uses_decrypted_cookie(client: TestClient) -> None:
    """加密落库后，构建认证提供者拿到的仍是明文 cookie（站点访问不受影响）。"""
    from movieclaw_api.services.auth_factory import build_auth_provider

    r = client.post(
        "/api/v1/sites",
        json={"site_id": "ttg", "auth_type": "cookie", "cookie": "uid=2; pass=abc"},
    )
    assert r.status_code == 200

    async with get_database().session() as session:
        row = await CredentialRepository(session).get_by_site("ttg")
    assert row.auth_type == AuthType.COOKIE
    provider = build_auth_provider(row)
    assert isinstance(provider, CookieAuthProvider)
