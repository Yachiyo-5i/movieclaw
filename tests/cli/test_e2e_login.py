"""端到端冒烟：真实 uvicorn + 真实子进程 CLI，P0 验证标准的落地。

覆盖「mclaw login && mclaw subscriptions list -o json 远程全通」这条主链路，
以及登录后的上下文记忆、status、logout 清凭证。
"""

from __future__ import annotations

import json


def test_login_then_list_subscriptions(run_mclaw, live_server, admin) -> None:
    login = run_mclaw(
        "login",
        "--server",
        live_server,
        "--username",
        admin["username"],
        "--password",
        admin["password"],
    )
    assert login.returncode == 0, login.stderr
    assert "登录成功" in login.stderr

    # 登录已把服务器记入上下文，后续命令无需 --server
    result = run_mclaw("subscriptions", "list", "-o", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    # 生成命令带 query 参数也全通
    history = run_mclaw("search", "history", "list", "--limit", "5", "-o", "json")
    assert history.returncode == 0, history.stderr
    assert json.loads(history.stdout) == []


def test_status_shows_identity(run_mclaw, live_server, admin) -> None:
    run_mclaw(
        "login",
        "--server",
        live_server,
        "--username",
        admin["username"],
        "--password",
        admin["password"],
    )
    result = run_mclaw("status", "-o", "json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["server"] == live_server
    assert payload["cli_spec_hash"]


def test_logout_clears_credentials(run_mclaw, live_server, admin) -> None:
    run_mclaw(
        "login",
        "--server",
        live_server,
        "--username",
        admin["username"],
        "--password",
        admin["password"],
    )
    logout = run_mclaw("logout")
    assert logout.returncode == 0, logout.stderr

    result = run_mclaw("subscriptions", "list")
    assert result.returncode == 3  # 凭证已清，回到未登录


def test_login_non_interactive_requires_flags(run_mclaw, live_server) -> None:
    """零交互原则：非 TTY 缺用户名/密码必须直接失败，不允许挂起等输入。"""
    result = run_mclaw("login", "--server", live_server)
    assert result.returncode == 2
    assert "--username" in result.stderr
