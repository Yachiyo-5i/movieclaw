"""mclaw 入口：全局标志 + 精选命令注册 + 生成命令树装配。

命令面两层（docs/design/cli.md §1）：
- 精选命令（overlay）：login / logout / status 等跨接口工作流，先注册；
- 生成命令（gen）：由 spec（内置基线，或偏斜刷新后的服务器缓存）动态
  构建，后挂载，同名让位于精选层。缓存 spec 建树失败自动回退内置基线。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import click
import httpx

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError
from movieclaw_cli.core.http import Api
from movieclaw_cli.gen import spec_loader
from movieclaw_cli.gen.tree_builder import DOMAIN_HELP, build_tree
from movieclaw_cli.overlay.agent_cmds import agent_attach, agent_run
from movieclaw_cli.overlay.auth_cmds import login, logout, status
from movieclaw_cli.overlay.groups import DefaultCommandGroup
from movieclaw_cli.overlay.jobs_cmds import jobs_wait
from movieclaw_cli.overlay.lib_cmds import lib_organize
from movieclaw_cli.overlay.logs_cmds import logs_tail
from movieclaw_cli.overlay.search_cmds import download, search
from movieclaw_cli.overlay.sub_cmds import sub_create


@dataclass
class Settings:
    """一次调用的全局标志集合，经 ctx.obj 传给所有命令。"""

    server: str | None = None
    context: str | None = None
    output: str | None = None
    timeout: float = 30.0
    debug: bool = False
    quiet: bool = False
    yes: bool = False
    # 测试注入点：端到端测试用 httpx MockTransport/ASGI 替换真实网络
    transport: httpx.BaseTransport | None = None

    def make_api(self, server: str | None = None) -> Api:
        target = server or cfg.resolve_server(self.server, self.context)
        return Api(target, timeout=self.timeout, debug=self.debug, transport=self.transport)


@click.group(
    name="mclaw",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    help="movieclaw 命令行工具：搜索、订阅、媒体库、下载、设置——页面能做的这里都能做。\n\n"
    "探索方式：mclaw <域> --help 看该域全部命令，mclaw <域> <命令> --help 看参数与示例。\n"
    "机器输出：加 -o json（非终端环境默认即 JSON）。",
)
@click.option("--server", help="movieclaw 服务器地址（优先级最高，覆盖上下文与环境变量）")
@click.option("--context", help="使用指定上下文（见 ~/.config/movieclaw/config.toml）")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["table", "json", "yaml"]),
    help="输出格式；缺省 TTY 为 table、管道/Agent 为 json",
)
@click.option("--timeout", type=float, default=30.0, show_default=True, help="请求超时（秒）")
@click.option("--debug", is_flag=True, help="打印请求/响应调试信息到 stderr（凭证自动打码）")
@click.option("--quiet", is_flag=True, help="成功时不输出数据（配合退出码使用）")
@click.option("--yes", is_flag=True, help="跳过确认提示（破坏性操作需要显式给出）")
@click.pass_context
def cli(ctx: click.Context, **kwargs) -> None:
    transport = None
    if ctx.obj is not None and isinstance(ctx.obj, Settings):
        transport = ctx.obj.transport  # 测试预置的 transport 透传
    ctx.obj = Settings(transport=transport, **kwargs)


def _assemble() -> None:
    """装配命令树：精选命令先注册（同名让位机制），生成命令后挂载
    （缓存 spec 失败回退内置基线）。"""
    cli.add_command(login)
    cli.add_command(logout)
    cli.add_command(status)
    cli.add_command(download)

    # 预建带精选子命令的组：生成层会把该域其余命令并入同一个组。
    # search 组是「默认子命令组」：mclaw search "沙丘2" = mclaw search run "沙丘2"，
    # 与 mclaw search history list 等生成命令共存。
    search_group = DefaultCommandGroup(
        name="search", default_command="run", help=DOMAIN_HELP.get("search")
    )
    search_group.add_command(search)
    cli.add_command(search_group)

    sub_group = click.Group(name="sub", help=DOMAIN_HELP.get("sub"))
    sub_group.add_command(sub_create)
    cli.add_command(sub_group)

    lib_group = click.Group(name="lib", help=DOMAIN_HELP.get("lib"))
    lib_group.add_command(lib_organize)  # 整体覆盖生成层的 lib organize preview/start
    cli.add_command(lib_group)

    agent_group = click.Group(name="agent", help=DOMAIN_HELP.get("agent"))
    agent_group.add_command(agent_run)
    agent_group.add_command(agent_attach)
    cli.add_command(agent_group)

    logs_group = click.Group(name="logs", help=DOMAIN_HELP.get("logs"))
    logs_group.add_command(logs_tail)
    cli.add_command(logs_group)

    jobs_group = click.Group(name="jobs", help=DOMAIN_HELP.get("jobs"))
    jobs_group.add_command(jobs_wait)
    cli.add_command(jobs_group)

    server = spec_loader.guess_server_for_startup(sys.argv[1:])
    spec, from_cache = spec_loader.load_active(server)
    try:
        build_tree(cli, spec)
    except Exception:
        if not from_cache:
            raise
        # 刷新缓存里的新 spec 含本版生成器不认识的形态：回退内置基线，
        # 并登记坏指纹——否则每次调用结束后偏斜检查都会重拉一遍同一份坏 spec
        bad_hash = spec_loader.active_spec_hash
        print(
            "提示：服务器接口目录较新，部分命令暂不可用，已回退内置命令目录；建议升级 CLI",
            file=sys.stderr,
        )
        spec, _ = spec_loader.load_active(None)
        build_tree(cli, spec)
        if server:
            spec_loader.mark_spec_bad(server, bad_hash)


# 装配失败（如安装包缺内置 spec）不能在 import 期裸抛 traceback——
# 记下来，进 main() 后用统一的中文错误格式报告
_ASSEMBLY_ERROR: CliError | None = None
try:
    _assemble()
except CliError as _exc:
    _ASSEMBLY_ERROR = _exc


def main() -> int:
    if _ASSEMBLY_ERROR is not None:
        print(f"错误：{_ASSEMBLY_ERROR.message}", file=sys.stderr)
        if _ASSEMBLY_ERROR.hint:
            print(f"提示：{_ASSEMBLY_ERROR.hint}", file=sys.stderr)
        return int(_ASSEMBLY_ERROR.exit_code)
    exit_code = 0
    settings: Settings | None = None
    try:
        cli(standalone_mode=False, obj=None)
    except CliError as exc:
        prefix = f"错误[{exc.code}]" if exc.code else "错误"
        print(f"{prefix}：{exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"提示：{exc.hint}", file=sys.stderr)
        if exc.details:
            print(f"详情：{exc.details}", file=sys.stderr)
        exit_code = int(exc.exit_code)
    except click.UsageError as exc:
        # 用法错误 → 退出码 2；附上 --help 指引（错误即帮助）
        exc.show()
        exit_code = 2
    except click.exceptions.Abort:
        return 130
    except click.exceptions.Exit as exc:
        exit_code = exc.exit_code
    # 版本偏斜检查：服务端 spec 指纹变了就拉新缓存（下次调用生效）。
    # 放在退出码结算之后，绝不影响本次命令的结果。
    try:
        settings = Settings()
        spec_loader.maybe_refresh(settings)
    except Exception:  # noqa: BLE001 - 刷新是尽力而为，任何失败都不能改变本次结果
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
