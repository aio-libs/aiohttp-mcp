import functools
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest import mock

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient
from pytest_aiohttp import AiohttpClient

from _helpers import MakeClient, request_body, request_headers
from aiohttp_mcp import (
    Authorization,
    BooleanSchema,
    EnumSchema,
    FormElicitation,
    InputRequired,
    IntegerSchema,
    MCPRequest,
    MultiSelectEnumSchema,
    NumberSchema,
    PromptRegistry,
    Resource,
    ResourceRegistry,
    StringSchema,
    TokenInfo,
    ToolRegistry,
    URLElicitation,
    setup,
)
from aiohttp_mcp._transport import Method

FORM_CAPS: dict[str, object] = {"elicitation": {"form": {}}}
ALL_CAPS: dict[str, object] = {"elicitation": {"form": {}, "url": {}}}

CONFIRM = FormElicitation(
    {
        "seat": StringSchema(description="Preferred seat", min_length=2, max_length=3),
        "bags": IntegerSchema(minimum=0, maximum=2, default=0, required=False),
    },
    message="Confirm the booking?",
)


async def book_flight(request: MCPRequest, date: str) -> str:
    confirm = await request.elicit("confirm", CONFIRM, state={"quote": 42})
    if confirm.action != "accept":
        return f"Not booked ({confirm.action})."
    assert confirm.content is not None
    assert request.state == {"quote": 42}
    return f"Booked {date}, seat {confirm.content['seat']}, quote {request.state['quote']}."


async def connect(request: MCPRequest) -> str:
    link = await request.elicit(
        "grant", URLElicitation("https://ext.example/connect", message="Connect your account.")
    )
    if link.action != "accept":
        return "Not connected."
    return "Connected."


def mrtr_post(
    client: TestClient[web.Request, web.Application],
    method: str,
    params: dict[str, object],
    name: str,
    *,
    capabilities: dict[str, object] | None = None,
    input_responses: dict[str, object] | None = None,
    request_state: str | None = None,
    token: str | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    """POST to an MRTR-enabled method (``prompts/get``/``resources/read``)."""
    params = dict(params)
    if input_responses is not None:
        params["inputResponses"] = input_responses
    if request_state is not None:
        params["requestState"] = request_state
    headers = request_headers(method)
    headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = request_body(
        method,
        params,
        capabilities=capabilities if capabilities is not None else ALL_CAPS,
    )
    return client.post("/mcp", json=body, headers=headers)


def get_prompt(
    client: TestClient[web.Request, web.Application],
    name: str,
    arguments: dict[str, str] | None = None,
    **kwargs: Any,
) -> AbstractAsyncContextManager[ClientResponse]:
    params: dict[str, object] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return mrtr_post(client, "prompts/get", params, name, **kwargs)


async def result_of(resp: ClientResponse) -> dict[str, Any]:
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert isinstance(body["result"], dict)
    return body["result"]


async def first_round(
    client: TestClient[web.Request, web.Application], **kwargs: Any
) -> tuple[dict[str, Any], str]:
    """Initial prompts/get: assert input_required and return (requests, state)."""
    async with get_prompt(client, "book_flight", {"date": "2026-11-01"}, **kwargs) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    state = result["requestState"]
    assert isinstance(state, str)
    return result.get("inputRequests", {}), state


ACCEPT: dict[str, object] = {"action": "accept", "content": {"seat": "12A"}}


# The MRTR-enabled methods elicitation can be exercised through: setup()
# kwargs, method, request params, Mcp-Name value, and the final-result text.
TARGETS: "dict[str, tuple[Callable[[], dict[str, Any]], str, dict[str, object], str, Callable[[dict[str, Any]], object]]]" = {
    "prompt": (
        lambda: {"prompts": PromptRegistry(book_flight)},
        "prompts/get",
        {"name": "book_flight", "arguments": {"date": "2026-11-01"}},
        "book_flight",
        lambda result: result["messages"][0]["content"]["text"],
    ),
    "resource": (
        # The same handler serves as a resource template.
        lambda: {"resources": ResourceRegistry(Resource(book_flight, "flights://{date}"))},
        "resources/read",
        {"uri": "flights://2026-11-01"},
        "flights://2026-11-01",
        lambda result: result["contents"][0]["text"],
    ),
    "tool": (
        lambda: {"tools": ToolRegistry(book_flight)},
        "tools/call",
        {"name": "book_flight", "arguments": {"date": "2026-11-01"}},
        "book_flight",
        lambda result: result["content"][0]["text"],
    ),
}


@pytest.mark.parametrize("target", TARGETS)
async def test_round_trip_on_each_mrtr_method(make_client: MakeClient, target: str) -> None:
    """The elicitation round trip works on every MRTR-enabled method."""
    make_kwargs, method, params, name, extract = TARGETS[target]
    client = await make_client(**make_kwargs())

    async with mrtr_post(client, method, params, name) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    assert list(result["inputRequests"]) == ["confirm"]

    async with mrtr_post(
        client,
        method,
        params,
        name,
        input_responses={"confirm": ACCEPT},
        request_state=result["requestState"],
    ) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    assert extract(result) == "Booked 2026-11-01, seat 12A, quote 42."


async def test_input_required_result(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    requests, _state = await first_round(client)
    assert requests == {
        "confirm": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Confirm the booking?",
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "seat": {
                            "type": "string",
                            "description": "Preferred seat",
                            "minLength": 2,
                            "maxLength": 3,
                        },
                        "bags": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 2,
                            "default": 0,
                        },
                    },
                    "required": ["seat"],
                },
            },
        }
    }


