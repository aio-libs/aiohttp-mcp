import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient

from _helpers import (
    META_SUBSCRIPTION_ID,
    MakeClient,
    next_sse_data,
    request_body,
    request_headers,
)
from aiohttp_mcp import (
    Content,
    EmbeddedResource,
    Icon,
    MCPRequest,
    MCPServer,
    PromptRegistry,
    Resource,
    ResourceContents,
    ResourceRegistry,
    TextContent,
    ToolRegistry,
)

Client = TestClient[web.Request, web.Application]


async def readme() -> str:
    """Project README.

    The entry point to the documentation.
    """
    return "# aiohttp-mcp"


async def logo() -> bytes:
    return b"\x89PNG..."


async def project_file(path: str) -> ResourceContents:
    """Project file.

    A file from the project directory.
    """
    return ResourceContents(text=f"contents of {path}")


def registry(**kwargs: Any) -> ResourceRegistry:
    return ResourceRegistry(
        Resource(readme, "docs://readme", mime_type="text/markdown", size=12),
        Resource(logo, "docs://logo", mime_type="image/png"),
        Resource(project_file, "file:///{+path}", mime_type="text/plain"),
        **kwargs,
    )


def post(
    client: Client,
    method: str,
    params: dict[str, object] | None = None,
    *,
    name: str | None = None,
    token: str | None = None,
    capabilities: dict[str, object] | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    headers = request_headers(method)
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = request_body(method, params, capabilities=capabilities)
    return client.post("/mcp", json=body, headers=headers)


def read(client: Client, uri: str, **kwargs: Any) -> AbstractAsyncContextManager[ClientResponse]:
    return post(client, "resources/read", {"uri": uri}, name=uri, **kwargs)


async def result_of(resp: ClientResponse) -> dict[str, Any]:
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert isinstance(body["result"], dict)
    return body["result"]


async def test_list(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with post(client, "resources/list") as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 300_000
    assert result["cacheScope"] == "private"
    assert result["resources"] == [
        {
            "uri": "docs://readme",
            "name": "readme",
            "title": "Project README.",
            "description": "The entry point to the documentation.",
            "mimeType": "text/markdown",
            "size": 12,
        },
        {"uri": "docs://logo", "name": "logo", "mimeType": "image/png"},
    ]


async def test_templates_list(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with post(client, "resources/templates/list") as resp:
        result = await result_of(resp)
    assert result["resourceTemplates"] == [
        {
            "uriTemplate": "file:///{+path}",
            "name": "project_file",
            "title": "Project file.",
            "description": "A file from the project directory.",
            "mimeType": "text/plain",
        }
    ]


async def test_capability(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with post(client, "server/discover") as resp:
        result = await result_of(resp)
    assert result["capabilities"]["resources"] == {"subscribe": True}


async def test_no_resources_configured(make_client: MakeClient) -> None:
    client = await make_client()
    async with post(client, "server/discover") as resp:
        assert "resources" not in (await result_of(resp))["capabilities"]
    async with post(client, "resources/list") as resp:
        assert resp.status == 404


async def test_read_text(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with read(client, "docs://readme") as resp:
        result = await result_of(resp)
    assert result["contents"] == [
        {"uri": "docs://readme", "mimeType": "text/markdown", "text": "# aiohttp-mcp"}
    ]
    assert result["ttlMs"] == 60_000
    assert result["cacheScope"] == "private"


async def test_read_blob(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with read(client, "docs://logo") as resp:
        result = await result_of(resp)
    assert result["contents"] == [
        {"uri": "docs://logo", "mimeType": "image/png", "blob": "iVBORy4uLg=="}
    ]


async def test_read_template(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with read(client, "file:///src/main.rs") as resp:
        result = await result_of(resp)
    # {+path} matches across '/' and is not percent-decoded.
    assert result["contents"] == [
        {"uri": "file:///src/main.rs", "text": "contents of src/main.rs"}
    ]


async def test_simple_variable_decoded(make_client: MakeClient) -> None:
    async def user(name: str) -> str:
        return f"user {name}"

    client = await make_client(resources=ResourceRegistry(Resource(user, "users://{name}")))
    async with read(client, "users://anne%20bonny") as resp:
        result = await result_of(resp)
    assert result["contents"][0]["text"] == "user anne bonny"
    # A simple variable must not match across a path segment.
    async with read(client, "users://a/b") as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_most_specific_template_wins(make_client: MakeClient) -> None:
    """Templates are resolved by the longest fixed prefix, like aiohttp routes."""

    async def catch_all(path: str) -> str:
        return f"generic {path}"

    async def source_file(path: str) -> str:
        return f"source {path}"

    client = await make_client(
        resources=ResourceRegistry(
            # Registered first, but with a less specific prefix.
            Resource(catch_all, "file:///{+path}"),
            Resource(source_file, "file:///src/{+path}"),
        )
    )
    async with read(client, "file:///src/main.rs") as resp:
        result = await result_of(resp)
    assert result["contents"][0]["text"] == "source main.rs"
    async with read(client, "file:///docs/readme.md") as resp:
        result = await result_of(resp)
    assert result["contents"][0]["text"] == "generic docs/readme.md"


async def test_multiple_contents(make_client: MakeClient) -> None:
    async def directory() -> list[ResourceContents]:
        return [
            ResourceContents("dir://a.txt", text="a"),
            ResourceContents("dir://b.bin", blob=b"b", mime_type="application/octet-stream"),
        ]

    client = await make_client(resources=ResourceRegistry(Resource(directory, "dir://")))
    async with read(client, "dir://") as resp:
        result = await result_of(resp)
    assert result["contents"] == [
        {"uri": "dir://a.txt", "text": "a"},
        {"uri": "dir://b.bin", "blob": "Yg==", "mimeType": "application/octet-stream"},
    ]


async def test_empty_contents_means_no_content(make_client: MakeClient) -> None:
    async def empty() -> list[ResourceContents]:
        return []

    client = await make_client(resources=ResourceRegistry(Resource(empty, "dir://")))
    async with read(client, "dir://") as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    assert result["contents"] == []


async def test_not_found(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with read(client, "docs://missing") as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32602
    assert error["data"] == {"uri": "docs://missing"}


async def test_list_rejects_cursor(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    for method in ("resources/list", "resources/templates/list"):
        async with post(client, method, {"cursor": ""}) as resp:
            assert (await resp.json())["error"]["code"] == -32602


async def test_read_ttl_overrides(make_client: MakeClient) -> None:
    client = await make_client(
        resources=ResourceRegistry(
            Resource(readme, "docs://readme", ttl=5.0, cache_scope="public"),
            read_ttl=120.0,
        )
    )
    async with read(client, "docs://readme") as resp:
        result = await result_of(resp)
    assert result["ttlMs"] == 5000
    assert result["cacheScope"] == "public"


async def test_request_context_and_overrides(make_client: MakeClient) -> None:
    async def status(request: MCPRequest) -> str:
        return f"serving {request.method}"

    resource = Resource(
        status,
        "app://status",
        name="server_status",
        title="Status",
        description="Live status.",
        icons=(Icon("https://example.com/icon.png"),),
        audience=("assistant",),
        priority=0.5,
        last_modified="2026-10-20T00:00:00Z",
    )
    client = await make_client(resources=ResourceRegistry(resource))
    async with post(client, "resources/list") as resp:
        info = (await result_of(resp))["resources"][0]
    assert info == {
        "uri": "app://status",
        "name": "server_status",
        "title": "Status",
        "description": "Live status.",
        "icons": [{"src": "https://example.com/icon.png"}],
        "annotations": {
            "audience": ["assistant"],
            "priority": 0.5,
            "lastModified": "2026-10-20T00:00:00Z",
        },
    }
    async with read(client, "app://status") as resp:
        result = await result_of(resp)
    assert result["contents"][0]["text"] == "serving resources/read"


@pytest.mark.parametrize(
    "resource",
    (
        lambda: Resource(project_file, "file://{path}/{path}"),  # duplicate var
        lambda: Resource(project_file, "file://{path:3}"),  # unsupported operator
        lambda: Resource(project_file, "file://{+path}", size=1),  # size on template
        lambda: Resource(readme, "file://{+path}"),  # params != variables
        lambda: Resource(project_file, "docs://static"),  # params != variables
    ),
)
async def test_invalid_definitions(resource: "Callable[[], Resource]") -> None:
    with pytest.raises(ValueError):
        ResourceRegistry(resource())


async def test_duplicate_uri() -> None:
    with pytest.raises(ValueError):
        ResourceRegistry(Resource(readme, "docs://readme"), Resource(logo, "docs://readme"))


async def test_registry_config_errors() -> None:
    with pytest.raises(ValueError):
        ResourceRegistry(ttl=-1.0)
    with pytest.raises(ValueError):
        ResourceRegistry(read_ttl=-1.0)


# - Subscriptions ------------------------------------------------------------


def listen(
    client: Client, *uris: str, request_id: object = "sub-1"
) -> AbstractAsyncContextManager[ClientResponse]:
    params: dict[str, object] = {"notifications": {"resourceSubscriptions": list(uris)}}
    body = request_body("subscriptions/listen", params, request_id=request_id)  # type: ignore[arg-type]
    return client.post(
        "/mcp", json=body, headers=request_headers("subscriptions/listen")
    )


async def test_subscription_updates(
    make_client: MakeClient, mcp_servers: list[MCPServer]
) -> None:
    client = await make_client(resources=registry())
    server = mcp_servers[0]
    async with listen(client, "docs://readme") as resp:
        async with asyncio.timeout(5):
            ack = await next_sse_data(resp)
            assert ack["params"]["notifications"] == {
                "resourceSubscriptions": ["docs://readme"]
            }

            server.resource_updated("docs://readme")
            update = await next_sse_data(resp)
    assert update["method"] == "notifications/resources/updated"
    assert update["params"]["uri"] == "docs://readme"
    assert update["params"]["_meta"][META_SUBSCRIPTION_ID] == "sub-1"


async def test_update_only_for_subscribed_uris(
    make_client: MakeClient, mcp_servers: list[MCPServer]
) -> None:
    client = await make_client(resources=registry())
    server = mcp_servers[0]
    async with listen(client, "docs://readme") as resp:
        async with asyncio.timeout(5):
            await next_sse_data(resp)  # ack
            server.resource_updated("docs://logo")  # not subscribed
            server.resource_updated("docs://readme")
            update = await next_sse_data(resp)
    assert update["params"]["uri"] == "docs://readme"


async def test_concurrent_subscriptions_demultiplexed(
    make_client: MakeClient, mcp_servers: list[MCPServer]
) -> None:
    client = await make_client(resources=registry())
    server = mcp_servers[0]
    async with (
        listen(client, "docs://readme", request_id="a") as resp_a,
        listen(client, "docs://readme", "docs://logo", request_id="b") as resp_b,
        asyncio.timeout(5)
    ):
        await next_sse_data(resp_a)
        await next_sse_data(resp_b)
        server.resource_updated("docs://logo")
        update_b = await next_sse_data(resp_b)
        assert update_b["params"]["_meta"][META_SUBSCRIPTION_ID] == "b"
        server.resource_updated("docs://readme")
        assert (await next_sse_data(resp_a))["params"]["_meta"][META_SUBSCRIPTION_ID] == "a"


async def test_subscriptions_not_honored_without_resources(make_client: MakeClient) -> None:
    client = await make_client()
    async with listen(client, "docs://readme") as resp:
        async with asyncio.timeout(5):
            ack = await next_sse_data(resp)
    assert ack["params"]["notifications"] == {}


async def test_graceful_shutdown_closes_stream(make_client: MakeClient) -> None:
    client = await make_client(resources=registry())
    async with listen(client, "docs://readme") as resp:
        async with asyncio.timeout(5):
            await next_sse_data(resp)  # ack
            assert client.app is not None
            await client.app.shutdown()
            final = await next_sse_data(resp)
    assert final["id"] == "sub-1"
    assert final["result"]["resultType"] == "complete"


# - Embedding registered resources -------------------------------------------


def tool_call(client: Client, name: str) -> AbstractAsyncContextManager[ClientResponse]:
    body = request_body("tools/call", {"name": name})
    headers = request_headers("tools/call")
    headers["Mcp-Name"] = name
    return client.post("/mcp", json=body, headers=headers)


async def test_embed_resource(make_client: MakeClient) -> None:
    """A tool embeds a registered resource without duplicating its contents."""

    async def attach_readme(request: MCPRequest) -> list[EmbeddedResource]:
        return await request.embed_resource("docs://readme")

    client = await make_client(resources=registry(), tools=ToolRegistry(attach_readme))
    async with tool_call(client, "attach_readme") as resp:
        result = await result_of(resp)
    assert result["content"] == [
        {
            "type": "resource",
            "resource": {
                "uri": "docs://readme",
                "mimeType": "text/markdown",
                "text": "# aiohttp-mcp",
            },
        }
    ]


async def test_embed_template_and_annotations(make_client: MakeClient) -> None:
    async def doc_prompt(request: MCPRequest) -> list[Content]:
        return [
            TextContent("Use this file:"),
            *await request.embed_resource("file:///src/main.rs"),
        ]

    client = await make_client(
        resources=ResourceRegistry(
            Resource(
                project_file,
                "file:///{+path}",
                mime_type="text/plain",
                audience=("assistant",),
                priority=0.9,
            )
        ),
        prompts=PromptRegistry(doc_prompt),
    )
    body = request_body("prompts/get", {"name": "doc_prompt"})
    headers = request_headers("prompts/get")
    headers["Mcp-Name"] = "doc_prompt"
    async with client.post("/mcp", json=body, headers=headers) as resp:
        result = await result_of(resp)
    embedded = result["messages"][1]["content"]
    assert embedded == {
        "type": "resource",
        "resource": {
            "uri": "file:///src/main.rs",
            "text": "contents of src/main.rs",
        },
        "annotations": {"audience": ["assistant"], "priority": 0.9},
    }


async def test_embed_unknown_uri_is_server_error(make_client: MakeClient) -> None:
    async def broken(request: MCPRequest) -> str:
        await request.embed_resource("docs://missing")
        assert False

    client = await make_client(resources=registry(), tools=ToolRegistry(broken))
    async with tool_call(client, "broken") as resp:
        assert resp.status == 500
        assert (await resp.json())["error"]["code"] == -32603


async def test_embed_without_resources_configured(make_client: MakeClient) -> None:
    async def broken(request: MCPRequest) -> str:
        await request.embed_resource("docs://readme")
        assert False

    client = await make_client(tools=ToolRegistry(broken))
    async with tool_call(client, "broken") as resp:
        assert resp.status == 500
