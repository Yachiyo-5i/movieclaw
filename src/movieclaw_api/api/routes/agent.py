from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_agent import AgentRunner, AgentStartParams, AgentTool, build_system_prompt, compact
from movieclaw_agent.tools import builtin_tools, make_mclaw_tool
from movieclaw_api.api.deps import require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import (
    BadRequestException,
    NotFoundException,
    UpstreamServiceException,
)
from movieclaw_api.schemas.agent import (
    AgentCompactView,
    AgentSessionDetailView,
    AgentSessionListItem,
    AgentSessionRenamePayload,
    AgentSessionTruncatePayload,
    AgentStartPayload,
    AgentStartView,
    AgentTruncateView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.agent_runs import get_agent_run_registry
from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
from movieclaw_api.services.agent_sessions import (
    CompactionEntry,
    SessionEntry,
    get_agent_session_store,
)
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.llm_config import acquire_llm_router
from movieclaw_api.services.mclaw_tool import render_service_map
from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.agent_session_repo import (
    AgentSessionRepository,
    is_running,
)
from movieclaw_llm import ChatMessage, ModelSettings

router = APIRouter(prefix="/agent", tags=["agent"])


def get_agent_tools(cli_env: dict[str, str]) -> list[AgentTool]:
    """Agent 的工具集：内置基础工具 + mclaw 产品操作工具。

    bash/read/write/edit 是纯工作区工具，**不携带**产品授权；mclaw 工具
    单独构建，令牌只注入它的子进程（每次运行的令牌不同，因此每次运行都
    重新构建工具集）。服务目录渲染自 CLI 内置 spec，与命令面严格同版。
    """
    workdir = Path(get_settings().agent_workspace_dir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    return [
        *builtin_tools(workdir),
        make_mclaw_tool(workdir, cli_env, render_service_map()),
    ]


# 前端页面路由表：模型拼可点击链接的唯一依据（(路径模式, 一行说明)）。
# 与 apps/web/app/(app) 的真实页面结构由守护测试保持同步（见
# tests/api/test_agent.py 的 test_page_routes_match_web_app_pages）——
# 前端加页面、改路径时测试会拦下，这张表不会悄悄过期。
_PAGE_ROUTES: list[tuple[str, str]] = [
    ("/", "首页"),
    ("/search", "站点资源搜索页"),
    ("/discover/{movie|tv}", "发现页（电影/剧集的榜单与分类浏览）"),
    ("/discover/movie/top250", "豆瓣电影 Top250 榜单"),
    ("/discover/movie/high-score", "豆瓣高分电影榜单"),
    ("/media/{movie|tv}/{TMDB ID}", "影片/剧集详情页"),
    ("/media/douban/{豆瓣ID}", "豆瓣词条详情页"),
    ("/subscriptions", "订阅列表"),
    ("/subscriptions/{订阅ID}", "订阅详情（ID 来自 sub list/show）"),
    ("/library", "媒体库总览"),
    ("/library/{库ID}", "某个媒体库的内容（库 ID 来自 lib list）"),
    ("/library/{库ID}/item/{条目ID}", "库内条目详情（条目 ID 即 lib items 的 media_item_id）"),
    ("/tasks", "任务中心（后台作业、下载与入库的统一观察页）"),
    ("/people/{影人ID}", "影人档案（ID 来自 people 域）"),
    ("/settings", "设置页"),
]


async def _agent_system_prompt() -> str:
    """组装本次运行的系统提示词：通用正文 + 部署环境事实。

    日志目录是 API 层的配置（LOG_DIR），按 prompts.build_system_prompt 的
    设计走 extra_environment 传入——mclaw 已对 Agent 隐藏 logs 域（见
    services/mclaw_tool 的 _EXCLUDED_DOMAINS），排查问题靠这里给出的
    路径用 bash 直接检索。

    外部访问地址来自「设置 → 应用设置」（保存即生效），因此每次运行时
    现读设置存储，不做缓存；未配置时整块不输出——没有前缀，路由表也
    拼不出有效链接，避免模型给用户无效地址。
    """
    log_dir = Path(get_settings().log_dir).resolve()
    lines = [
        f"- 运行日志目录：{log_dir}，按天一个文件（movieclaw-YYYY-MM-DD.log）。"
        "排查问题时用 bash 的 grep/tail 直接检索日志。"
    ]
    external_url = (await get_setting_store().get(AppServerSetting)).external_url
    if external_url:
        routes = "\n".join(f"  {pattern}  {desc}" for pattern, desc in _PAGE_ROUTES)
        lines.append(
            f"- 本应用的外部访问地址：{external_url}。提到订阅、影片、库内条目等页面时，"
            "尽量附上可点击的 Markdown 链接：以外部访问地址为前缀，路径按下表拼接。"
            f"只拼表内路径，不要用容器内地址拼链接。\n{routes}"
        )
    return build_system_prompt("\n".join(lines))


async def _cli_env(session_id: str) -> dict[str, str]:
    """构造工作区里 mclaw CLI 的自动授权环境（docs/design/cli.md §6.2）。

    令牌按运行签发、短时效、无状态签名（改密轮换密钥即全体失效）；
    服务器地址走容器内回环直连。Agent 在工作区内执行 `mclaw ...` 即可
    直接操作本产品，全程零登录零交互。
    """
    settings = get_settings()
    token = await auth_service.issue_agent_token(session_id)
    return {
        "MOVIECLAW_SERVER": f"http://127.0.0.1:{settings.port}",
        "MOVIECLAW_TOKEN": token,
    }


@router.post(
    "/start",
    response_model=ApiResponse[AgentStartView],
    status_code=202,
    summary="创建一次异步 Agent 运行",
    operation_id="agent.start",
)
async def start_agent(
    payload: AgentStartPayload,
    identity: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[AgentStartView]:
    """创建后台运行并立即返回编号，执行生命周期不再绑定当前 HTTP 连接。

    会话持久化：每次运行都归属一个服务端会话（新建或续聊）。用户输入先落
    转录文件，运行过程中的定稿消息经 recorder 持续追加；续聊时 LLM 上下文
    从转录重建，前端无需再回传历史。

    路由器在任何会话记录落盘之前组装：读配置、解密 Key 依赖请求级 session；
    组装完成后 runner 只持有进程级 LlmRouter、工具集和纯数据参数，后台执行不
    再访问该 session。尚未配置模型供应商时同步返回 404，且因校验前置，不会残
    留任何空会话记录，便于前端引导用户去设置。
    """
    # 递归硬闸（docs/design/agent-cli-integration.md §4）：Agent 工作区令牌
    # 不允许再发起新的 Agent 运行——工具层已有软闸，这里是绕过工具（curl 等）
    # 也拦得住的最后防线。放在一切校验/落盘之前。
    if identity.kind == "agent":
        raise BadRequestException("Agent 工作区内不能再发起新的 Agent 运行（禁止递归）")

    store = get_agent_session_store()
    repo = AgentSessionRepository(session)

    # 先组装路由器：内部会校验模型供应商是否已配置，未配置时抛 404。必须在任何
    # 会话记录落盘（转录文件 / 索引行）之前完成——否则校验失败时，前端虽然收到
    # 正确的错误提示，磁盘上却已残留一条空会话，下次刷新侧栏会冒出来。
    llm_router = await acquire_llm_router(session)

    if payload.session_id:
        row = await repo.get(payload.session_id)
        if row is None:
            raise NotFoundException("Agent 会话不存在")
        if is_running(row):
            raise BadRequestException("该会话已有正在进行的运行，请先停止或等待完成")
        # 续聊：LLM 上下文从转录文件重建（事实源），忽略前端回传的 history
        history = store.build_history(payload.session_id)
        session_id = payload.session_id
        # 一条 entry 对应一条消息，重建出的历史长度就是文件当前的 entry 数
        entry_count = len(history)
    else:
        header = store.create()
        session_id = header.session_id
        await repo.create(session_id, title=None)
        # 过渡期兼容：老前端在新会话上仍可能带本地历史，只用于本次上下文，
        # 不写入转录（新文件从 0 条 entry 起步）
        history = [ChatMessage(role=m.role, content=m.content) for m in payload.history]
        entry_count = 0

    recorder = AgentSessionRecorder(store, session_id, entry_count=entry_count)
    entry_uuid = await recorder.record_user_input(payload.input)

    runner = AgentRunner(
        llm_router,
        tools=get_agent_tools(await _cli_env(session_id)),
        on_message=recorder.on_message,
        on_compaction=recorder.on_compaction,
    )
    params = AgentStartParams(
        input=payload.input,
        history=history,
        model=payload.model,
        system_prompt=await _agent_system_prompt(),
    )
    run_id = get_agent_run_registry().start(runner, params, on_terminal=recorder.on_terminal)
    await recorder.begin(run_id)
    return ok(
        AgentStartView(run_id=run_id, session_id=session_id, entry_uuid=entry_uuid),
        message="Agent 运行已创建",
    )


@router.get(
    "/sessions",
    response_model=ApiResponse[list[AgentSessionListItem]],
    summary="最近会话列表（按最后活跃时间倒序）",
    operation_id="agent.sessions.list",
)
async def list_agent_sessions(
    limit: int = Query(default=50, description="返回条数上限"),
    offset: int = Query(default=0, description="分页偏移（跳过前 N 条）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[AgentSessionListItem]]:
    """从索引表分页读取；运行状态由 active_run_id + 心跳窗派生，
    running 的条目附带 active_run_id 供调用方重新订阅事件流。"""
    rows = await AgentSessionRepository(session).list_recent(
        limit=min(limit, 200), offset=max(offset, 0)
    )
    return ok([AgentSessionListItem.from_model(row) for row in rows])


@router.get(
    "/sessions/{session_id}",
    response_model=ApiResponse[AgentSessionDetailView],
    summary="会话详情（完整消息回放）",
    operation_id="agent.sessions.show",
)
async def get_agent_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[AgentSessionDetailView]:
    """entries 为转录文件的原样投影，渲染约定见 AgentSessionDetailView。"""
    row = await AgentSessionRepository(session).get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    _, entries = get_agent_session_store().read(session_id)
    return ok(
        AgentSessionDetailView(
            session=AgentSessionListItem.from_model(row),
            entries=[_entry_view(e) for e in entries],
        )
    )


def _entry_view(entry: SessionEntry | CompactionEntry) -> dict:
    """entry → 详情接口的渲染投影。

    压缩行不含 replacement_history：那是 resume 重建数据（可达几十 KB），
    渲染只需要摘要与前后 token 数。
    """
    if isinstance(entry, CompactionEntry):
        return entry.model_dump(exclude_none=True, exclude={"replacement_history"})
    return entry.model_dump(exclude_none=True)


@router.patch(
    "/sessions/{session_id}",
    response_model=ApiResponse[AgentSessionListItem],
    summary="重命名会话",
    operation_id="agent.sessions.rename",
)
async def rename_agent_session(
    session_id: str,
    payload: AgentSessionRenamePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[AgentSessionListItem]:
    """标题只写索引表（转录文件 append-only，不存可变元数据）。"""
    row = await AgentSessionRepository(session).rename(session_id, payload.title)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    return ok(AgentSessionListItem.from_model(row), message="会话已重命名")


@router.post(
    "/sessions/{session_id}/compact",
    response_model=ApiResponse[AgentCompactView],
    summary="手动压缩会话上下文",
    operation_id="agent.sessions.compact",
)
async def compact_agent_session(
    session_id: str,
    identity: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[AgentCompactView]:
    """把会话历史压缩成「保留的用户原话 + 交接摘要」，压缩行写入转录。

    与运行中的自动压缩共用同一套领域逻辑（movieclaw_agent.compact），只是
    触发与持久化路径不同：这里同步执行、直接落盘。会话正在运行时拒绝——
    运行内自有 mid-run 自动压缩，双方并发写转录会破坏链尾一致性。
    模型沿用会话最近一次使用的模型（无记录时走默认路由）。
    """
    store = get_agent_session_store()
    repo = AgentSessionRepository(session)
    row = await repo.get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(row):
        raise BadRequestException("该会话正在运行中，请等待完成后再压缩")

    llm_router = await acquire_llm_router(session)
    history = store.build_history(session_id)
    if not history:
        raise BadRequestException("会话没有可压缩的内容")

    # 沿用会话最近一次运行的模型（转录里 assistant 行带 model 元数据）
    _, entries = store.read(session_id)
    model = next(
        (e.model for e in reversed(entries) if isinstance(e, SessionEntry) and e.model),
        "",
    )

    messages = [ChatMessage(role="system", content=await _agent_system_prompt()), *history]
    result = await compact(llm_router, model, messages, ModelSettings())
    if result is None:
        raise UpstreamServiceException("压缩失败：模型未能生成摘要，请稍后重试")

    entry = store.append_compaction(session_id, result)
    await repo.touch_after_append(
        session_id,
        leaf_uuid=entry.uuid,
        entry_count=len(entries) + 1,
    )
    return ok(
        AgentCompactView(
            summary=result.summary,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            entry_uuid=entry.uuid,
        ),
        message="会话上下文已压缩",
    )


@router.post(
    "/sessions/{session_id}/truncate",
    response_model=ApiResponse[AgentTruncateView],
    summary="从某条提问处截断会话（该轮及其后的记录全部丢弃）",
    operation_id="agent.sessions.truncate",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def truncate_agent_session(
    session_id: str,
    payload: AgentSessionTruncatePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[AgentTruncateView]:
    """「改写某轮重新提问」的服务端动作：把这一轮连同其后的往返从转录里删除。

    调用方（会话页）随后把原提问填回输入框，用户改完再发一次——新的一轮就
    接在被截断处，重建出的 LLM 上下文里不再有被丢弃的内容。**不可逆**：转录
    是事实源，删掉即无从恢复，前端必须先做二次确认。

    运行中拒绝：正在写转录的运行与整文件重写并发，会把链尾写乱。
    """
    store = get_agent_session_store()
    repo = AgentSessionRepository(session)
    row = await repo.get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(row):
        raise BadRequestException("该会话正在运行中，请先停止运行再改写")

    removed = store.truncate_from(session_id, payload.entry_uuid)
    # 按截断后的文件重新校准索引：条数、链尾、列表副标题都变了
    summary = store.summarize(session_id)
    await repo.resync_after_truncate(
        session_id,
        leaf_uuid=summary.leaf_uuid,
        entry_count=summary.entry_count,
        last_prompt=summary.last_prompt,
    )
    return ok(
        AgentTruncateView(removed_entries=removed, entry_count=summary.entry_count),
        message="会话已截断",
    )


@router.delete(
    "/sessions/{session_id}",
    response_model=ApiResponse[dict],
    summary="删除会话（转录文件与索引一并删除）",
    operation_id="agent.sessions.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_agent_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """有正在进行的运行时拒绝删除（先取消运行）；按「先文件后索引」的
    逆序执行——先删文件再删行，即使中途失败也不会出现幽灵会话。"""
    repo = AgentSessionRepository(session)
    row = await repo.get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(row):
        raise BadRequestException("该会话正在运行中，请先停止运行再删除")
    get_agent_session_store().delete(session_id)
    await repo.delete(session_id)
    return ok({}, message="会话已删除")


@router.get(
    "/runs/{run_id}/stream",
    summary="订阅 Agent 运行事件（SSE，支持断线续传）",
    operation_id="agent.runs.stream",
    openapi_extra={
        "x-cli-stream": {"terminal_events": ["agent_done", "agent_error", "agent_cancelled"]},
    },
)
async def stream_agent_run(
    run_id: str,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """先回放游标后的历史，再实时推送新事件，直到运行进入终态。

    SSE ``id`` 是运行内从 1 开始的递增序号。客户端首次订阅不传
    ``Last-Event-ID`` 即可回放全部；重连时传最后已处理的 id，服务端只发送
    缺失事件。心跳使用 SSE 注释，不进入事件日志，也不推进游标。
    """
    registry = get_agent_run_registry()
    cursor = last_event_id or 0
    # 在 StreamingResponse 建立前完成存在性和游标校验，确保 404/400 仍能以
    # 标准 JSON 错误返回，而不是已经发出 200 后才在生成器里异常断流。
    initial_events, initial_terminal = await registry.get_events(
        run_id,
        cursor,
        timeout_seconds=0,
    )

    async def event_source():
        nonlocal cursor
        events = initial_events
        terminal = initial_terminal
        while True:
            for stored in events:
                cursor = stored.sequence
                event = stored.event
                yield (
                    f"id: {stored.sequence}\n"
                    f"event: {event.type}\n"
                    f"data: {event.model_dump_json(exclude_none=True)}\n\n"
                )
            if terminal:
                return
            events, terminal = await registry.get_events(
                run_id,
                cursor,
                timeout_seconds=15,
            )
            if not events and not terminal:
                yield ": heartbeat\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            # SSE 反缓冲三件套（原理见 routes/search.py 的流式端点）
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=ApiResponse[dict],
    summary="取消一次 Agent 运行",
    operation_id="agent.runs.cancel",
)
async def cancel_agent_run(run_id: str) -> ApiResponse[dict]:
    """幂等请求取消后台任务；运行的 SSE 会以 agent_cancelled 事件收尾。"""
    await get_agent_run_registry().cancel(run_id)
    return ok({}, message="已请求停止 Agent 运行")
