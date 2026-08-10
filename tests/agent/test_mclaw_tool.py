"""mclaw 工具的行为测试（docs/design/agent-cli-integration.md §5）。

覆盖：硬闸（agent 递归 / login）、参数解析错误、真实子进程执行（--help
无需服务器）、退出码语义标注、令牌隔离（bash 拿不到、mclaw 工具拿到）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from movieclaw_agent.tools.bash import make_bash_tool
from movieclaw_agent.tools.mclaw import build_description, make_mclaw_tool


def _tool(tmp_path: Path, env: dict[str, str] | None = None):
    return make_mclaw_tool(tmp_path, env or {}, "可用服务：\n- sub 订阅")


def test_agent_subcommand_is_hard_blocked(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    with pytest.raises(ValueError, match="禁止递归"):
        asyncio.run(tool.handler({"args": "agent run 帮我整理"}))


def test_login_logout_blocked(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    for cmd in ("login --server http://x", "logout"):
        with pytest.raises(ValueError, match="不允许执行 login/logout"):
            asyncio.run(tool.handler({"args": cmd}))


@pytest.mark.parametrize(
    "args",
    (
        "sub list --server http://example.invalid",
        "sub list --server=http://example.invalid",
        "sub list --context other",
        "dl torrent files delete 1 " + "a" * 40 + " --yes",
    ),
)
def test_agent_blocks_token_exfiltration_and_disk_deletion(tmp_path: Path, args: str) -> None:
    """高权限工作区令牌不能改投其他服务，也不能由模型删除磁盘数据。"""
    tool = _tool(tmp_path)
    with pytest.raises(ValueError, match="不允许覆盖服务器|不能删除已下载文件"):
        asyncio.run(tool.handler({"args": args}))


def test_unparseable_args_gives_chinese_error(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    with pytest.raises(ValueError, match="引号不配对"):
        asyncio.run(tool.handler({"args": 'search "沙丘'}))
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(tool.handler({"args": "  "}))


def test_help_runs_without_server(tmp_path: Path) -> None:
    """--help 走内置基线 spec，断网/无服务器也能探索命令面。"""
    tool = _tool(tmp_path)
    result = asyncio.run(tool.handler({"args": "sub --help"}))
    assert "create" in result and "list" in result
    assert "[退出码" not in result  # 成功不标注


def test_exit_code_annotation_teaches_next_step(tmp_path: Path) -> None:
    """用法错误（2）的结果里附中文指引：先 --help 再试。"""
    tool = _tool(tmp_path)
    result = asyncio.run(tool.handler({"args": "sub show"}))  # 缺必填参数
    assert "[退出码 2" in result and "--help" in result


def test_network_failure_annotated_as_exit_4(tmp_path: Path) -> None:
    tool = _tool(
        tmp_path,
        {"MOVIECLAW_SERVER": "http://127.0.0.1:9", "MOVIECLAW_CONFIG_DIR": str(tmp_path)},
    )
    result = asyncio.run(tool.handler({"args": "sub list --timeout 3"}))
    assert "[退出码 4" in result and "无法连接" in result


def test_token_only_reaches_mclaw_tool_not_bash(tmp_path: Path) -> None:
    """令牌隔离：mclaw 工具子进程拿得到注入环境，bash 工具拿不到。"""
    bash = make_bash_tool(tmp_path)  # 装配层不再向 bash 注入 cli env
    result = asyncio.run(bash.handler({"command": "echo token=[$MOVIECLAW_TOKEN]"}))
    assert "token=[]" in result

    mclaw = _tool(tmp_path, {"MOVIECLAW_TOKEN": "tok-1", "MOVIECLAW_SERVER": "http://127.0.0.1:9"})
    # 工具子进程确实带上了环境：借 --debug 的脱敏通道无从验证，这里用
    # 网络失败路径证明 SERVER 环境生效（地址来自注入值）
    result = asyncio.run(mclaw.handler({"args": "sub list --timeout 3"}))
    assert "127.0.0.1:9" in result


def test_description_contains_service_map_and_protocol() -> None:
    desc = build_description("可用服务：\n- sub 订阅")
    assert "可用服务" in desc and "sub 订阅" in desc
    assert "--help" in desc and "--yes" in desc
    assert "Agent 不可删除" in desc
    assert "bash" in desc  # 排他性引导：不要经 bash 调用
