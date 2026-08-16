"""精选命令：mclaw jobs wait——持续等待一个持久化任务到终态。"""

from __future__ import annotations

import click

from movieclaw_cli.core.output import emit
from movieclaw_cli.gen.tree_builder import wait_persistent_job
from movieclaw_cli.overlay.groups import output_option


@click.command(name="wait", short_help="等待后台任务完成（断点状态由服务端持久化）")
@click.argument("job_id")
@click.option(
    "--wait-timeout",
    type=float,
    default=3600.0,
    show_default=True,
    help="最长等待秒数；超时只停止等待，不取消任务",
)
@output_option
@click.pass_obj
def jobs_wait(settings, job_id: str, wait_timeout: float, output_override: str | None) -> None:
    """等待任务到成功、失败、取消或需要人工处理；Ctrl-C 不会取消任务。"""
    api = settings.make_api()
    try:
        wait_persistent_job(api, {"id": job_id}, {"id_path": "id"}, wait_timeout)
        final = api.request("GET", f"/jobs/{job_id}")
        emit(final, output=output_override or settings.output, quiet=settings.quiet)
    finally:
        api.close()
