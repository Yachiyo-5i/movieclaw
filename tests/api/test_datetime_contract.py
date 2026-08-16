"""API 时间契约门禁：所有 datetime 响应都必须携带明确时区。"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime
from pathlib import Path
from typing import get_args

from fastapi.routing import APIRoute
from pydantic import BaseModel as PydanticBaseModel

from movieclaw_api.app import create_app
from movieclaw_api.schemas.base import BaseModel, normalize_utc_iso, utc_isoformat

_SAMPLE_NAIVE_UTC = datetime(2026, 8, 14, 11, 12, 52, 123456)


def _contains_datetime(annotation: object) -> bool:
    if annotation is datetime:
        return True
    return any(_contains_datetime(item) for item in get_args(annotation))


def _model_types(annotation: object, seen: set[object] | None = None) -> set[type]:
    """递归找出泛型响应信封、列表和联合类型内的 Pydantic 模型。"""
    seen = seen or set()
    if annotation in seen:
        return set()
    seen.add(annotation)
    found: set[type] = set()
    if inspect.isclass(annotation) and issubclass(annotation, PydanticBaseModel):
        found.add(annotation)
        for field in annotation.model_fields.values():
            found.update(_model_types(field.annotation, seen))
    for item in get_args(annotation):
        found.update(_model_types(item, seen))
    return found


def _routes(routes: list, prefix: str = ""):  # type: ignore[type-arg]
    """兼容 FastAPI 新版延迟 include_router，展开全部真实业务路由。"""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            child_prefix = route.include_context.prefix or ""
            yield from _routes(route.original_router.routes, prefix + child_prefix)


def test_common_api_model_serializes_all_datetime_shapes_as_utc() -> None:
    class ExampleView(BaseModel):
        direct: datetime
        sequence: list[datetime]
        mapping: dict[str, datetime]

    view = ExampleView(
        direct=_SAMPLE_NAIVE_UTC,
        sequence=[_SAMPLE_NAIVE_UTC],
        mapping={"at": _SAMPLE_NAIVE_UTC},
    )

    assert view.model_dump(mode="json") == {
        "direct": "2026-08-14T11:12:52.123456+00:00",
        "sequence": ["2026-08-14T11:12:52.123456+00:00"],
        "mapping": {"at": "2026-08-14T11:12:52.123456+00:00"},
    }
    assert utc_isoformat(_SAMPLE_NAIVE_UTC).endswith("+00:00")
    assert normalize_utc_iso("2026-08-14T11:12:52.123456") == (
        "2026-08-14T11:12:52.123456+00:00"
    )


def test_every_rest_response_datetime_has_timezone() -> None:
    """新增响应模型若漏继承公共基类或漏 serializer，本测试立即失败。"""
    failures: list[str] = []
    app = create_app()
    for path, route in _routes(app.routes):
        if route.response_model is None:
            continue
        for model in _model_types(route.response_model):
            fields = {
                name
                for name, field in model.model_fields.items()
                if _contains_datetime(field.annotation)
            }
            if not fields:
                continue
            instance = model.model_construct(
                **{name: _SAMPLE_NAIVE_UTC for name in fields}
            )
            payload = instance.model_dump(mode="json", include=fields)
            for name in fields:
                rendered = payload.get(name)
                try:
                    parsed = datetime.fromisoformat(rendered) if isinstance(rendered, str) else None
                except ValueError:
                    parsed = None
                if parsed is None or parsed.tzinfo is None:
                    failures.append(
                        f"{','.join(sorted(route.methods or []))} {path}: "
                        f"{model.__module__}.{model.__name__}.{name}={rendered!r}"
                    )

    assert not failures, "发现没有时区标记的 API 时间：\n" + "\n".join(sorted(failures))


def test_routes_do_not_bypass_central_datetime_serializer() -> None:
    """SSE/手写 JSON 也必须走公共 helper，禁止在路由里裸调 isoformat。"""
    routes_dir = Path(__file__).parents[2] / "src/movieclaw_api/api/routes"
    violations: list[str] = []
    for path in routes_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "isoformat"
            ):
                violations.append(f"{path.relative_to(routes_dir)}:{node.lineno}")

    assert not violations, "路由不得裸调 isoformat，请使用 utc_isoformat：" + ", ".join(
        sorted(violations)
    )
