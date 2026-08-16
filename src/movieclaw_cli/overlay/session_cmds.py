"""精选命令：AI 会话的开始、继续、指定消息重试与实时跟随。

公开交互始终以 session_id 为主键；后台 run_id 仅用于服务端任务调度，
不会出现在命令参数或输出中。SSE 支持 Last-Event-ID 断点续传，终态事件
决定退出码（agent_error → 1）。
"""

from __future__ import annotations

import json
import sys
import time

import click

from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.http import Api
from movieclaw_cli.core.output import emit
from movieclaw_cli.overlay.groups import output_option

_TERMINAL_EVENTS = {"agent_done", "agent_error", "agent_cancelled"}
_MAX_RECONNECTS = 5


def _render_event(event: str, payload: dict) -> None:
    """把一帧事件画到终端：正文进 stdout，过程信息进 stderr。"""
    if event == "agent_start":
        print(
            f"[会话开始] {payload.get('provider')}/{payload.get('model')}",
            file=sys.stderr,
        )
    elif event == "text_delta":
        print(payload.get("delta") or "", end="", flush=True)
    elif event == "tool_call":
        call = payload.get("tool_call") or {}
        args = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
        if len(args) > 120:
            args = args[:117] + "..."
        print(f"\n→ 工具 {call.get('name')}：{args}", file=sys.stderr)
    elif event == "tool_result":
        result = payload.get("tool_result") or {}
        mark = "✗" if result.get("is_error") else "✓"
        print(f"  {mark} 完成（{result.get('elapsed_ms')}ms）", file=sys.stderr)
    elif event == "context_compacted":
        compaction = payload.get("compaction") or {}
        print(
            f"\n[上下文已压缩] {compaction.get('tokens_before')} → "
            f"{compaction.get('tokens_after')} tokens",
            file=sys.stderr,
        )


def _stream_session(api: Api, session_id: str, from_id: int | None) -> tuple[str, dict]:
    """消费当前用户消息触发的事件流直到终态；断流自动续传。"""
    cursor = from_id
    failures = 0
    while True:
        try:
            got_event = False
            for sse in api.stream_sse(
                f"/sessions/{session_id}/events",
                last_event_id=cursor,
            ):
                got_event = True
                failures = 0
                if sse.id is not None:
                    cursor = sse.id
                payload = json.loads(sse.data) if sse.data else {}
                if sse.event in _TERMINAL_EVENTS:
                    return sse.event, payload
                _render_event(sse.event, payload)
            raise CliError("事件流在终态前闭合", exit_code=ExitCode.NETWORK, details=got_event)
        except CliError as exc:
            if exc.exit_code != ExitCode.NETWORK:
                raise
            failures += 1
            if failures > _MAX_RECONNECTS:
                raise CliError(
                    f"事件流连续断开 {failures} 次，停止重连（会话可能仍在执行）",
                    hint=f"稍后用 mclaw session follow {session_id} 接回，"
                    f"或 mclaw session get-transcript {session_id} 查看完整轨迹",
                ) from None
            delay = min(0.5 * (2 ** (failures - 1)), 5.0)
            print(f"（连接断开，{delay:.1f}s 后续传…）", file=sys.stderr)
            time.sleep(delay)


def _finish(event: str, payload: dict) -> None:
    """终态渲染与退出码结算。"""
    if event == "agent_done":
        result = payload.get("result") or {}
        print("", flush=True)
        usage = result.get("usage") or {}
        print(
            f"[完成] {result.get('steps')} 步，{result.get('elapsed_ms')}ms，"
            f"tokens in/out={usage.get('input_tokens')}/{usage.get('output_tokens')}",
            file=sys.stderr,
        )
        return
    if event == "agent_cancelled":
        print("\n[已停止] 当前消息的模型处理已停止", file=sys.stderr)
        return
    raise CliError(
        payload.get("error") or "AI 会话执行失败",
        hint="完整过程可用 mclaw session get-transcript <session_id> 回看",
    )


