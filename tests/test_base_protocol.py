"""Transport, validation, and server/discover tests for the base protocol."""

from typing import Any

from aiohttp import ClientResponse, web
from pytest_aiohttp import AiohttpClient

import aiohttp_mcp
from _helpers import (
    META_CAPABILITIES,
    META_SERVER_INFO,
    META_VERSION,
    PROTOCOL_VERSION,
    MakeClient,
    request_body,
    request_headers,
)


async def assert_error(resp: ClientResponse, status: int, code: int) -> dict[str, Any]:
    assert resp.status == status
    body: dict[str, Any] = await resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == code
    return body


async def test_discover(make_client: MakeClient) -> None:
    client = await make_client(
        title="Test Server",
        description="A server for tests.",
        website_url="https://example.com",
        instructions="Use wisely.",
    )
    async with client.post(
        "/mcp",
        json=request_body("server/discover"),
        headers=request_headers("server/discover"),
    ) as resp:
        assert resp.status == 200
        assert resp.content_type == "application/json"
        body = await resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [PROTOCOL_VERSION]
    assert result["capabilities"] == {}
    assert result["instructions"] == "Use wisely."
    assert result["ttlMs"] == 3_600_000
    assert result["cacheScope"] == "public"
    assert result["_meta"][META_SERVER_INFO] == {
        "name": "test-server",
        "version": "1.2.3",
        "title": "Test Server",
        "description": "A server for tests.",
        "websiteUrl": "https://example.com",
    }


async def test_discover_minimal(make_client: MakeClient) -> None:
    client = await make_client(discover_ttl=0.5, discover_cache_scope="private")
    async with client.post(
        "/mcp",
        json=request_body("server/discover", request_id="abc"),
        headers=request_headers("server/discover"),
    ) as resp:
        body = await resp.json()
    assert body["id"] == "abc"
    result = body["result"]
    assert result["ttlMs"] == 500
    assert result["cacheScope"] == "private"
    assert "instructions" not in result
    assert result["_meta"][META_SERVER_INFO] == {
        "name": "test-server",
        "version": "1.2.3",
    }


async def test_no_server_info(make_client: MakeClient) -> None:
    client = await make_client(include_server_info=False)
    async with client.post(
        "/mcp",
        json=request_body("server/discover"),
        headers=request_headers("server/discover"),
    ) as resp:
        assert "_meta" not in (await resp.json())["result"]


async def test_unknown_method(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body("tools/list"),
        headers=request_headers("tools/list"),
    ) as resp:
        body = await assert_error(resp, 404, -32601)
    assert body["id"] == 1


async def test_notification_rejected(make_client: MakeClient) -> None:
    client = await make_client()
    # Client notification is not valid over HTTP.
    body = request_body("notifications/cancelled")
    del body["id"]  # type: ignore[misc]
    async with client.post(
        "/mcp", json=body, headers=request_headers("notifications/cancelled")
    ) as resp:
        error = await assert_error(resp, 400, -32600)
    assert "id" not in error


