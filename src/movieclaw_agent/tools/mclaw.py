"""mclaw 工具：模型操作 movieclaw 产品本身的唯一入口。

设计（docs/design/agent-cli-integration.md）：
- 独立工具而非 bash 捎带——模型在「选工具」层直接看见产品操作入口，
  description 携带一级服务目录（由 API 层从 spec 渲染后传入，防漂移）；
- 参数面最小：args 单字符串 + 可选 timeout。mclaw 自身就是完整的参数体系
  （--help 齐全、错误带 hint），工具层不重复建模；
- shlex 解析后以 argv 直接执行，不经 shell——无管道/变量展开，注入面为零；
  令牌只注入本工具子进程，bash 环境里拿不到（泄漏面收窄）；
- handler 硬闸：agent 子命令（递归）与 login/logout（破坏凭证）直接拒绝；
- 退出码语义标注：结果尾部按退出码契约附一行中文解读，模型从工具结果
  本身就能学会正确的下一步（运行时教学，不占 description）。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

from movieclaw_agent.toolkit import AgentTool
from movieclaw_agent.tools.bash import _truncate_tail
from movieclaw_llm import ToolDefinition

_DEFAULT_TIMEOUT = 300.0

# 退出码 → 给模型的下一步指引（与 movieclaw_cli.core.errors.ExitCode 契约一致）
_EXIT_CODE_NOTES = {
    1: "业务错误——原因与建议见 stderr",
    2: "用法错误——先执行对应命令的 --help 纠正参数再试",
    3: "授权失效——不要重试，直接向用户报告",
    4: "无法连接服务器——网络问题，可稍后重试",
    5: "该操作需要确认——核对无误后在参数末尾加 --yes 重跑",
    6: "等待超时，任务仍在后台执行——用对应查询命令轮询进度",
    7: "结果有歧义——stdout 是候选清单，选定后按 stderr 提示重跑",
}

# 硬闸：这些子命令在工具里没有任何合法用途
_BLOCKED = {
    "agent": "不能在工具里调用 mclaw agent——那是你自己的运行入口（禁止递归）。"
    "请直接在当前会话完成任务。",
    "login": "授权已自动配置，不需要也不允许执行 login/logout（会破坏凭证状态）。",
    "logout": "授权已自动配置，不需要也不允许执行 login/logout（会破坏凭证状态）。",
}

# Agent 的工作区令牌只应访问创建该运行的本机服务。--server/--context 是人类
# CLI 的多实例便利功能；在 Agent 里保留它们会把 MOVIECLAW_TOKEN 一并发往
# 任意 URL，形成令牌外泄与 SSRF 通道。删除下载数据同样不能由模型自己把
# --yes 拼出来，先只开放可逆的「删除任务、保留文件」能力。
_BLOCKED_OPTIONS = ("--server", "--context")
_DISK_DELETE_COMMAND = ("dl", "torrent", "files", "delete")


def _validate_agent_argv(argv: list[str]) -> None:
    """在启动子进程前拒绝会扩大 Agent 权限边界的参数组合。"""
    for arg in argv:
        if arg in _BLOCKED_OPTIONS or arg.startswith(("--server=", "--context=")):
            raise ValueError(
                "产品内 Agent 不允许覆盖服务器或上下文，"
                "以免将工作区授权令牌发送到其他地址"
            )
    if tuple(argv[: len(_DISK_DELETE_COMMAND)]) == _DISK_DELETE_COMMAND:
        raise ValueError(
            "产品内 Agent 不能删除已下载文件；"
            "如需清除磁盘数据，请由用户在 Web 界面或本机 CLI 中明确执行"
        )

_PROTOCOL = """\
movieclaw 的官方命令行工具。对本产品的一切操作——搜索资源、订阅、媒体库、下载、\
站点/下载器/规则等全部设置——都用本工具完成（不要通过 bash 调用，bash 环境没有授权）。\
授权已自动配置，永远不需要 login。

{service_map}

