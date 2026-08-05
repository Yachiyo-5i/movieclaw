"""Webhook 投递器单测：签名可验、订阅过滤、重试、熔断、异常不外抛。

全部走 httpx.MockTransport，不发真实网络请求；设置读取与 server 信息打桩，
不依赖数据库。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from movieclaw_api.services.webhook import deliveries, dispatcher
from movieclaw_api.settings.webhook import WebhookEndpoint, WebhookSetting
from movieclaw_events import OutboundEvent
from movieclaw_net import CircuitOpenError

SERVER = {"id": "srv01", "name": "MovieClaw", "version": "0.0.0-test"}


def _endpoint(**overrides) -> WebhookEndpoint:
    payload = {
        "id": "ep01",
        "name": "测试端点",
        "url": "http://receiver.lan/hook",
        "format": "movieclaw",
        "enabled": True,
        "events": ["playback.started", "playback.completed"],
        "secret": "mcwh_testsecret",
    }
    payload.update(overrides)
    return WebhookEndpoint(**payload)


def _event(name: str = "playback.started") -> OutboundEvent:
    return OutboundEvent(event=name, data={"media": {"tmdb_id": 1}})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    deliveries.clear_deliveries()
    monkeypatch.setattr(dispatcher, "RETRY_DELAYS", (0.0, 0.0))

    async def _fake_server_info() -> dict:
        return SERVER

    monkeypatch.setattr(dispatcher, "_server_info", _fake_server_info)
    yield
    deliveries.clear_deliveries()


def _mock_transport(monkeypatch, handler):
    monkeypatch.setattr(
        dispatcher, "egress_transport", lambda service, scope: httpx.MockTransport(handler)
    )


async def test_signature_and_envelope(monkeypatch):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    _mock_transport(monkeypatch, handler)
    endpoint = _endpoint()
    event = _event("playback.completed")
    record = await dispatcher._deliver_one(endpoint, event, SERVER)

    assert record.ok and record.attempts == 1
    request = captured[0]
    body = request.content
    payload = json.loads(body)
    assert payload["spec_version"] == "1"
    assert payload["event"] == "playback.completed"
    assert payload["event_id"] == event.event_id
    assert payload["server"] == SERVER
    assert payload["data"] == event.data
    assert request.headers["X-MovieClaw-Event"] == "playback.completed"
    assert request.headers["X-MovieClaw-Delivery"] == event.event_id
    expected = hmac.new(b"mcwh_testsecret", body, hashlib.sha256).hexdigest()
    assert request.headers["X-MovieClaw-Signature"] == f"sha256={expected}"


async def test_retry_until_success(monkeypatch):
    codes = iter([500, 502, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(codes))

    _mock_transport(monkeypatch, handler)
    record = await dispatcher._deliver_one(_endpoint(), _event(), SERVER)
    assert record.ok
    assert record.attempts == 3


async def test_network_error_exhausts_attempts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("拒绝连接")

    _mock_transport(monkeypatch, handler)
    record = await dispatcher._deliver_one(_endpoint(), _event(), SERVER)
    assert not record.ok
    assert record.status_code is None
    assert record.attempts == 3
    assert "请求失败" in record.error


async def test_circuit_open_fails_fast(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise CircuitOpenError("webhook", 30.0)

    _mock_transport(monkeypatch, handler)
    record = await dispatcher._deliver_one(_endpoint(), _event(), SERVER)
    assert not record.ok
    assert record.attempts == 1  # 熔断快速失败，不重试
    assert "熔断" in record.error


async def test_jellyfin_format_without_template_records_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("未配置模板时不应发出请求")

    _mock_transport(monkeypatch, handler)
    record = await dispatcher._deliver_one(_endpoint(format="jellyfin"), _event(), SERVER)
    assert not record.ok
    assert "未配置模板" in record.error


async def test_jellyfin_format_renders_template(monkeypatch):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    _mock_transport(monkeypatch, handler)
    endpoint = _endpoint(
        format="jellyfin",
        template='{"type": "{{NotificationType}}", "server": "{{ServerId}}"}',
        headers={"Authorization": "Bearer xyz"},
    )
    event = _event("playback.started")
    record = await dispatcher._deliver_one(endpoint, event, SERVER)
    assert record.ok
    body = json.loads(captured[0].content)
    assert body == {"type": "PlaybackStart", "server": "srv01"}
    assert captured[0].headers["Authorization"] == "Bearer xyz"
    # jellyfin 格式不签名（对齐插件行为）
    assert "X-MovieClaw-Signature" not in captured[0].headers


async def test_jellyfin_template_error_recorded(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("渲染失败不应发出请求")

    _mock_transport(monkeypatch, handler)
    endpoint = _endpoint(format="jellyfin", template="{{substring Name 0 3}}")
    record = await dispatcher._deliver_one(endpoint, _event(), SERVER)
    assert not record.ok
    assert "模板渲染失败" in record.error


async def test_emit_events_filters_subscription(monkeypatch):
    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.headers["X-MovieClaw-Event"])
        return httpx.Response(204)

    _mock_transport(monkeypatch, handler)
    setting = WebhookSetting(
        enabled=True,
        endpoints=[_endpoint(events=["playback.started"])],
    )

    class _StubStore:
        async def get(self, model_cls):
            return setting

    monkeypatch.setattr(dispatcher, "get_setting_store", lambda: _StubStore())
    dispatcher.emit_events(
        [_event("playback.started"), _event("playback.stopped"), _event("bogus.event")]
    )
    await asyncio.gather(*dispatcher._tasks)
    assert received == ["playback.started"]
    assert len(deliveries.recent_deliveries("ep01")) == 1


async def test_emit_events_disabled_noop(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("总开关关闭时不应发请求")

    _mock_transport(monkeypatch, handler)

    class _StubStore:
        async def get(self, model_cls):
            return WebhookSetting(enabled=False, endpoints=[_endpoint()])

    monkeypatch.setattr(dispatcher, "get_setting_store", lambda: _StubStore())
    dispatcher.emit_events([_event()])
    await asyncio.gather(*dispatcher._tasks)
    assert deliveries.recent_deliveries("ep01") == []


async def test_emit_events_swallows_store_errors(monkeypatch):
    def _boom():
        raise RuntimeError("配置存储尚未初始化")

    monkeypatch.setattr(dispatcher, "get_setting_store", _boom)
    dispatcher.emit_events([_event()])
    await asyncio.gather(*dispatcher._tasks)  # 不外抛即通过


async def test_deliver_test_single_attempt(monkeypatch):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    _mock_transport(monkeypatch, handler)
    record = await dispatcher.deliver_test(_endpoint())
    assert record.event == "webhook.test"
    assert record.attempts == 1 and len(calls) == 1
    assert not record.ok
