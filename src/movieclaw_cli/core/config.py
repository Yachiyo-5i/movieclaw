"""CLI 本地配置与凭证（docs/design/cli.md §8.2）。

两个文件、职责分离（配置可进 dotfiles 同步，凭证绝不）：

    ~/.config/movieclaw/config.toml    多上下文配置（服务器地址、默认输出）
    ~/.config/movieclaw/credentials    会话凭证（JSON，0600，按服务器地址存）

服务器地址解析优先级（与 gcloud/kubectl 一致）：
    --server 标志 > MOVIECLAW_SERVER 环境变量 > 当前上下文 > 报错并提示
环境变量是 Agent/CI 的主通道，可完全不落盘。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from movieclaw_cli.core.errors import CliError, ExitCode

ENV_SERVER = "MOVIECLAW_SERVER"
ENV_TOKEN = "MOVIECLAW_TOKEN"
# 产品内 Agent 注入的服务端锁。普通 CLI 不设置它，仍可通过 --server 管理多台实例；
# Agent 带着短时效高权限令牌，绝不能把该令牌随 --server 转发给任意地址。
ENV_AGENT_LOCKED_SERVER = "MOVIECLAW_AGENT_LOCKED_SERVER"
ENV_CONTEXT = "MOVIECLAW_CONTEXT"
ENV_CONFIG_DIR = "MOVIECLAW_CONFIG_DIR"


def config_dir() -> Path:
    if override := os.environ.get(ENV_CONFIG_DIR):
        return Path(override)
    return Path.home() / ".config" / "movieclaw"


def _config_path() -> Path:
    return config_dir() / "config.toml"


def _credentials_path() -> Path:
    return config_dir() / "credentials"


def load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CliError(
            f"配置文件格式错误：{path}（{exc}）",
            exit_code=ExitCode.USAGE,
            hint="修正该文件或删除后重新执行 mclaw login",
        ) from exc


def save_config(config: dict[str, Any]) -> None:
    """写回配置。结构固定且简单，手写 TOML 序列化避免引入写库依赖。

    字符串值用 json.dumps 序列化——JSON 字符串转义是合法的 TOML 基本
    字符串，含引号/反斜杠的值不会写出损坏的配置文件。
    """
    lines: list[str] = []
    if current := config.get("current_context"):
        lines.append(f"current_context = {json.dumps(current, ensure_ascii=False)}")
        lines.append("")
    for name, ctx in (config.get("contexts") or {}).items():
        lines.append(f"[contexts.{name}]")
        for key, value in ctx.items():
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        lines.append("")
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def resolve_server(flag_server: str | None, flag_context: str | None) -> str:
    """按优先级解析目标服务器地址；解析不出来时给出可行动的中文指引。"""
    if flag_server:
        return flag_server.rstrip("/")
    if env := os.environ.get(ENV_SERVER):
        return env.rstrip("/")
    config = load_config()
    contexts = config.get("contexts") or {}
    name = flag_context or os.environ.get(ENV_CONTEXT) or config.get("current_context")
    if name:
        ctx = contexts.get(name)
        if ctx is None:
            raise CliError(
                f"上下文不存在：{name}",
                exit_code=ExitCode.USAGE,
                hint=f"可用上下文：{', '.join(contexts) or '（无）'}；"
                "或用 mclaw login --server <地址> 新建",
            )
        return str(ctx["server"]).rstrip("/")
    raise CliError(
        "未指定 movieclaw 服务器地址",
        exit_code=ExitCode.USAGE,
        hint="三种方式任选：mclaw login --server http://<主机>:3000 登录并记住；"
        "或设置环境变量 MOVIECLAW_SERVER；或加 --server 标志",
    )


_CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def save_context(server: str, name: str = "default") -> None:
    """登录成功后把服务器记进上下文；首个上下文自动设为当前。"""
    if not _CONTEXT_NAME_RE.match(name):
        raise CliError(
            f"上下文名不合法：{name}",
            exit_code=ExitCode.USAGE,
            hint="只允许字母、数字、连字符与下划线（TOML 表名约束）",
        )
    config = load_config()
    contexts = config.setdefault("contexts", {})
    contexts[name] = {"server": server}
    config.setdefault("current_context", name)
    save_config(config)


# ---------------------------------------------------------------------------
# 会话凭证（Cookie）。P1 引入 API Token 后此文件同样承载 token。
# ---------------------------------------------------------------------------


def _load_credentials() -> dict[str, Any]:
    path = _credentials_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_session_cookie(server: str) -> str | None:
    entry = _load_credentials().get(server)
    return entry.get("session_cookie") if isinstance(entry, dict) else None


def _write_credentials(creds: dict[str, Any]) -> None:
    """凭证落盘：以 0600 权限打开后再写入，不留「先写后 chmod」的可读窗口。"""
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(creds, ensure_ascii=False, indent=2))
    path.chmod(0o600)  # 已存在的旧文件也收紧权限


def save_session_cookie(server: str, cookie: str) -> None:
    creds = _load_credentials()
    creds[server] = {"session_cookie": cookie}
    _write_credentials(creds)


def delete_session_cookie(server: str) -> None:
    creds = _load_credentials()
    if server in creds:
        del creds[server]
        _write_credentials(creds)