使用协议：
- 输出即数据：stdout 是 JSON（默认），stderr 是过程提示与错误原因。
- 参数拿不准就先 --help（域级与命令级都有，含示例），不要凭记忆猜参数或取值。
- 列表默认有条数上限、长字段有截断；下结论前确认数据没有被截断（--limit 可调）。
- 带 ⚠ 的命令需要 --yes 确认。其中 lib items delete 会删除磁盘上的媒体文件：\
必须先用只读命令查清将删除的具体条目、向用户复述并取得本轮明确同意后才能执行；\
用户泛泛说「清理/整理」不构成删除文件的同意。其余 ⚠ 命令（删配置、清记录）在用户\
任务明确要求时可直接 --yes。
- dl torrent delete 默认只删 qBittorrent 任务、保留数据。产品内 Agent 不可删除\
 已下载文件；如需清除磁盘数据，用户须在 Web 界面或本机 CLI 中明确执行。
- 扫描/整理/元数据刷新默认阻塞等待完成；预计超过 4 分钟的任务用 --no-wait 启动后\
轮询进度，或调大本工具的 timeout 参数。"""


def build_description(service_map: str) -> str:
    """组装完整工具描述：静态使用协议 + 动态一级服务目录。"""
    return _PROTOCOL.format(service_map=service_map.strip()) + "\n"


def make_mclaw_tool(
    workdir: Path,
    extra_env: dict[str, str],
    service_map: str,
) -> AgentTool:
    """构建 mclaw 工具。

    extra_env：MOVIECLAW_SERVER / MOVIECLAW_TOKEN——只进本工具的子进程；
    service_map：一级服务目录文本，由调用方从 spec 渲染（见
    movieclaw_api.services.mclaw_tool），保证与真实命令面同版。
    """

    async def handler(args: dict) -> str:
        raw: str = args["args"]
        timeout = float(args.get("timeout") or _DEFAULT_TIMEOUT)
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            raise ValueError(f"参数串无法解析（引号不配对？）：{exc}") from None
        if not argv:
            raise ValueError('args 不能为空；例如 args="sub list" 或 args="--help"')
        if note := _BLOCKED.get(argv[0]):
            raise ValueError(note)
        _validate_agent_argv(argv)

        child_env = {**os.environ, **extra_env}
        if server := extra_env.get("MOVIECLAW_SERVER"):
            # CLI HTTP 层的第二道出口闸门（见 core.config.ENV_AGENT_LOCKED_SERVER）。
            child_env["MOVIECLAW_AGENT_LOCKED_SERVER"] = server.rstrip("/")

        # 与当前解释器同环境的 CLI 入口（同 venv/镜像内必然可用，无需依赖 PATH）
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "movieclaw_cli",
            *argv,
            cwd=workdir,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ValueError(
                f"命令执行超时（{timeout:.0f} 秒），已终止；"
                "长任务可用 --no-wait 启动后轮询，或调大 timeout 参数"
            ) from None

        sections: list[str] = []
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        if stdout.strip():
            sections.append(_truncate_tail(stdout, label="stdout"))
        if stderr.strip():
            sections.append("[stderr]\n" + _truncate_tail(stderr, label="stderr"))
        code = proc.returncode or 0
        if code != 0:
            note = _EXIT_CODE_NOTES.get(code, "")
            sections.append(f"[退出码 {code}{'：' + note if note else ''}]")
        return "\n".join(sections) or "（命令无输出，退出码 0）"

    return AgentTool(
        definition=ToolDefinition(
            name="mclaw",
            description=build_description(service_map),
            parameters={
                "type": "object",
                "properties": {
                    "args": {
                        "type": "string",
                        "description": "mclaw 后面的完整参数串（不含 mclaw 本身），"
                        '如 "sub list" 或 "search \\"沙丘2\\" --resolution 2160p"',
                    },
                    "timeout": {
                        "type": "number",
                        "description": f"超时秒数（可选，默认 {_DEFAULT_TIMEOUT:.0f}；"
                        "等待长任务时适当调大）",
                    },
                },
                "required": ["args"],
            },
        ),
        handler=handler,
    )
