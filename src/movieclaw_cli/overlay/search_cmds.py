"""精选命令：统一搜索入口与 mclaw download（衔接种子快照）。

``search titles / torrents / library-items`` 与公开 API 的能力名一一对应。
其中 torrents 背后是流式协议 + 客户端筛选排序 + 本地快照三件事：
- 内部走 /search/torrents/stream（快的站点先出），站点进度打 stderr；
- stdout 输出**聚合完成后的稳定结果**（Agent 心智：一次调用一个完整结果），
  每行带稳定行号；
- 结果落本地快照，`mclaw download <行号>` 直接引用，免去复制长 URL。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.output import emit
from movieclaw_cli.overlay.groups import output_option

_SORT_KEYS = {
    "seeders": lambda r: r.get("seeders") or 0,
    "size": lambda r: r.get("size_bytes") or 0,
    "snatched": lambda r: r.get("snatched") or 0,
}


def _snapshot_path() -> Any:
    return cfg.config_dir() / "last-search.json"


def save_snapshot(server: str, keyword: str, items: list[dict]) -> None:
    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"server": server, "keyword": keyword, "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_snapshot() -> dict | None:
    path = _snapshot_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _row_view(index: int, hit: dict) -> dict:
    attrs = hit.get("attrs") or {}
    return {
        "row": index,
        "title": hit.get("title"),
        "size": hit.get("size"),
        "seeders": hit.get("seeders"),
        "resolution": attrs.get("resolution"),
        "group": attrs.get("release_group"),
        "site": hit.get("site_name"),
        "free": hit.get("free"),
    }


def _torrent_identity(hit: dict) -> tuple[str, str, int] | None:
    """按 Web 下载弹窗的同一门槛，从搜索结果提取智能入库身份三件套。"""
    attrs = hit.get("attrs") or {}
    titles = (attrs.get("titles_zh") or []) + (attrs.get("titles_en") or [])
    kind = attrs.get("media_type")
    title = titles[0] if titles else None
    year = attrs.get("year")
    if (
        kind not in ("movie", "tv")
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(year, int)
        or not 1888 <= year <= 2100
    ):
        return None
    return kind, title.strip(), year


def _auto_route_body(
    api,
    hit: dict,
    *,
    row: int,
    selected_tmdb_id: int | None,
    output: str | None,
) -> dict[str, Any]:
    """复用 Web 的「识别预检 → 确认身份」协议，返回真实提交所需字段。

    CLI 面向脚本和 Agent，不能像弹窗一样停下来等点击。因此唯一身份自动
    继续；歧义把结构化候选写到 stdout 并以退出码 7 停止，调用方可带
    ``--tmdb-id`` 重试。任何不确定或不可入库状态都不会静默落下载器默认目录。
    """
    identity = _torrent_identity(hit)
    if identity is None:
        raise CliError(
            "搜索结果缺少可靠的媒体类型、片名或年份，未提交下载",
            hint=(
                "可指定 --library <id> 或 --save-path <目录>；"
                "确认无需自动入库时显式加 --downloader-default"
            ),
        )

    kind, title, year = identity
    resolve_body: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "year": year,
        "subtitle": hit.get("subtitle") or None,
    }
    if selected_tmdb_id is not None:
        resolve_body["selected_tmdb_id"] = selected_tmdb_id
    target = api.request("POST", "/downloaders/resolve-target", json_body=resolve_body) or {}
    status = target.get("status")
    if status == "ambiguous":
        # 歧义是机器可恢复状态：stdout 保持结构化，stderr 只写下一步。
        emit(
            {"status": status, "candidates": target.get("candidates") or []},
            output=output,
        )
        raise CliError(
            "识别到多个可能的影视条目，未提交下载",
            exit_code=ExitCode.AMBIGUOUS,
            hint=f"确认候选后重试：mclaw download {row} --tmdb-id <候选 tmdb_id>",
        )
    if status != "ready" or target.get("tmdb_id") is None:
        raise CliError(
            "未能可靠识别该资源，未提交下载",
            hint=(
                "可指定 --library <id> 或 --save-path <目录>；"
                "确认无需自动入库时显式加 --downloader-default"
            ),
        )
    if not target.get("ok") or target.get("library_id") is None:
        raise CliError(
            target.get("warning") or "已识别资源，但当前配置不能完成自动入库",
            hint=(
                "请按提示修复媒体库、监听目录或路径映射；也可显式指定 "
                "--library/--save-path，或用 --downloader-default 跳过自动入库"
            ),
        )

    route = target.get("route_reason") or f"入库到「{target.get('library_name')}」"
    path = target.get("path")
    print(f"智能入库：{route}" + (f"；投递目录 {path}" if path else ""), file=sys.stderr)
    return {
        "auto_route": True,
        "media_kind": kind,
        "tmdb_id": target["tmdb_id"],
        "title": title,
        "year": year,
        "subtitle": hit.get("subtitle") or None,
    }


@click.command(name="titles", short_help="按片名搜索 TMDB、豆瓣或全部影视来源")
@click.argument("query")
@click.option(
    "--provider",
    type=click.Choice(["all", "tmdb", "douban"]),
    default="all",
    show_default=True,
    help="影视数据来源",
)
@click.option("--incognito", is_flag=True, help="无痕搜索：不写入服务端搜索历史")
@output_option
@click.pass_obj
def search_titles(
    settings,
    output_override: str | None,
    query: str,
    provider: str,
    incognito: bool,
):
    """搜索影视条目。

    示例：

        mclaw search titles "沙丘" --provider all

    TMDB 与豆瓣单边失败时仍会返回另一边结果；默认记录搜索历史。
    """
    api = settings.make_api()
    try:
        result = (
            api.request(
                "POST",
                "/search/titles",
                json_body={
                    "query": query,
                    "provider": provider,
                    "save_history": not incognito,
                },
            )
            or {}
        )
        for status in result.get("providers") or []:
            if not status.get("success"):
                print(
                    f"{status.get('provider')} 搜索失败：{status.get('message') or '未知错误'}",
                    file=sys.stderr,
                )
        emit(
            result.get("titles") or [],
            output=output_override or settings.output,
            quiet=settings.quiet,
        )
    finally:
        api.close()


@click.command(name="library-items", short_help="搜索全部可见媒体库中的已入库条目")
@click.argument("keyword")
@output_option
@click.pass_obj
def search_library_items(settings, output_override: str | None, keyword: str):
    """按标题或原名搜索本地媒体库。

    示例：

        mclaw search library-items "沙丘"
    """
    api = settings.make_api()
    try:
        result = api.request("GET", "/search/library-items", params={"keyword": keyword}) or []
        emit(result, output=output_override or settings.output, quiet=settings.quiet)
    finally:
        api.close()


@click.command(name="torrents", short_help="跨 PT 站点搜索种子（结果行号可直接下载）")
@click.argument("keyword")
@click.option("--category", "categories", multiple=True, help="分类过滤（可多次指定）")
@click.option("--site", "sites", multiple=True, help="限定站点（可多次指定，站点 id）")
@click.option("--page", type=int, default=1, show_default=True, help="页码（各站点独立分页）")
@click.option(
    "--sort",
    type=click.Choice(sorted(_SORT_KEYS)),
    default="seeders",
    show_default=True,
    help="排序字段（降序）",
)
@click.option("--resolution", help="按分辨率过滤（如 2160p / 1080p）")
@click.option("--free-only", is_flag=True, help="只要免费种")
@click.option("--limit", type=int, default=50, show_default=True, help="输出条数上限")
@click.option("--incognito", is_flag=True, help="无痕搜索：不写入服务端搜索历史")
@click.option("--stream-events", is_flag=True, help="逐事件输出 NDJSON（增量消费方用；不落快照）")
@click.option(
    "-o",
    "--output",
    "output_override",
    type=click.Choice(["table", "json", "yaml"]),
    default=None,
    help="输出格式（覆盖全局设置）",
)
@click.pass_obj
def search_torrents(
    settings,
    keyword: str,
    categories: tuple[str, ...],
    sites: tuple[str, ...],
    page: int,
    sort: str,
    resolution: str | None,
    free_only: bool,
    limit: int,
    incognito: bool,
    stream_events: bool,
    output_override: str | None,
):
    """跨站点搜索种子。

    快的站点先出结果（进度在 stderr），全部站点返回后输出按 --sort 排序的
    稳定结果；每行的 row 行号可直接用于下载：

        mclaw search torrents "沙丘2" --resolution 2160p

        mclaw download 3          # 下载上面结果里的第 3 行
    """
    params: dict[str, Any] = {"keyword": keyword, "page": page}
    if categories:
        params["categories"] = list(categories)
    if sites:
        params["sites"] = list(sites)
    if incognito:
        params["no_history"] = True

    api = settings.make_api()
    collected: list[dict] = []
    site_status: list[dict] = []
    saw_done = False
    try:
        for event in api.stream_sse("/search/torrents/stream", params=params):
            payload = json.loads(event.data) if event.data else {}
            if stream_events:
                print(json.dumps({"event": event.event, **payload}, ensure_ascii=False))
                continue
            if event.event == "start":
                total_sites = len(payload.get("sites") or [])
                print(f"开始搜索「{keyword}」，共 {total_sites} 个站点", file=sys.stderr)
            elif event.event == "site_result":
                collected.extend(payload.get("items") or [])
                print(
                    f"  {payload.get('site_name')}：{payload.get('count')} 条"
                    f"（{payload.get('elapsed_ms')}ms）",
                    file=sys.stderr,
                )
            elif event.event == "site_error":
                site_status.append(payload)
                print(
                    f"  {payload.get('site_name')}：失败——{payload.get('error')}",
                    file=sys.stderr,
                )
            elif event.event == "done":
                saw_done = True
                print(
                    f"完成：共 {payload.get('total')} 条（{payload.get('elapsed_ms')}ms）",
                    file=sys.stderr,
                )
    finally:
        api.close()
    if not saw_done:
        # 流在 done 之前闭合：结果不完整，绝不能当完整结果输出/落快照
        raise CliError(
            f"搜索流提前中断，结果不完整（仅收到 {len(collected)} 条，未落快照）",
            exit_code=ExitCode.NETWORK,
            hint="网络可能不稳或服务已重启，请重试 mclaw search torrents",
        )
    if stream_events:
        return

    # 客户端侧筛选/排序：与页面筛选弹层同语义，但发生在完整结果集上
    rows = collected
    if resolution:
        rows = [
            r
            for r in rows
            if ((r.get("attrs") or {}).get("resolution") or "").lower() == resolution.lower()
        ]
    if free_only:
        rows = [r for r in rows if r.get("free")]
    rows.sort(key=_SORT_KEYS[sort], reverse=True)
    total = len(rows)
    if total > limit:
        print(f"共 {total} 条，已截断到前 {limit} 条（--limit 可调）", file=sys.stderr)
        rows = rows[:limit]

    # 快照供 mclaw download <行号> 引用（行号 = 截断排序后的展示顺序）
    save_snapshot(api.server, keyword, rows)
    emit(
        [_row_view(i + 1, r) for i, r in enumerate(rows)],
        output=output_override or settings.output,
        quiet=settings.quiet,
    )
    if rows:
        print("下载某一行：mclaw download <row>", file=sys.stderr)


@click.command(name="download", short_help="下载种子（用搜索结果行号，或显式给站点+链接）")
@click.argument("row", type=int, required=False)
@click.option("--site-id", help="显式形态：种子所属站点 id（与 --url 搭配，替代行号）")
@click.option("--url", help="显式形态：种子下载入口 download_url")
@click.option("--library", "library_id", type=int, help="显式指定入库目标库 id，跳过智能选库")
@click.option("--save-path", help="显式指定保存目录（movieclaw 视角），跳过智能入库")
@click.option("--downloader-default", is_flag=True, help="跳过智能入库，使用下载器默认目录")
@click.option(
    "--tmdb-id",
    type=click.IntRange(min=1),
    help="自动识别有歧义时，指定候选 TMDB ID 后重试",
)
@output_option
@click.pass_obj
def download(
    settings,
    output_override: str | None,
    row: int | None,
    site_id: str | None,
    url: str | None,
    library_id: int | None,
    save_path: str | None,
    downloader_default: bool,
    tmdb_id: int | None,
):
    """把一条种子提交到默认下载器。

    两种形态：

        mclaw download 3                          # 上次 mclaw search 结果的第 3 行

        mclaw download --site-id mteam --url ...  # 显式指定（脚本/跨会话）

    行号形态默认与 Web 下载弹窗一样，先识别 TMDB 身份并预演路由；只有
    身份唯一且路由可用才提交。歧义时用 --tmdb-id 确认候选后重试。

    --library / --save-path 可显式覆盖自动识别；确实只想交给下载器自身决定
    保存位置时，使用 --downloader-default。
    """
    api = settings.make_api()
    try:
        if row is not None and (site_id is not None or url is not None):
            raise click.UsageError("行号不能与 --site-id/--url 同时使用")
        if row is None and bool(site_id) != bool(url):
            raise click.UsageError("--site-id 与 --url 必须同时提供")
        explicit_targets = sum((library_id is not None, save_path is not None, downloader_default))
        if explicit_targets > 1:
            raise click.UsageError("--library、--save-path 与 --downloader-default 只能选择一个")
        if tmdb_id is not None and explicit_targets:
            raise click.UsageError("--tmdb-id 不能与显式保存目标同时使用")

        body: dict[str, Any]
        hit: dict | None = None
        if row is not None:
            snapshot = load_snapshot()
            if snapshot is None:
                raise CliError(
                    "没有可用的搜索快照",
                    exit_code=ExitCode.USAGE,
                    hint="先执行 mclaw search <关键词>，或改用 --site-id + --url 显式指定",
                )
            if snapshot.get("server") and snapshot["server"] != api.server:
                raise CliError(
                    f"搜索快照来自另一台服务器（{snapshot['server']}），不能提交到 {api.server}",
                    exit_code=ExitCode.USAGE,
                    hint="在当前服务器重新执行 mclaw search 后再用行号下载",
                )
            items = snapshot.get("items") or []
            if not 1 <= row <= len(items):
                keyword = snapshot.get("keyword")
                raise CliError(
                    f"行号超出范围：{row}（上次搜索「{keyword}」共 {len(items)} 条）",
                    exit_code=ExitCode.USAGE,
                    hint="行号以 mclaw search 输出的 row 列为准",
                )
            hit = items[row - 1]
            attrs = hit.get("attrs") or {}
            parsed_titles = (attrs.get("titles_zh") or []) + (attrs.get("titles_en") or [])
            body = {
                "site_id": hit.get("site_id"),
                "download_url": hit.get("download_url"),
                "title": parsed_titles[0] if parsed_titles else None,
                "year": attrs.get("year"),
                "subtitle": hit.get("subtitle") or None,
            }
            print(f"下载：{hit.get('title')}（{hit.get('site_name')}）", file=sys.stderr)
        elif site_id and url:
            body = {"site_id": site_id, "download_url": url}
        else:
            raise click.UsageError("请给出搜索结果行号，或同时提供 --site-id 与 --url")

        if tmdb_id is not None and row is None:
            raise click.UsageError("--tmdb-id 只能用于搜索结果行号下载")

        # Web 用弹窗显式选择；CLI/Agent 没有中途交互，因此行号下载默认走
        # 智能选项，只有显式保存目标才跳过预检。
        use_auto_route = row is not None and explicit_targets == 0
        if use_auto_route:
            assert hit is not None and row is not None
            body.update(
                _auto_route_body(
                    api,
                    hit,
                    row=row,
                    selected_tmdb_id=tmdb_id,
                    output=output_override or settings.output,
                )
            )
        elif library_id is not None:
            body["library_id"] = library_id
        elif save_path is not None:
            body["save_path"] = save_path

        data = api.request("POST", "/downloaders/submit", json_body=body)
        if api.last_message:
            print(api.last_message, file=sys.stderr)
    finally:
        api.close()
    emit(data, output=output_override or settings.output, quiet=settings.quiet)