async def test_retry_completes(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    _requests, state = await first_round(client)
    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": ACCEPT},
        request_state=state,
    ) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    text = result["messages"][0]["content"]["text"]
    assert text == "Booked 2026-11-01, seat 12A, quote 42."


@pytest.mark.parametrize("action", ("decline", "cancel"))
async def test_decline_and_cancel(make_client: MakeClient, action: str) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    _requests, state = await first_round(client)
    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": {"action": action}},
        request_state=state,
    ) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == f"Not booked ({action})."


async def test_tampered_state(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    _requests, state = await first_round(client)
    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": ACCEPT},
        request_state=state[:-4],
    ) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_expired_state(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    with mock.patch("aiohttp_mcp._elicitation._STATE_TTL", -1.0):
        _requests, state = await first_round(client)
    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": ACCEPT},
        request_state=state,
    ) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_state_bound_to_request(make_client: MakeClient) -> None:
    """Replaying state on a retry with different params must be rejected."""
    client = await make_client(prompts=PromptRegistry(book_flight))
    _requests, state = await first_round(client)
    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-12-24"},  # not the original request
        input_responses={"confirm": ACCEPT},
        request_state=state,
    ) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_retry_without_state(make_client: MakeClient) -> None:
    async def ask(request: MCPRequest) -> str:
        answer = await request.elicit(
            "name", FormElicitation({"name": StringSchema()}, message="Who?")
        )
        assert answer.content is not None
        return str(answer.content["name"])

    client = await make_client(prompts=PromptRegistry(ask))
    async with get_prompt(
        client,
        "ask",
        input_responses={"name": {"action": "accept", "content": {"name": "sam"}}},
    ) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "sam"


async def test_progressive_elicitation(make_client: MakeClient) -> None:
    """Consecutive elicits accumulate responses across round trips."""

    async def two_steps(request: MCPRequest) -> str:
        first = await request.elicit("first", FormElicitation({"a": StringSchema()}, message="A?"))
        second = await request.elicit("second", FormElicitation({"b": StringSchema()}, message="B?"))
        assert first.content is not None and second.content is not None
        return f"{first.content['a']}{second.content['b']}"

    client = await make_client(prompts=PromptRegistry(two_steps))

    async with get_prompt(client, "two_steps") as resp:
        result = await result_of(resp)
    assert tuple(result["inputRequests"]) == ("first",)

    async with get_prompt(
        client,
        "two_steps",
        input_responses={"first": {"action": "accept", "content": {"a": "x"}}},
        request_state=result["requestState"],
    ) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    assert tuple(result["inputRequests"]) == ("second",)

    async with get_prompt(
        client,
        "two_steps",
        input_responses={"second": {"action": "accept", "content": {"b": "y"}}},
        request_state=result["requestState"],
    ) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "xy"


