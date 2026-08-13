"""OpenAPI → CLI 契约守护测试（docs/design/cli.md §2）。

CLI 的命令树由 OpenAPI spec 自动生成，spec 里的元数据就是 CLI 的
命令名与 help。本测试把「新增路由必须带齐 CLI 化元数据」变成机械保证：

- 每个端点必须有非空 summary（→ 命令一行简介）；
- operation_id 必须显式声明、符合 `<域>.<动作>` 两/三段式命名约定
  （→ 直接决定命令树：subscriptions.list → mclaw subscriptions list）且全局唯一。

漏写任何一项，这里直接红——模式对标 test_auth.py 的全路由鉴权守护。
"""

from __future__ import annotations

import re

import pytest

from movieclaw_api.export_openapi import build_spec

# 与 docs/design/cli.md §2.2 约定一致：全小写，段间用点，段内可用连字符
OPERATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9-]+)+$")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def operations() -> list[tuple[str, str, dict]]:
    spec = build_spec()
    ops: list[tuple[str, str, dict]] = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method in _HTTP_METHODS:
                ops.append((method.upper(), path, op))
    return ops


def test_every_operation_has_summary(operations: list[tuple[str, str, dict]]) -> None:
    missing = [f"{m} {p}" for m, p, op in operations if not op.get("summary", "").strip()]
    assert not missing, f"以下端点缺少 summary（CLI help 的一行简介来源）：{missing}"


def test_every_operation_id_follows_convention(
    operations: list[tuple[str, str, dict]],
) -> None:
    bad = [
        f"{m} {p} -> {op.get('operationId')!r}"
        for m, p, op in operations
        if not OPERATION_ID_PATTERN.match(op.get("operationId") or "")
    ]
    assert not bad, f"以下端点的 operation_id 不符合 `<域>.<动作>` 约定：{bad}"


def test_operation_ids_are_unique(operations: list[tuple[str, str, dict]]) -> None:
    seen: dict[str, str] = {}
    dup: list[str] = []
    for method, path, op in operations:
        opid = op["operationId"]
        if opid in seen:
            dup.append(f"{opid}: {seen[opid]} 与 {method} {path}")
        else:
            seen[opid] = f"{method} {path}"
    assert not dup, f"operation_id 重复（命令名会冲突）：{dup}"


def test_removed_compatibility_routes_do_not_return(
    operations: list[tuple[str, str, dict]],
) -> None:
    """接口重构直接收口，不允许重新引入 legacy/deprecated 双轨实现。"""
    old_paths = {
        "/api/v1/discover/search",
        "/api/v1/discover/douban/collection/{collection_id}",
        "/api/v1/discover/douban/{douban_id}",
        "/api/v1/discover/{kind}/layout",
        "/api/v1/discover/{kind}/hero",
        "/api/v1/discover/{kind}/rows/{row_id}",
        "/api/v1/discover/{kind}/{tmdb_id}",
        "/api/v1/subscriptions/prepare",
        "/api/v1/subscriptions/dispatch-preview",
        "/api/v1/subscriptions/pipeline-health",
        "/api/v1/subscriptions/{subscription_id}/downloads",
        "/api/v1/subscriptions/{subscription_id}/search-now",
        "/api/v1/subscriptions/{subscription_id}/grab",
        "/api/v1/subscriptions/{subscription_id}/pause",
    }
    remaining_paths = sorted(old_paths & {path for _method, path, _op in operations})
    legacy_ops = [
        op["operationId"]
        for _method, _path, op in operations
        if op["operationId"].startswith("legacy.") or op.get("deprecated")
    ]
    assert not remaining_paths, f"旧接口路径被重新引入：{remaining_paths}"
    assert not legacy_ops, f"旧接口 operation_id/deprecated 被重新引入：{legacy_ops}"


# ---------------------------------------------------------------------------
# x-cli-* 标注守护（docs/design/cli.md §2.3）：漏标即红
# ---------------------------------------------------------------------------


def test_every_delete_declares_danger_level(operations: list[tuple[str, str, dict]]) -> None:
    """DELETE 端点必须声明 x-cli-dangerous——CLI 据此决定 --yes 门槛。"""
    missing = [
        op["operationId"]
        for method, _path, op in operations
        if method == "DELETE" and op.get("x-cli-dangerous") not in ("confirm", "destructive")
    ]
    assert not missing, (
        f"以下 DELETE 端点未声明 x-cli-dangerous（confirm/destructive）：{missing}"
    )


def test_x_cli_vocabulary_is_valid(operations: list[tuple[str, str, dict]]) -> None:
    """x-cli-* 扩展字段的取值必须合法，long-task 的进度端点必须真实存在。"""
    all_ids = {op["operationId"] for _m, _p, op in operations}
    problems: list[str] = []
    for _method, _path, op in operations:
        opid = op["operationId"]
        if (danger := op.get("x-cli-dangerous")) is not None and danger not in (
            "confirm",
            "destructive",
        ):
            problems.append(f"{opid}: x-cli-dangerous 取值非法 {danger!r}")
        if (task := op.get("x-cli-long-task")) is not None:
            if task.get("progress_op") not in all_ids:
                problems.append(f"{opid}: x-cli-long-task.progress_op 指向不存在的端点")
            if not (task.get("progress_field") or task.get("done_field")):
                problems.append(f"{opid}: x-cli-long-task 缺少 progress_field/done_field")
        if (stream := op.get("x-cli-stream")) is not None and not stream.get("terminal_events"):
            problems.append(f"{opid}: x-cli-stream 缺少 terminal_events")
    assert not problems, f"x-cli 标注不合法：{problems}"