def _submit_and_follow(
    settings,
    output_override: str | None,
    *,
    path: str,
    body: dict,
    detach: bool,
) -> None:
    """提交会启动模型处理的会话动作，并按需跟随其事件流。"""
    api = settings.make_api()
    try:
        started = api.request("POST", path, json_body=body) or {}
        actual_session_id = started.get("session_id")
        print(
            f"消息已提交：session_id={actual_session_id} "
            f"message_id={started.get('message_id')}",
            file=sys.stderr,
        )
        if detach:
            emit(started, output=output_override or settings.output, quiet=settings.quiet)
            return
        event, payload = _stream_session(api, actual_session_id, from_id=None)
    finally:
        api.close()
    _finish(event, payload)


def _submission_options(function):
    """提交消息类动作共用的模型、后台执行与输出选项。"""
    function = click.option("--model", help="模型 ID（缺省用默认供应商的默认模型）")(
        function
    )
    function = click.option(
        "--detach",
        is_flag=True,
        help="提交后立即返回会话与消息编号，不等待模型完成",
    )(function)
    return output_option(function)


@click.command(name="start", short_help="开始新会话或继续已有会话，并实时显示执行过程")
@click.argument("prompt")
@click.option("--session-id", help="已有会话编号（从 session list 获取）；给出时继续该会话")
@_submission_options
@click.pass_obj
def session_start(
    settings,
    output_override,
    prompt: str,
    session_id: str | None,
    model: str | None,
    detach: bool,
):
    """提交 PROMPT 并启动模型处理。

    不传 --session-id 时创建新会话；传入时从服务端轨迹重建有效上下文后继续。
    默认实时显示模型输出直到终态；--detach 只返回 session_id/message_id，稍后可用
    session follow 接回事件流。

    \b
    示例：
      mclaw session start "整理我的订阅"
      mclaw session start "继续" --session-id <session_id> --detach
    """
    body: dict = {"content": prompt}
    if session_id:
        body["session_id"] = session_id
    if model:
        body["model"] = model
    _submit_and_follow(
        settings,
        output_override,
        path="/sessions",
        body=body,
        detach=detach,
    )


@click.command(name="retry", short_help="重新提问指定用户消息，并替换该消息及后续轨迹")
@click.argument("session_id")
@click.option(
    "--message-id",
    required=True,
    help="要重新提问的 user message 编号（先用 get-transcript 查看）",
)
@click.option("--prompt", help="替换后的问题；留空时按原文重试")
@_submission_options
@click.pass_obj
def session_retry(
    settings,
    output_override: str | None,
    session_id: str,
    message_id: str,
    prompt: str | None,
    model: str | None,
    detach: bool,
):
    """重新提问 MESSAGE_ID；现有目标消息及其后的轨迹会被永久删除。

    目标必须是 user message。不传 --prompt 时按原文重试；传入时改用新问题。
    成功后会生成新的 message_id。默认实时显示新一轮输出；--detach 立即返回。

    \b
    示例：
      mclaw session retry <session_id> --message-id <message_id>
      mclaw session retry <session_id> --message-id <message_id> --prompt "改写后的问题"
    """
    body = {"message_id": message_id}
    if prompt:
        body["content"] = prompt
    if model:
        body["model"] = model
    _submit_and_follow(
        settings,
        output_override,
        path=f"/sessions/{session_id}/retry",
        body=body,
        detach=detach,
    )


@click.command(name="follow", short_help="跟随会话当前消息的实时事件流（支持断点续传）")
@click.argument("session_id")
@click.option("--from-id", type=int, default=None, help="从指定事件序号之后续传（缺省回放全部）")
@click.pass_obj
def session_follow(settings, session_id: str, from_id: int | None):
    """接回 SESSION_ID 当前或最近一条用户消息的处理事件。

    适用于 start/retry --detach 后或网络中断后继续观看；已结束时回放到终态后
    立即返回。--from-id 只接收该 SSE 事件序号之后的事件。
    """
    api = settings.make_api()
    try:
        event, payload = _stream_session(api, session_id, from_id)
    finally:
        api.close()
    _finish(event, payload)
