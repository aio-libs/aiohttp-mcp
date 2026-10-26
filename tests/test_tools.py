from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any, Literal, NotRequired, TypedDict

import pytest
from _helpers import MakeClient, request_body, request_headers
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient

from aiohttp_mcp import (
    Doc,
    MCPRequest,
    TextContent,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
)

Client = TestClient[web.Request, web.Application]


class Forecast(TypedDict):
    temperature: float
    conditions: str
    humidity: NotRequired[int]


async def get_weather(
    location: Annotated[str, Doc("City name or zip code")],
    units: Literal["C", "F"] = "C",
) -> Forecast:
    """Weather Information Provider.

    Get current weather information for a location.
    """
    return {"temperature": 22.5 if units == "C" else 72.5, "conditions": "cloudy"}


async def echo(text: str) -> str:
    return text[::-1]


async def total(values: list[float], precision: int | None = None) -> float:
    result = sum(values)
    return round(result, precision) if precision is not None else result


def post(
    client: Client,
    method: str,
    params: dict[str, object] | None = None,
    *,
    name: str | None = None,
    capabilities: dict[str, object] | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    headers = request_headers(method)
    if name is not None:
        headers["Mcp-Name"] = name
    body = request_body(method, params, capabilities=capabilities)
    return client.post("/mcp", json=body, headers=headers)


def call(
    client: Client, name: str, arguments: dict[str, object] | None = None, **kwargs: Any
) -> AbstractAsyncContextManager[ClientResponse]:
    params: dict[str, object] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return post(client, "tools/call", params, name=name, **kwargs)


async def result_of(resp: ClientResponse) -> dict[str, Any]:
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert isinstance(body["result"], dict)
    return body["result"]


async def test_list(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(get_weather))
    async with post(client, "tools/list") as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 300_000
    assert result["cacheScope"] == "private"
    assert result["tools"] == [
        {
            "name": "get_weather",
            "title": "Weather Information Provider.",
            "description": "Get current weather information for a location.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or zip code",
                    },
                    "units": {"type": "string", "enum": ["C", "F"], "default": "C"},
                },
                "required": ["location"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "temperature": {"type": "number"},
                    "conditions": {"type": "string"},
                    "humidity": {"type": "integer"},
                },
                "required": ["conditions", "temperature"],
            },
        }
    ]


async def test_capability(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(echo))
    async with post(client, "server/discover") as resp:
        result = await result_of(resp)
    assert result["capabilities"]["tools"] == {}


async def test_no_tools_configured(make_client: MakeClient) -> None:
    client = await make_client()
    async with post(client, "server/discover") as resp:
        assert "tools" not in (await result_of(resp))["capabilities"]
    async with post(client, "tools/list") as resp:
        assert resp.status == 404


async def test_complex_input_schema(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(total))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"] == {
        "values": {"type": "array", "items": {"type": "number"}},
        "precision": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": None,
        },
    }
    assert schema["required"] == ["values"]


async def test_structured_result(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(get_weather))
    async with call(client, "get_weather", {"location": "York"}) as resp:
        result = await result_of(resp)
    assert result["structuredContent"] == {"temperature": 22.5, "conditions": "cloudy"}
    assert "isError" not in result


async def test_text_result(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(echo))
    async with call(client, "echo", {"text": "abc"}) as resp:
        result = await result_of(resp)
    assert result["content"] == [{"type": "text", "text": "cba"}]
    assert "structuredContent" not in result


async def test_scalar_structured_result(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(total))
    async with call(client, "total", {"values": [1.5, 2], "precision": 0}) as resp:
        result = await result_of(resp)
    assert result["structuredContent"] == 4
    async with post(client, "tools/list") as resp:
        assert (await result_of(resp))["tools"][0]["outputSchema"] == {"type": "number"}


async def test_unannotated_parameter_is_any(make_client: MakeClient) -> None:
    async def shout(text) -> str:  # type: ignore[no-untyped-def]
        return str(text).upper()

    client = await make_client(tools=ToolRegistry(shout))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"] == {"text": {}}
    for value, expected in (("hi", "HI"), (5, "5"), ([1], "[1]")):
        async with call(client, "shout", {"text": value}) as resp:
            result = await result_of(resp)
        assert result["content"] == [{"type": "text", "text": expected}]