async def test_no_elicitation_capability(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    async with get_prompt(
        client, "book_flight", {"date": "2026-11-01"}, capabilities={}
    ) as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32021
    assert error["data"] == {"requiredCapabilities": {"elicitation": {"form": {}}}}


async def test_empty_elicitation_capability_is_form(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight, connect))
    capabilities: dict[str, object] = {"elicitation": {}}
    async with get_prompt(
        client, "book_flight", {"date": "2026-11-01"}, capabilities=capabilities
    ) as resp:
        assert (await result_of(resp))["resultType"] == "input_required"

    # url is not supported
    params: dict[str, object] = {"name": "connect"}
    headers = request_headers("prompts/get")
    headers["Mcp-Name"] = "connect"
    body = request_body("prompts/get", params, capabilities=capabilities)
    async with client.post("/mcp", json=body, headers=headers) as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32021
    assert error["data"] == {"requiredCapabilities": {"elicitation": {"url": {}}}}


async def test_url_mode(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(connect))
    async with get_prompt(client, "connect") as resp:
        result = await result_of(resp)
    assert result["inputRequests"]["grant"] == {
        "method": "elicitation/create",
        "params": {
            "mode": "url",
            "message": "Connect your account.",
            "url": "https://ext.example/connect",
        },
    }
    async with get_prompt(
        client,
        "connect",
        input_responses={"grant": {"action": "accept"}},
        request_state=result["requestState"],
    ) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "Connected."


async def test_state_only_result(make_client: MakeClient) -> None:
    async def poll(request: MCPRequest) -> str:
        attempt = request.state["n"] if isinstance(request.state, dict) else 0
        assert isinstance(attempt, int)
        if attempt < 2:
            raise InputRequired(state={"n": attempt + 1})
        return f"Done after {attempt} polls."

    client = await make_client(prompts=PromptRegistry(poll))
    async with get_prompt(client, "poll") as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    assert "inputRequests" not in result
    async with get_prompt(client, "poll", request_state=result["requestState"]) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    async with get_prompt(client, "poll", request_state=result["requestState"]) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "Done after 2 polls."


@pytest.mark.parametrize(
    "response",
    (
        "not-a-mapping",
        {"action": "maybe"},
        {"action": "accept"},  # missing content for a form
        {"action": "accept", "content": {"seat": 12}},  # wrong type
        {"action": "accept", "content": {}},  # missing required field
        {"action": "accept", "content": {"seat": "A"}},  # below min_length
        {"action": "accept", "content": {"seat": "12A", "bags": 3}},  # above maximum
        {"action": "accept", "content": {"seat": "12A", "bags": True}},  # bool not int
    ),
)
async def test_invalid_responses(make_client: MakeClient, response: object) -> None:
    client = await make_client(prompts=PromptRegistry(book_flight))
    _requests, state = await first_round(client)
    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": response},
        request_state=state,
    ) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_input_required_on_unsupported_method(aiohttp_client: AiohttpClient) -> None:
    """Raising InputRequired outside prompts/get is an internal error."""
    async def raising(ctx: MCPRequest) -> None:
        raise InputRequired(state={"n": 1})

    app = web.Application()
    server = setup(app)
    server._methods["test/raise"] = Method(raising)
    client = await aiohttp_client(app)

    body = request_body("test/raise", capabilities=ALL_CAPS)
    headers = request_headers("test/raise")
    async with client.post("/mcp", json=body, headers=headers) as resp:
        assert resp.status == 500
        assert (await resp.json())["error"]["code"] == -32603


