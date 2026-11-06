"""subscriptions/listen."""

import asyncio

from aiohttp import web
from pytest_aiohttp import AiohttpClient

import aiohttp_mcp
from _helpers import (
    META_SERVER_INFO,
    META_SUBSCRIPTION_ID,
    MakeClient,
    next_sse,
    next_sse_data,
    request_body,
    request_headers,
)
from aiohttp_mcp._protocol import InputResult, JSONRPCNotification
from aiohttp_mcp._transport import MCPRequest, Method


async def test_listen_acknowledged(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body(
            "subscriptions/listen",
            {"notifications": {"toolsListChanged": True}},
        ),
        headers=request_headers("subscriptions/listen"),
    ) as resp:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        assert resp.headers["X-Accel-Buffering"] == "no"

        async with asyncio.timeout(5):
            ack = await next_sse_data(resp)
    assert ack["jsonrpc"] == "2.0"
    assert "id" not in ack
    assert ack["method"] == "notifications/subscriptions/acknowledged"
    assert ack["params"]["_meta"][META_SUBSCRIPTION_ID] == 1
    assert ack["params"]["notifications"] == {}


async def test_listen_no_filter(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body("subscriptions/listen"),
        headers=request_headers("subscriptions/listen"),
    ) as resp:
        async with asyncio.timeout(5):
            ack = await next_sse_data(resp)
    assert ack["params"]["notifications"] == {}


async def test_listen_invalid_filter(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body("subscriptions/listen", {"notifications": 5}),
        headers=request_headers("subscriptions/listen"),
    ) as resp:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        async with asyncio.timeout(5):
            event = await next_sse_data(resp)
        assert event["id"] == 1
        assert event["error"]["code"] == -32602
        assert await next_sse(resp) is None


async def test_emit_from_simple_method(aiohttp_client: AiohttpClient) -> None:
    """A non-streaming method emitting a message is a loud server-side error."""
    app = web.Application()
    server = aiohttp_mcp.setup(app, name="test-server", version="1.2.3")

    async def bad(req: MCPRequest) -> InputResult:
        notification: JSONRPCNotification = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
        }
        await req._emit(notification)
        return {}

    server._methods = {**server._methods, "test/emit": Method(bad)}
    client = await aiohttp_client(app)
    async with client.post(
        "/mcp",
        json=request_body("test/emit"),
        headers=request_headers("test/emit"),
    ) as resp:
        assert resp.status == 500
        assert (await resp.json())["error"]["code"] == -32603


async def test_listen_keepalive(make_client: MakeClient) -> None:
    client = await make_client(sse_keepalive=0.05)
    async with client.post(
        "/mcp",
        json=request_body("subscriptions/listen", request_id="sub-1"),
        headers=request_headers("subscriptions/listen"),
    ) as resp:
        async with asyncio.timeout(5):
            await next_sse_data(resp)  # ack
            event = await next_sse(resp)
    assert event == ("comment", "ping")


async def test_listen_graceful_shutdown(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body("subscriptions/listen", request_id="sub-1"),
        headers=request_headers("subscriptions/listen"),
    ) as resp:
        async with asyncio.timeout(5):
            await next_sse_data(resp)  # ack

            # Server-initiated teardown: respond to the request, then close.
            assert client.app is not None
            await client.app.shutdown()

            final = await next_sse_data(resp)
            assert final["id"] == "sub-1"
            result = final["result"]
            assert result["resultType"] == "complete"
            assert result["_meta"][META_SUBSCRIPTION_ID] == "sub-1"
            assert result["_meta"][META_SERVER_INFO]["name"] == "test-server"

            assert await next_sse(resp) is None  # stream closed


async def test_listen_disconnect_cancels(aiohttp_client: AiohttpClient) -> None:
    app = web.Application()
    server = aiohttp_mcp.setup(
        app, name="test-server", version="1.2.3", sse_keepalive=0.05
    )
    client = await aiohttp_client(app)
    async with client.post(
        "/mcp",
        json=request_body("subscriptions/listen"),
        headers=request_headers("subscriptions/listen"),
    ) as resp:
        async with asyncio.timeout(5):
            await next_sse_data(resp)  # ack
        assert len(server._subscriptions) == 1
    # Leaving the block closes the response stream: the cancellation signal.
    async with asyncio.timeout(5):
        while server._subscriptions:
            await asyncio.sleep(0.01)


async def test_concurrent_subscriptions(aiohttp_client: AiohttpClient) -> None:
    app = web.Application()
    server = aiohttp_mcp.setup(app, name="test-server", version="1.2.3")
    client = await aiohttp_client(app)
    headers = request_headers("subscriptions/listen")
    async with (
        client.post(
            "/mcp",
            json=request_body("subscriptions/listen", request_id="a"),
            headers=headers,
        ) as resp1,
        client.post(
            "/mcp",
            json=request_body("subscriptions/listen", request_id="b"),
            headers=headers,
        ) as resp2,
    ):
        async with asyncio.timeout(5):
            ack1 = await next_sse_data(resp1)
            ack2 = await next_sse_data(resp2)
        assert ack1["params"]["_meta"][META_SUBSCRIPTION_ID] == "a"
        assert ack2["params"]["_meta"][META_SUBSCRIPTION_ID] == "b"
        assert len(server._subscriptions) == 2