async def test_any_and_object_parameters(make_client: MakeClient) -> None:
    async def record(event: Any, payload: Annotated[object, Doc("Raw data")]) -> str:
        return f"{event}:{payload}"

    client = await make_client(tools=ToolRegistry(record))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"] == {
        "event": {},
        "payload": {"description": "Raw data"},
    }
    async with call(client, "record", {"event": {"id": 1}, "payload": None}) as resp:
        result = await result_of(resp)
    assert result["content"] == [{"type": "text", "text": "{'id': 1}:None"}]


async def test_nested_any(make_client: MakeClient) -> None:
    async def store(blob: dict[str, Any]) -> str:
        return str(len(blob))

    client = await make_client(tools=ToolRegistry(store))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"]["blob"] == {"type": "object", "additionalProperties": {}}
    async with call(client, "store", {"blob": {"a": 1, "b": [True]}}) as resp:
        assert (await result_of(resp))["content"][0]["text"] == "2"
    async with call(client, "store", {"blob": "nope"}) as resp:
        assert (await result_of(resp))["isError"] is True


async def test_any_return_is_dynamic(make_client: MakeClient) -> None:
    async def flexible() -> Any:
        return "plain text"

    client = await make_client(tools=ToolRegistry(flexible))
    async with post(client, "tools/list") as resp:
        assert "outputSchema" not in (await result_of(resp))["tools"][0]
    async with call(client, "flexible") as resp:
        result = await result_of(resp)
    assert result["content"] == [{"type": "text", "text": "plain text"}]
    assert "structuredContent" not in result


async def test_no_output(make_client: MakeClient) -> None:
    async def restart_service(name: str) -> None:
        assert name == "db"

    client = await make_client(tools=ToolRegistry(restart_service))
    async with post(client, "tools/list") as resp:
        info = (await result_of(resp))["tools"][0]
    assert "outputSchema" not in info
    async with call(client, "restart_service", {"name": "db"}) as resp:
        result = await result_of(resp)
    assert result["content"] == []
    assert "structuredContent" not in result
    assert "isError" not in result


async def test_content_result(make_client: MakeClient) -> None:
    async def diagram() -> list[TextContent]:
        return [TextContent("block one"), TextContent("block two")]

    client = await make_client(tools=ToolRegistry(diagram))
    async with call(client, "diagram") as resp:
        result = await result_of(resp)
    assert result["content"] == [
        {"type": "text", "text": "block one"},
        {"type": "text", "text": "block two"},
    ]


async def test_integer_literal(make_client: MakeClient) -> None:
    async def zoom(level: Literal[1, 2, 4]) -> str:
        return f"x{level}"

    client = await make_client(tools=ToolRegistry(zoom))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"]["level"] == {"type": "integer", "enum": [1, 2, 4]}
    async with call(client, "zoom", {"level": 2}) as resp:
        result = await result_of(resp)
    assert result["content"] == [{"type": "text", "text": "x2"}]
    async with call(client, "zoom", {"level": 3}) as resp:
        assert (await result_of(resp))["isError"] is True
    # 1 == True in Python, but True is not a member of this enum.
    async with call(client, "zoom", {"level": True}) as resp:
        assert (await result_of(resp))["isError"] is True


async def test_mixed_literal(make_client: MakeClient) -> None:
    async def pick(choice: Literal["auto", 0, False]) -> str:
        return repr(choice)

    client = await make_client(tools=ToolRegistry(pick))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    # Heterogeneous members: an enum without a single type.
    assert schema["properties"]["choice"] == {"enum": ["auto", 0, False]}
    async with call(client, "pick", {"choice": False}) as resp:
        result = await result_of(resp)
    assert result["content"] == [{"type": "text", "text": "False"}]


async def test_variadic_tuple_return(make_client: MakeClient) -> None:
    async def fib() -> tuple[int, ...]:
        return (1, 1, 2, 3)

    client = await make_client(tools=ToolRegistry(fib))
    async with post(client, "tools/list") as resp:
        info = (await result_of(resp))["tools"][0]
    assert info["outputSchema"] == {"type": "array", "items": {"type": "integer"}}
    async with call(client, "fib") as resp:
        result = await result_of(resp)
    assert result["structuredContent"] == [1, 1, 2, 3]


