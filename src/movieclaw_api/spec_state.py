"""spec 内容指纹的进程内缓存。

指纹用途（docs/design/cli.md §2.1）：CLI 比对「内置基线 spec 的 hash」与
「服务器返回的 hash」即可零成本发现版本偏斜。指纹随 /health 响应与所有
/api/v1 响应头（X-Movieclaw-Spec-Hash）下发；算法与 export_openapi.spec_hash
完全一致——两端必须同一算法，否则偏斜检测失效。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from movieclaw_api.export_openapi import spec_hash

SPEC_HASH_HEADER = "X-Movieclaw-Spec-Hash"

# create_app() 在测试中会为每个用例构造新 FastAPI 实例，但这些实例挂载的业务
# 路由完全相同。FastAPI 只把 OpenAPI 缓存在 app.openapi_schema 上，原先再叠加的
# app.state 缓存仍无法跨实例复用，导致 400KB 级 schema 被重复生成数百次。
#
# 这里按「会影响 OpenAPI 的应用元数据 + 路由声明」做一个小型进程级缓存：生产环境
# 通常只有一个 app；测试里的同构 app 命中同一项；临时追加路由的测试则因签名不同
# 独立计算。缓存有界，避免动态创建大量 app 时长期持有端点函数。
_SHARED_CACHE_SIZE = 8
_shared_spec_hashes: OrderedDict[tuple[Any, ...], str] = OrderedDict()
_shared_spec_hashes_lock = threading.Lock()


def _stable_value(value: Any) -> Any:
    """把路由元数据变成可哈希值；常见类/函数保留身份，容器退回 repr。"""
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _route_signature(app: FastAPI) -> tuple[Any, ...]:
    """提取足以区分 OpenAPI 声明的轻量签名，不触发 schema 生成。"""
    routes = tuple(
        (
            route.path_format,
            tuple(sorted(route.methods or ())),
            route.endpoint,
            route.name,
            route.operation_id,
            route.include_in_schema,
            route.status_code,
            route.summary,
            route.description,
            route.deprecated,
            tuple(route.tags or ()),
            _stable_value(route.response_model),
            _stable_value(route.response_class),
            repr(route.responses),
            repr(route.openapi_extra),
            tuple(_stable_value(item.dependency) for item in route.dependencies),
        )
        for route in app.routes
        if isinstance(route, APIRoute)
    )
    return (
        app.title,
        app.summary,
        app.description,
        app.version,
        app.openapi_version,
        repr(app.servers),
        repr(app.openapi_tags),
        app.separate_input_output_schemas,
        routes,
    )


def get_spec_hash(app: FastAPI) -> str:
    """取当前 app 的 spec 指纹，并在同构 app 之间复用计算结果。"""
    cached = getattr(app.state, "spec_hash", None)
    if cached is not None:
        return cached

    signature = _route_signature(app)
    # 首次生成约需数百毫秒。锁覆盖计算过程，避免并发启动多个同构 app 时重复
    # 做同一份 CPU 密集工作；后续请求先命中 app.state，不会再竞争此锁。
    with _shared_spec_hashes_lock:
        cached = _shared_spec_hashes.get(signature)
        if cached is None:
            cached = spec_hash(app.openapi())
            _shared_spec_hashes[signature] = cached
            if len(_shared_spec_hashes) > _SHARED_CACHE_SIZE:
                _shared_spec_hashes.popitem(last=False)
        else:
            _shared_spec_hashes.move_to_end(signature)

    app.state.spec_hash = cached
    return cached
