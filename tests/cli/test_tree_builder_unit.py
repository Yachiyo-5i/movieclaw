"""生成器映射规则的单元测试：请求体组装与长任务轮询终态判定。"""

from __future__ import annotations

import pytest

from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.gen.tree_builder import _build_body, wait_long_task, wait_persistent_job


def _op_with_body() -> dict:
    return {
        "body_fields": [
            {"name": "kind", "kind": "simple", "required": True, "schema": {}, "description": ""},
            {
                "name": "seasons",
                "kind": "json",
                "required": False,
                "schema": {},
                "description": "",
            },
        ],
        "has_json_body": True,
    }


def test_build_body_from_flags() -> None:
    kwargs = {"kind": "tv", "seasons_json": "[1, 2]"}
    assert _build_body(_op_with_body(), kwargs) == {"kind": "tv", "seasons": [1, 2]}


def test_build_body_missing_required_raises_usage_error() -> None:
    with pytest.raises(CliError) as exc:
        _build_body(_op_with_body(), {"seasons_json": None, "kind": None})
    assert exc.value.exit_code == ExitCode.USAGE
    assert "--kind" in exc.value.message


def test_build_body_invalid_json_field() -> None:
    with pytest.raises(CliError) as exc:
        _build_body(_op_with_body(), {"kind": "tv", "seasons_json": "[not json"})
    assert exc.value.exit_code == ExitCode.USAGE
    assert "--seasons-json" in exc.value.message


def test_input_replaces_field_flags(tmp_path) -> None:
    body_file = tmp_path / "body.json"
    body_file.write_text('{"kind": "movie"}', encoding="utf-8")
    kwargs = {"input": str(body_file), "kind": "tv", "seasons_json": None}
    assert _build_body(_op_with_body(), kwargs) == {"kind": "movie"}


class _FakeApi:
    """按序返回预设进度的假客户端（只实现 wait_long_task 用到的 request）。"""

    def __init__(self, responses: list) -> None:
        self._responses = responses
        self.calls = 0

    def request(self, method: str, url: str, **_kwargs) -> dict | None:
        self.calls += 1
        return self._responses.pop(0)


def _long_task_op(task: dict) -> tuple[dict, dict]:
    progress_op = {
        "operation_id": "lib.show",
        "path": "/api/v1/libraries/{library_id}",
        "params": [{"name": "library_id", "in": "path"}],
    }
    op = {"long_task": task}
    return op, {"lib.show": progress_op}


def test_wait_terminates_when_progress_field_becomes_null(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    op, ops_by_id = _long_task_op({"progress_op": "lib.show", "progress_field": "scan_progress"})
    api = _FakeApi(
        [
            {"scan_progress": {"phase": "盘点", "processed": 1}},
            {"scan_progress": {"phase": "入账", "processed": 9}},
            {"scan_progress": None},
        ]
    )
    wait_long_task(op, ops_by_id, api, {"library_id": 1}, wait_timeout=60)
    assert api.calls == 3


def test_wait_terminates_on_done_field(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    op, ops_by_id = _long_task_op({"progress_op": "lib.show", "done_field": "refreshing"})
    api = _FakeApi([{"refreshing": True}, {"refreshing": False}])
    wait_long_task(op, ops_by_id, api, {"library_id": 1}, wait_timeout=60)
    assert api.calls == 2


def test_wait_timeout_exits_6(monkeypatch) -> None:
    fake_now = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: fake_now[0])

    def advance(_s: float) -> None:
        fake_now[0] += 100.0

    monkeypatch.setattr("time.sleep", advance)
    op, ops_by_id = _long_task_op({"progress_op": "lib.show", "progress_field": "scan_progress"})
    api = _FakeApi([{"scan_progress": {"phase": "盘点"}} for _ in range(10)])
    with pytest.raises(CliError) as exc:
        wait_long_task(op, ops_by_id, api, {"library_id": 1}, wait_timeout=50)
    assert exc.value.exit_code == ExitCode.TASK_FAILED
    assert "lib show" in (exc.value.hint or "")


def test_wait_survives_startup_race(monkeypatch) -> None:
    """后台任务注册状态前的轮询不能误判完成：先 None 后运行仍能正确等待。"""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    op, ops_by_id = _long_task_op({"progress_op": "lib.show", "progress_field": "scan_progress"})
    api = _FakeApi(
        [
            {"scan_progress": None},  # 任务还没注册状态
            {"scan_progress": {"phase": "盘点"}},
            {"scan_progress": None},  # 真正结束
        ]
    )
    wait_long_task(op, ops_by_id, api, {"library_id": 1}, wait_timeout=60)
    assert api.calls == 3


def test_wait_concludes_after_streak_when_never_running(monkeypatch) -> None:
    """快任务可能在首次轮询前就完成：连续 3 次终态读数后收敛，不无限等。"""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    op, ops_by_id = _long_task_op({"progress_op": "lib.show", "progress_field": "scan_progress"})
    api = _FakeApi([{"scan_progress": None}] * 5)
    wait_long_task(op, ops_by_id, api, {"library_id": 1}, wait_timeout=60)
    assert api.calls == 3


def test_persistent_job_wait_uses_job_status_not_domain_progress(monkeypatch) -> None:
    monkeypatch.setattr("time.monotonic", lambda: 0.0)
    api = _FakeApi(
        [
            {
                "changed": True,
                "job": {
                    "id": "job_1",
                    "revision": 2,
                    "status": "running",
                    "progress": {"message": "翻译中", "percent": 50},
                },
            },
            {
                "changed": True,
                "job": {
                    "id": "job_1",
                    "revision": 3,
                    "status": "succeeded",
                    "progress": {"message": "完成", "percent": 100},
                },
            },
        ]
    )

    wait_persistent_job(api, {"id": "job_1"}, {"id_path": "id"}, 60)

    assert api.calls == 2


def test_persistent_job_wait_reports_structured_failure(monkeypatch) -> None:
    monkeypatch.setattr("time.monotonic", lambda: 0.0)
    api = _FakeApi(
        [
            {
                "changed": True,
                "job": {
                    "id": "job_2",
                    "revision": 4,
                    "status": "failed",
                    "progress": {"message": "未完成", "percent": None},
                    "error": {
                        "message": "模型请求失败",
                        "actions": [{"type": "handoff_agent", "label": "交给 Agent"}],
                    },
                },
            }
        ]
    )

    with pytest.raises(CliError) as exc:
        wait_persistent_job(api, {"id": "job_2"}, {"id_path": "id"}, 60)

    assert exc.value.exit_code == ExitCode.TASK_FAILED
    assert exc.value.message == "模型请求失败"
    assert "Agent" in (exc.value.hint or "")