async def test_fixed_tuple(make_client: MakeClient) -> None:
    async def locate(point: tuple[float, float]) -> str:
        return f"{point[0]},{point[1]}"

    client = await make_client(tools=ToolRegistry(locate))
    async with post(client, "tools/list") as resp:
        schema = (await result_of(resp))["tools"][0]["inputSchema"]
    assert schema["properties"]["point"] == {
        "type": "array",
        "prefixItems": [{"type": "number"}, {"type": "number"}],
        "items": False,
        "minItems": 2,
        "maxItems": 2,
    }
    async with call(client, "locate", {"point": [1.5, 2.5]}) as resp:
        result = await result_of(resp)
    assert result["content"] == [{"type": "text", "text": "1.5,2.5"}]
    async with call(client, "locate", {"point": [1.5]}) as resp:
        assert (await result_of(resp))["isError"] is True


async def test_sequence_return_accepts_tuple(make_client: MakeClient) -> None:
    async def names() -> Sequence[str]:
        return ("a", "b")  # a tuple is a perfectly good Sequence

    client = await make_client(tools=ToolRegistry(names))
    async with call(client, "names") as resp:
        result = await result_of(resp)
    assert result["structuredContent"] == ["a", "b"]


async def test_optional_output(make_client: MakeClient) -> None:
    """`-> Data | None` drops the null arm; None means no output."""

    async def check(ok: bool) -> Forecast | None:
        if ok:
            return {"temperature": 1.0, "conditions": "snow"}
        return None

    client = await make_client(tools=ToolRegistry(check))
    async with post(client, "tools/list") as resp:
        info = (await result_of(resp))["tools"][0]
    assert "anyOf" not in info["outputSchema"]
    assert info["outputSchema"]["type"] == "object"
    async with call(client, "check", {"ok": True}) as resp:
        assert "structuredContent" in await result_of(resp)
    async with call(client, "check", {"ok": False}) as resp:
        result = await result_of(resp)
    assert result == {"resultType": "complete", "content": [], "_meta": result["_meta"]}



async def test_content_annotations_on_results(make_client: MakeClient) -> None:
    async def report() -> ToolResult[Forecast]:
        return ToolResult(
            content=(TextContent("See the chart.", audience=("user",), priority=0.2),),
            structured={"temperature": 22.5, "conditions": "cloudy"},
        )

    client = await make_client(tools=ToolRegistry(report))
    async with call(client, "report") as resp:
        result = await result_of(resp)
    assert result["content"] == [
        {
            "type": "text",
            "text": "See the chart.",
            "annotations": {"audience": ["user"], "priority": 0.2},
        }
    ]
    assert result["structuredContent"] == {"temperature": 22.5, "conditions": "cloudy"}


async def test_bare_tool_result_is_unstructured(make_client: MakeClient) -> None:
    async def lookup() -> ToolResult[object]:
        return ToolResult(content=(TextContent("2 users"),), structured={"count": 2})

    client = await make_client(tools=ToolRegistry(lookup))
    async with post(client, "tools/list") as resp:
        assert "outputSchema" not in (await result_of(resp))["tools"][0]


async def test_structured_results_not_validated(make_client: MakeClient) -> None:
    """A badly formed response beats an internal error."""

    async def liar() -> ToolResult[Forecast]:
        return ToolResult(content=(TextContent("hot"),), structured={"temperature": "hot"})  # type: ignore[arg-type]

    client = await make_client(tools=ToolRegistry(liar))
    async with call(client, "liar") as resp:
        result = await result_of(resp)
    assert result["structuredContent"] == {"temperature": "hot"}


async def test_structured_error_result_skips_schema(make_client: MakeClient) -> None:
    async def flaky() -> ToolResult[Forecast | None]:
        return ToolResult(content=(TextContent("Upstream API is down."),), is_error=True)

    client = await make_client(tools=ToolRegistry(flaky))
    async with call(client, "flaky") as resp:
        result = await result_of(resp)
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert result["content"][0]["text"] == "Upstream API is down."