async def test_missing_version_header(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body("server/discover"),
        headers=request_headers("server/discover", header_version=None),
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_version_header_body_mismatch(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    body["params"]["_meta"][META_VERSION] = "2025-11-25"  # type: ignore[index]
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_unsupported_version(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    body["params"]["_meta"][META_VERSION] = "2025-11-25"  # type: ignore[index]
    async with client.post(
        "/mcp",
        json=body,
        headers=request_headers("server/discover", header_version="2025-11-25"),
    ) as resp:
        error = await assert_error(resp, 400, -32022)
    assert error["error"]["data"] == {
        "supported": [PROTOCOL_VERSION],
        "requested": "2025-11-25",
    }


async def test_missing_method_header(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("server/discover")
    del headers["Mcp-Method"]
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_method_header_mismatch(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("subscriptions/listen")
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_missing_meta(make_client: MakeClient) -> None:
    client = await make_client()
    body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32602)


async def test_missing_capabilities(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    del body["params"]["_meta"][META_CAPABILITIES]  # type: ignore[attr-defined]
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32602)


async def test_missing_meta_version(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    del body["params"]["_meta"][META_VERSION]  # type: ignore[attr-defined]
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32602)


async def test_null_id(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    body["id"] = None  # type: ignore[typeddict-item]
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        error = await assert_error(resp, 400, -32600)
    assert "id" not in error


async def test_float_id(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    body["id"] = 1.5  # type: ignore[typeddict-item]
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32600)


async def test_bool_id(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    body["id"] = True
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32600)


async def test_non_object_params(make_client: MakeClient) -> None:
    client = await make_client()
    body = request_body("server/discover")
    body["params"] = 5  # type: ignore[typeddict-item]
    async with client.post(
        "/mcp", json=body, headers=request_headers("server/discover")
    ) as resp:
        await assert_error(resp, 400, -32600)


async def test_batch_rejected(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        # Array of requests (JSON-RPC batching) is not permitted.
        json=[request_body("server/discover")],
        headers=request_headers("server/discover"),
    ) as resp:
        await assert_error(resp, 400, -32600)


async def test_client_response_rejected(make_client: MakeClient) -> None:
    client = await make_client()
    body = {"jsonrpc": "2.0", "id": 1, "result": {}}
    async with client.post("/mcp", json=body, headers=request_headers("x")) as resp:
        await assert_error(resp, 400, -32600)


async def test_parse_error(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp", data=b"{not json", headers=request_headers("server/discover")
    ) as resp:
        body = await assert_error(resp, 400, -32700)
    assert "id" not in body


async def test_parse_error_non_utf8(make_client: MakeClient) -> None:
    """JSON must be UTF-8, whatever charset a client declares."""
    client = await make_client()
    headers = request_headers("server/discover")
    headers["Content-Type"] = "application/json; charset=latin-1"
    async with client.post("/mcp", data=b'{"m\xe9thod": 1}', headers=headers) as resp:
        await assert_error(resp, 400, -32700)


async def test_non_post(make_client: MakeClient) -> None:
    client = await make_client()
    for method in ("GET", "DELETE", "PUT"):
        async with client.request(method, "/mcp") as resp:
            assert resp.status == 405
            assert resp.headers["Allow"] == "POST"


async def test_origin_rejected(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("server/discover")
    headers["Origin"] = "https://evil.example"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        body = await assert_error(resp, 403, -32600)
    assert "id" not in body


async def test_origin_null_rejected(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("server/discover")
    headers["Origin"] = "null"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 403


async def test_legacy_initialize(make_client: MakeClient) -> None:
    client = await make_client()
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "old-client", "version": "1.0"},
        },
    }
    async with client.post("/mcp", json=body) as resp:
        error = await assert_error(resp, 404, -32601)
    assert error["id"] == 0
    assert PROTOCOL_VERSION in error["error"]["message"]
    assert error["error"]["data"] == {"supported": [PROTOCOL_VERSION]}


async def test_origin_loopback(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("server/discover")
    for origin in (f"http://{client.host}:{client.port}", "http://localhost:3000"):
        headers["Origin"] = origin
        async with client.post(
            "/mcp", json=request_body("server/discover"), headers=headers
        ) as resp:
            assert resp.status == 200


async def test_origin_dns_rebinding(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("server/discover")
    headers["Origin"] = "http://evil.example:8080"
    headers["Host"] = "evil.example:8080"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 403


async def test_origin_allowlist(make_client: MakeClient) -> None:
    client = await make_client(allowed_origins={"app.example"})
    headers = request_headers("server/discover")
    headers["Origin"] = "https://app.example"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 200


async def test_origin_callable(make_client: MakeClient) -> None:
    client = await make_client(allowed_origins=lambda o: o.endswith(".test"))
    headers = request_headers("server/discover")
    headers["Origin"] = "https://foo.test"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 200

    # A callable replaces the default loopback policy entirely.
    headers["Origin"] = f"http://{client.host}:{client.port}"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 403


async def test_name_header_required(make_client: MakeClient) -> None:
    client = await make_client()
    async with client.post(
        "/mcp",
        json=request_body("tools/call", {"name": "add"}),
        headers=request_headers("tools/call"),
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_name_header_mismatch(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("tools/call")
    headers["Mcp-Name"] = "subtract"
    async with client.post(
        "/mcp", json=request_body("tools/call", {"name": "add"}), headers=headers
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_name_header_match_reaches_dispatch(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("tools/call")
    headers["Mcp-Name"] = "add"
    async with client.post(
        "/mcp", json=request_body("tools/call", {"name": "add"}), headers=headers
    ) as resp:
        await assert_error(resp, 404, -32601)


async def test_name_header_base64_sentinel(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("tools/call")
    # "Hello, 世界" per the spec's encoding example.
    headers["Mcp-Name"] = "=?base64?SGVsbG8sIOS4lueVjA==?="
    async with client.post(
        "/mcp",
        json=request_body("tools/call", {"name": "Hello, 世界"}),
        headers=headers,
    ) as resp:
        await assert_error(resp, 404, -32601)


async def test_name_header_bad_base64(make_client: MakeClient) -> None:
    client = await make_client()
    headers = request_headers("tools/call")
    headers["Mcp-Name"] = "=?base64?!!not-base64!!?="
    async with client.post(
        "/mcp", json=request_body("tools/call", {"name": "add"}), headers=headers
    ) as resp:
        await assert_error(resp, 400, -32020)


async def test_second_endpoint(make_client: MakeClient, aiohttp_client: AiohttpClient) -> None:
    app = web.Application()
    aiohttp_mcp.setup(app, name="one", version="1.0")
    aiohttp_mcp.setup(app, name="two", version="2.0", path="/other")
    client = await aiohttp_client(app)
    for path, name in (("/mcp", "one"), ("/other", "two")):
        async with client.post(
            path,
            json=request_body("server/discover"),
            headers=request_headers("server/discover"),
        ) as resp:
            body = await resp.json()
        assert body["result"]["_meta"][META_SERVER_INFO]["name"] == name
