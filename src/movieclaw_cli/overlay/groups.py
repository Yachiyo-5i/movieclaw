"""精选层的命令组基建。

DefaultCommandGroup：带「默认子命令」的命令组——首个参数不是已知子命令时，
整串参数转交给默认子命令。用于 `mclaw search "沙丘2"`（= search torrents）与
`mclaw search history list` 在同一个组下共存。

极端情况（关键词恰好叫 history/presets 等子命令名）用显式形态兜底：
`mclaw search torrents history`。
"""

from __future__ import annotations

import click


def output_option(f):
    """尾随 `-o/--output` 输出覆盖标志（与生成命令的全局覆盖标志同语义）。

    精选命令统一挂载，参数名固定 output_override，命令内用
    `output_override or settings.output` 取值。
    """
    return click.option(
        "-o",
        "--output",
        "output_override",
        type=click.Choice(["table", "json", "yaml"]),
        default=None,
        help="输出格式（覆盖全局设置）",
    )(f)


class DefaultCommandGroup(click.Group):
    """首参不匹配任何子命令时路由到 default_command 的命令组。"""

    def __init__(self, *args, default_command: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.default_command = default_command

    def resolve_command(self, ctx: click.Context, args: list[str]):
        base = super()
        try:
            return base.resolve_command(ctx, args)
        except click.UsageError:
            command = self.commands[self.default_command]
            return self.default_command, command, args

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # 首参就是选项（如 `mclaw search --limit 5 沙丘`）时，click 组解析
        # 会报「缺少命令」——先垫上默认子命令名再交给标准解析
        if args and args[0].startswith("-") and args[0] not in ("-h", "--help"):
            args = [self.default_command, *args]
        return super().parse_args(ctx, args)