async def test_tool_error(make_client: MakeClient) -> None:
    async def book(date: str) -> str:
        raise ToolError("Invalid departure date: must be in the future.")

    client = await make_client(tools=ToolRegistry(book))
    async with call(client, "book", {"date": "2020-01-01"}) as resp:
        result = await result_of(resp)
    assert result["isError"] is True
    assert result["content"] == [
        {"type": "text", "text": "Invalid departure date: must be in the future."}
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        ({"location": 5}, "expected a string"),
        ({}, "Missing required arguments: location"),
        ({"location": "York", "units": "K"}, "not one of the allowed values"),
        ({"location": "York", "shoe_size": 9}, "Unexpected arguments: shoe_size"),
    ),
)
async def test_invalid_arguments_are_execution_errors(
    make_client: MakeClient, arguments: dict[str, object], message: str
) -> None:
    client = await make_client(tools=ToolRegistry(get_weather))
    async with call(client, "get_weather", arguments) as resp:
        result = await result_of(resp)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]


async def test_nested_argument_validation(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(total))
    async with call(client, "total", {"values": [1.0, "x"]}) as resp:
        result = await result_of(resp)
    assert result["isError"] is True
    assert "values[1]" in result["content"][0]["text"]


async def test_unknown_tool_is_protocol_error(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(echo))
    async with call(client, "nope") as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32602
    assert "Unknown tool" in error["message"]


async def test_unhandled_exception_is_internal_error(make_client: MakeClient) -> None:
    async def broken() -> str:
        raise RuntimeError("secret detail")

    client = await make_client(tools=ToolRegistry(broken))
    async with call(client, "broken") as resp:
        assert resp.status == 500
        error = (await resp.json())["error"]
    assert error["code"] == -32603
    assert "secret detail" not in error["message"]


async def test_request_context_and_overrides(make_client: MakeClient) -> None:
    async def whoami(request: MCPRequest) -> str:
        return f"via {request.method}"

    tool = Tool(
        whoami,
        name="who_am_i",
        title="Who Am I",
        description="Reports the calling method.",
        read_only=True,
        idempotent=True,
        open_world=False,
    )
    client = await make_client(tools=ToolRegistry(tool))
    async with post(client, "tools/list") as resp:
        info = (await result_of(resp))["tools"][0]
    assert info == {
        "name": "who_am_i",
        "title": "Who Am I",
        "description": "Reports the calling method.",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }
    async with call(client, "who_am_i") as resp:
        result = await result_of(resp)
    assert result["content"][0]["text"] == "via tools/call"


async def test_list_rejects_cursor(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(echo))
    async with post(client, "tools/list", {"cursor": ""}) as resp:
        assert (await resp.json())["error"]["code"] == -32602


async def test_deterministic_order(make_client: MakeClient) -> None:
    client = await make_client(tools=ToolRegistry(total, echo, get_weather))
    for _ in range(2):
        async with post(client, "tools/list") as resp:
            names = [tool["name"] for tool in (await result_of(resp))["tools"]]
        assert names == ["total", "echo", "get_weather"]


@pytest.mark.parametrize("name", ("has space", "naïve", "a" * 129, "comma,name"))
async def test_invalid_tool_names(name: str) -> None:
    with pytest.raises(ValueError, match="invalid tool name"):
        ToolRegistry(Tool(echo, name=name))


async def test_valid_tool_names() -> None:
    for name in ("getUser", "DATA_EXPORT_v2", "admin.tools.list", "a", "a" * 128):
        ToolRegistry(Tool(echo, name=name))


async def test_registry_config_errors() -> None:
    async def var_args(*args: str) -> str:
        return ""

    with pytest.raises(ValueError):
        ToolRegistry(var_args)
    with pytest.raises(ValueError):
        ToolRegistry(echo, echo)  # duplicate names
    with pytest.raises(ValueError):
        ToolRegistry(ttl=-1.0)


async def test_unrepresentable_literal_rejected() -> None:
    async def bad(mode: Literal[b'raw']) -> str:
        return ""

    with pytest.raises(ValueError):
        ToolRegistry(bad)
