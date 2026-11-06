"""x-mcp-header: schema emission and Mcp-Param-* server validation."""

from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient
from multidict import CIMultiDict

from _helpers import MakeClient, request_body, request_headers
from aiohttp_mcp import Doc, Header, ToolRegistry

Client = TestClient[web.Request, web.Application]


async def execute_sql(
    region: Annotated[str, Doc("The region to execute the query in"), Header("Region")],
    query: str,
) -> str:
    """Execute SQL."""
    return f"ran {query!r} in {region}"


async def paginate(
    cursor: Annotated[str | None, Header("Cursor")] = None,
    limit: Annotated[int, Header("Limit")] = 10,
    dry_run: Annotated[bool, Header("Dry-Run")] = False,
) -> str:
    return f"{cursor}:{limit}:{dry_run}"


def call(
    client: Client,
    name: str,
    arguments: dict[str, object],
    param_headers: dict[str, str] | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    headers = CIMultiDict(request_headers("tools/call"))
    headers["Mcp-Name"] = name
    for key, value in (param_headers or {}).items():
        headers.add(f"Mcp-Param-{key}", value)
    body = request_body("tools/call", {"name": name, "arguments": arguments})
    return client.post("/mcp", json=body, headers=headers)


async def result_of(resp: ClientResponse) -> dict[str, Any]:
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert isinstance(body["result"], dict)
    return body["result"]


async def assert_mismatch(resp: ClientResponse) -> None:
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == -32020


async def test_schema_annotation(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    body = request_body("tools/list")
    async with client.post("/mcp", json=body, headers=request_headers("tools/list")) as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"]["region"] == {
        "type": "string",
        "description": "The region to execute the query in",
        "x-mcp-header": "Region",
    }
    assert "x-mcp-header" not in schema["properties"]["query"]


async def test_matching_header(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    arguments: dict[str, object] = {"region": "us-west1", "query": "SELECT 1"}
    async with call(client, "execute_sql", arguments, {"Region": "us-west1"}) as resp:
        result = await result_of(resp)
    assert "us-west1" in result["content"][0]["text"]


async def test_missing_header_rejected(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    arguments: dict[str, object] = {"region": "us-west1", "query": "SELECT 1"}
    async with call(client, "execute_sql", arguments) as resp:
        await assert_mismatch(resp)


async def test_mismatched_header_rejected(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    arguments: dict[str, object] = {"region": "us-west1", "query": "SELECT 1"}
    async with call(client, "execute_sql", arguments, {"Region": "eu-west1"}) as resp:
        await assert_mismatch(resp)


async def test_header_for_absent_parameter_rejected(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(paginate))
    async with call(client, "paginate", {}, {"Cursor": "abc"}) as resp:
        await assert_mismatch(resp)


async def test_null_parameter_omits_header(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(paginate))
    async with call(client, "paginate", {"cursor": None}) as resp:
        assert resp.status == 200
    async with call(client, "paginate", {"cursor": None}, {"Cursor": "abc"}) as resp:
        await assert_mismatch(resp)


async def test_repeated_header_rejected(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    arguments: dict[str, object] = {"region": "us-west1", "query": "SELECT 1"}
    headers = {"Region": "us-west1"}
    req = call(client, "execute_sql", arguments, headers)
    # Build manually with a duplicated header.
    raw = CIMultiDict(request_headers("tools/call"))
    raw["Mcp-Name"] = "execute_sql"
    raw.add("Mcp-Param-Region", "us-west1")
    raw.add("Mcp-Param-Region", "us-west1")
    body = request_body("tools/call", {"name": "execute_sql", "arguments": arguments})
    async with client.post("/mcp", json=body, headers=raw) as resp:
        await assert_mismatch(resp)
    async with req as resp:
        assert resp.status == 200


async def test_base64_sentinel_value(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    arguments: dict[str, object] = {"region": "Hello, 世界", "query": "SELECT 1"}
    sentinel = "=?base64?SGVsbG8sIOS4lueVjA==?="
    async with call(client, "execute_sql", arguments, {"Region": sentinel}) as resp:
        assert resp.status == 200
    async with call(client, "execute_sql", arguments, {"Region": "=?base64?!bad!?="}) as resp:
        await assert_mismatch(resp)


async def test_numeric_and_boolean_comparison(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(paginate))
    headers = {"Limit": "42.0", "Dry-Run": "true"}
    async with call(client, "paginate", {"limit": 42, "dry_run": True}, headers) as resp:
        assert resp.status == 200
    async with call(client, "paginate", {"limit": 42}, {"Limit": "41"}) as resp:
        await assert_mismatch(resp)
    async with call(client, "paginate", {"dry_run": True}, {"Dry-Run": "True"}) as resp:
        await assert_mismatch(resp)  # the encoding is lowercase


async def test_unrecognized_param_headers_ignored(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(execute_sql))
    arguments: dict[str, object] = {"region": "us-west1", "query": "SELECT 1"}
    headers = {"Region": "us-west1", "Other": "whatever"}
    async with call(client, "execute_sql", arguments, headers) as resp:
        assert resp.status == 200


@pytest.mark.parametrize(
    "annotation",
    (
        Annotated[str, Header("")],  # empty
        Annotated[str, Header("Bad Name")],  # space is not a tchar
        Annotated[str, Header("X\r\nInjected")],  # control characters
        Annotated[float, Header("Rate")],  # float is not permitted
        Annotated[list[str], Header("Items")],  # not a primitive
    ),
)
async def test_invalid_header_definitions(annotation: object) -> None:
    async def bad(value) -> str:  # type: ignore[no-untyped-def]
        return ""

    bad.__annotations__["value"] = annotation
    with pytest.raises(ValueError):
        ToolRegistry(bad)


async def test_case_insensitive_uniqueness() -> None:
    async def clash(
        a: Annotated[str, Header("Region")], b: Annotated[str, Header("region")]
    ) -> str:
        return ""

    with pytest.raises(ValueError, match="duplicate x-mcp-header"):
        ToolRegistry(clash)
