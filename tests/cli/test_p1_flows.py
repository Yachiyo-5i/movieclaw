"""P1 端到端流程：危险确认、写操作、PAT Bearer 通道。"""

from __future__ import annotations

import json


def _login(run_mclaw, live_server, admin) -> None:
    result = run_mclaw(
        "login",
        "--server",
        live_server,
        "--username",
        admin["username"],
        "--password",
        admin["password"],
    )
    assert result.returncode == 0, result.stderr


def test_dangerous_without_yes_exits_5(run_mclaw) -> None:
    """零交互原则：非 TTY 下危险操作缺 --yes 直接退出码 5，且不发请求
    （无需配置服务器也能得到明确指引）。"""
    result = run_mclaw("subscriptions", "delete", "1")
    assert result.returncode == 5
    assert "--yes" in result.stderr


def test_dangerous_with_yes_reaches_server(run_mclaw, live_server, admin) -> None:
    _login(run_mclaw, live_server, admin)
    result = run_mclaw("subscriptions", "delete", "999", "--yes")
    assert result.returncode == 1  # 到达服务器：订阅不存在的业务错误
    assert "错误" in result.stderr


def test_write_operation_with_body_flags(run_mclaw, live_server, admin) -> None:
    _login(run_mclaw, live_server, admin)
    result = run_mclaw("auth", "profile", "update", "--nickname", "新昵称", "-o", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["nickname"] == "新昵称"
    assert "已更新" in result.stderr  # 服务端 message 透传到 stderr


def test_missing_required_body_flag_exits_2_with_hint(run_mclaw, live_server, admin) -> None:
    _login(run_mclaw, live_server, admin)
    result = run_mclaw("auth", "tokens", "create")
    assert result.returncode == 2
    assert "--name" in result.stderr
    assert "--input" in result.stderr  # hint 提到整体替代形态


def test_pat_token_channel(run_mclaw, live_server, admin, tmp_path) -> None:
    """PAT 全链路：登录创建令牌 → 全新环境仅凭 MOVIECLAW_TOKEN 调用成功
    → 吊销后立即 401（退出码 3）。这正是产品内 Agent 工作区的调用形态。"""
    _login(run_mclaw, live_server, admin)
    created = run_mclaw("auth", "tokens", "create", "--name", "ci", "-o", "json")
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    token = payload["token"]
    assert token.startswith("mclaw_")

    fresh_home = tmp_path / "fresh-cli-home"
    fresh_home.mkdir()
    env = {
        "MOVIECLAW_CONFIG_DIR": str(fresh_home),  # 无任何本地凭证
        "MOVIECLAW_SERVER": live_server,
        "MOVIECLAW_TOKEN": token,
    }
    result = run_mclaw("subscriptions", "list", "-o", "json", env_extra=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    revoked = run_mclaw("auth", "tokens", "revoke", payload["id"], "--yes")
    assert revoked.returncode == 0, revoked.stderr
    result = run_mclaw("subscriptions", "list", env_extra=env)
    assert result.returncode == 3
