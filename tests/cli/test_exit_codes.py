"""退出码契约测试（docs/design/cli.md §5.8）。

退出码是脚本与 Agent 的机器接口：0 成功 / 1 业务 / 2 用法 / 3 认证 /
4 网络。这里用真实子进程逐一钉死，防止将来无意改变含义。
"""

from __future__ import annotations


def test_help_exits_zero(run_mclaw) -> None:
    result = run_mclaw("--help")
    assert result.returncode == 0
    assert "movieclaw 命令行工具" in result.stdout
    assert "发现电影/剧集" in result.stdout
    assert "搜索和下载 PT 资源" in result.stdout


def test_offline_help_browsable_without_server(run_mclaw) -> None:
    """断网状态 --help 全树可浏览（内置基线 spec，P0 验证标准）。"""
    for args in (["sub", "--help"], ["lib", "--help"], ["sub", "list", "--help"]):
        result = run_mclaw(*args)
        assert result.returncode == 0, result.stderr
        assert result.stdout


def test_rules_help_lists_frontend_filter_fields(run_mclaw) -> None:
    """规则页已暴露的字段也必须出现在离线 CLI 帮助里，Agent 才能正确组 JSON。"""
    result = run_mclaw("rules", "create", "--help")
    assert result.returncode == 0, result.stderr
    for field in (
        "subtitle_languages_require",
        "audio_languages_require",
        "hr_unknown_policy",
    ):
        assert field in result.stdout


def test_usage_error_exits_2(run_mclaw) -> None:
    result = run_mclaw("sub", "show")  # 缺少必填的 subscription_id
    assert result.returncode == 2


def test_unknown_command_exits_2(run_mclaw) -> None:
    result = run_mclaw("sub", "nonexistent")
    assert result.returncode == 2


def test_no_server_configured_exits_2_with_hint(run_mclaw) -> None:
    result = run_mclaw("sub", "list")
    assert result.returncode == 2
    assert "未指定 movieclaw 服务器地址" in result.stderr
    assert "MOVIECLAW_SERVER" in result.stderr


def test_unreachable_server_exits_4(run_mclaw) -> None:
    result = run_mclaw("--server", "http://127.0.0.1:9", "--timeout", "3", "sub", "list")
    assert result.returncode == 4
    assert "无法连接" in result.stderr


def test_unauthenticated_exits_3_with_login_hint(run_mclaw, live_server) -> None:
    result = run_mclaw("--server", live_server, "sub", "list")
    assert result.returncode == 3
    assert "mclaw login" in result.stderr


def test_server_url_without_scheme_exits_4_with_hint(run_mclaw) -> None:
    """地址漏写 http:// 前缀（极常见）：中文提示而非裸 traceback。"""
    result = run_mclaw("--server", "127.0.0.1:9", "--timeout", "3", "sub", "list")
    assert result.returncode == 4
    assert "http://" in result.stderr
    assert "Traceback" not in result.stderr