async def test_elicitation_validation() -> None:
    with pytest.raises(ValueError):
        URLElicitation("not-a-url", message="msg")
    with pytest.raises(ValueError):
        URLElicitation("ftp://x.example/path", message="msg")
    with pytest.raises(ValueError):
        FormElicitation({}, message="msg")
    with pytest.raises(ValueError):
        EnumSchema(options=())
    with pytest.raises(ValueError):
        MultiSelectEnumSchema(options=())


def test_enum_schema_wire() -> None:
    assert EnumSchema(options=("Red", "Green"))._to_wire() == {
        "type": "string",
        "enum": ("Red", "Green"),
    }
    assert EnumSchema(options={"#F00": "Red"}, title="Colour")._to_wire() == {
        "type": "string",
        "title": "Colour",
        "oneOf": ({"const": "#F00", "title": "Red"},),
    }
    assert MultiSelectEnumSchema(
        options=("Red", "Green"), min_items=1, max_items=2
    )._to_wire() == {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "items": {"type": "string", "enum": ("Red", "Green")},
    }
    assert MultiSelectEnumSchema(options={"#F00": "Red"}, default=("#F00",))._to_wire() == {
        "type": "array",
        "items": {"anyOf": ({"const": "#F00", "title": "Red"},)},
        "default": ("#F00",),
    }


def test_schema_validation() -> None:
    assert MultiSelectEnumSchema(options=("a", "b"))._validate(["a"]) == ("a",)
    with pytest.raises(ValueError):
        EnumSchema(options=("a", "b"))._validate("c")
    with pytest.raises(ValueError):
        EnumSchema(options=("a", "b"))._validate(["a"])
    with pytest.raises(ValueError):
        MultiSelectEnumSchema(options=("a",))._validate("a")
    with pytest.raises(ValueError):
        MultiSelectEnumSchema(options=("a", "b"), min_items=2)._validate(["a"])
    with pytest.raises(ValueError):
        NumberSchema()._validate(True)
    assert NumberSchema()._validate(1) == 1
    assert BooleanSchema()._validate(False) is False
    with pytest.raises(ValueError):
        BooleanSchema()._validate("true")


# - State bound to the authenticated principal ------------------------------

TOKENS = {
    "alice-token": TokenInfo(scopes=frozenset({"mcp"}), subject="alice"),
    "bob-token": TokenInfo(scopes=frozenset({"mcp"}), subject="bob"),
}


async def validator(token: str, request: web.Request) -> TokenInfo | None:
    return TOKENS.get(token)


make_auth = functools.partial(
    Authorization,
    validator,
    resource="https://mcp.server.test",
    auth_servers=("https://auth.server.test",),
    default_scopes=("mcp",),
)


async def test_state_bound_to_principal(make_client: MakeClient) -> None:
    client = await make_client(
        authorization=make_auth(), prompts=PromptRegistry(book_flight)
    )
    _requests, state = await first_round(client, token="alice-token")

    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": ACCEPT},
        request_state=state,
        token="bob-token",
    ) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602

    async with get_prompt(
        client,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": ACCEPT},
        request_state=state,
        token="alice-token",
    ) as resp:
        assert (await result_of(resp))["resultType"] == "complete"


async def test_stable_secret_across_instances(make_client: MakeClient) -> None:
    secret = b"s" * 32
    client_a = await make_client(
        prompts=PromptRegistry(book_flight), state_secret=secret
    )
    client_b = await make_client(
        prompts=PromptRegistry(book_flight), state_secret=secret
    )
    _requests, state = await first_round(client_a)
    async with get_prompt(
        client_b,
        "book_flight",
        {"date": "2026-11-01"},
        input_responses={"confirm": ACCEPT},
        request_state=state,
    ) as resp:
        assert (await result_of(resp))["resultType"] == "complete"
