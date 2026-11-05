from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient

from _helpers import MakeClient, request_body, request_headers
from aiohttp_mcp import (
    Content,
    Doc,
    Icon,
    ImageContent,
    MCPRequest,
    Message,
    Prompt,
    PromptRegistry,
    TextContent,
)


async def code_review(
    code: Annotated[str, Doc("The code to review")],
    focus: Annotated[str, Doc("What to focus the review on")] = "general quality",
) -> str:
    """Request Code Review.

    Asks the LLM to analyse code quality and suggest improvements.
    """
    return f"Please review this code, focusing on {focus}:\n\n{code}"


async def plain(text: str) -> str:
    return text[::-1]


def post(
    client: TestClient[web.Request, web.Application],
    method: str,
    params: dict[str, object] | None = None,
    *,
    name: str | None = None,
    token: str | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    headers = request_headers(method)
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=request_body(method, params), headers=headers)


def get_prompt(
    client: TestClient[web.Request, web.Application],
    name: str,
    arguments: dict[str, str] | None = None,
    *,
    token: str | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    params: dict[str, object] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return post(client, "prompts/get", params, name=name, token=token)


async def result_of(resp: ClientResponse) -> dict[str, Any]:
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert isinstance(body["result"], dict)
    return body["result"]


async def test_list(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    async with post(client, "prompts/list") as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 600_000
    assert result["cacheScope"] == "private"
    assert "nextCursor" not in result
    assert result["prompts"] == [
        {
            "name": "code_review",
            "title": "Request Code Review.",
            "description": "Asks the LLM to analyse code quality and suggest improvements.",
            "arguments": [
                {"name": "code", "description": "The code to review", "required": True},
                {"name": "focus", "description": "What to focus the review on"},
            ],
        }
    ]


async def test_single_line_docstring_is_title(make_client: MakeClient) -> None:
    async def shout(text: str) -> str:
        """Shout the text back."""
        assert False

    client = await make_client(prompts=PromptRegistry(shout))
    async with post(client, "prompts/list") as resp:
        (prompt,) = (await result_of(resp))["prompts"]
    assert prompt["title"] == "Shout the text back."
    assert "description" not in prompt


async def test_capability_advertised(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    async with post(client, "server/discover") as resp:
        result = await result_of(resp)
    assert result["capabilities"] == {"prompts": {}}


async def test_no_prompts_configured(make_client: MakeClient) -> None:
    client = await make_client()
    async with post(client, "server/discover") as resp:
        assert (await result_of(resp))["capabilities"] == {}
    async with post(client, "prompts/list") as resp:
        assert resp.status == 404
        assert (await resp.json())["error"]["code"] == -32601


async def test_list_config(make_client: MakeClient) -> None:
    client = await make_client(
        prompts=PromptRegistry(code_review, ttl=1.5, cache_scope="public")
    )
    async with post(client, "prompts/list") as resp:
        result = await result_of(resp)
    assert result["ttlMs"] == 1500
    assert result["cacheScope"] == "public"


async def test_list_rejects_cursor(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    async with post(client, "prompts/list", {"cursor": ""}) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_get_string_result(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    async with get_prompt(client, "code_review", {"code": "1 + 1"}) as resp:
        result = await result_of(resp)
    assert result["description"] == (
        "Asks the LLM to analyse code quality and suggest improvements."
    )
    assert result["messages"] == [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": "Please review this code, focusing on general quality:\n\n1 + 1",
            },
        }
    ]


async def test_args_override_default(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    arguments = {"code": "x", "focus": "security"}
    async with get_prompt(client, "code_review", arguments) as resp:
        result = await result_of(resp)
    messages = result["messages"]
    assert isinstance(messages, list)
    assert "focusing on security" in messages[0]["content"]["text"]


async def test_get_message_seq_result(make_client: MakeClient) -> None:
    async def dialogue() -> tuple[Message, ...]:
        return (
            Message(TextContent("Hello")),
            Message(TextContent("Hi!"), role="assistant"),
        )

    client = await make_client(prompts=PromptRegistry(dialogue))
    async with get_prompt(client, "dialogue") as resp:
        result = await result_of(resp)
    assert result["messages"] == [
        {"role": "user", "content": {"type": "text", "text": "Hello"}},
        {"role": "assistant", "content": {"type": "text", "text": "Hi!"}},
    ]
    assert "description" not in result


async def test_get_content_list_result(make_client: MakeClient) -> None:
    """Bare content blocks become user messages."""

    async def show_image() -> list[Content]:
        return [ImageContent(b"\x89PNG", "image/png"), TextContent("Check the image")]

    client = await make_client(prompts=PromptRegistry(show_image))
    async with get_prompt(client, "show_image") as resp:
        result = await result_of(resp)
    assert result["messages"] == [
        {
            "role": "user",
            "content": {"type": "image", "data": "iVBORw==", "mimeType": "image/png"},
        },
        {"role": "user", "content": {"type": "text", "text": "Check the image"}},
    ]


async def test_request_context_param(make_client: MakeClient) -> None:
    async def whoami(request: MCPRequest, greeting: str = "Hello") -> str:
        assert request.method == "prompts/get"
        return f"{greeting}, {request.http.remote or 'unknown'}"

    client = await make_client(prompts=PromptRegistry(whoami))
    async with get_prompt(client, "whoami") as resp:
        result = await result_of(resp)
    # The request parameter is not advertised as a prompt argument.
    async with post(client, "prompts/list") as resp:
        prompts = (await result_of(resp))["prompts"]
    assert prompts[0]["arguments"] == [{"name": "greeting"}]
    messages = result["messages"]
    assert messages[0]["content"]["text"].startswith("Hello, ")


async def test_foreign_annotated_metadata_ignored(make_client: MakeClient) -> None:
    """Only Doc() marks a description; other metadata must not be picked up."""

    class OtherLibraryMarker:
        """Dummy class."""

    async def mixed(
        text: Annotated[str, OtherLibraryMarker(), "not a description", Doc("The text")],
        plain_str_meta: Annotated[str, "bare strings are not descriptions"] = "x",
    ) -> str:
        return text

    client = await make_client(prompts=PromptRegistry(mixed))
    async with post(client, "prompts/list") as resp:
        prompts = (await result_of(resp))["prompts"]
    assert prompts[0]["arguments"] == [
        {"name": "text", "description": "The text", "required": True},
        {"name": "plain_str_meta"},
    ]


async def test_unknown_prompt(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    async with get_prompt(client, "nope") as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_missing_required_argument(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    async with get_prompt(client, "code_review", {"focus": "style"}) as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32602
    assert "code" in error["message"]


async def test_non_string_argument(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(code_review))
    params: dict[str, object] = {"name": "code_review", "arguments": {"code": 5}}
    async with post(client, "prompts/get", params, name="code_review") as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == -32602


async def test_unrecognised_argument_ignored(make_client: MakeClient) -> None:
    client = await make_client(prompts=PromptRegistry(plain))
    arguments = {"text": "abc", "surprise": "ignored"}
    async with get_prompt(client, "plain", arguments) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "cba"


async def test_prompt_overrides(make_client: MakeClient) -> None:
    prompt = Prompt(
        plain,
        name="reverse",
        title="Reverse Text",
        description="Reverses the input.",
        icons=(Icon("https://example.com/icon.png", sizes=("48x48",)),),
    )
    client = await make_client(prompts=PromptRegistry(prompt))
    async with post(client, "prompts/list") as resp:
        prompts = (await result_of(resp))["prompts"]
    assert prompts == [
        {
            "name": "reverse",
            "title": "Reverse Text",
            "description": "Reverses the input.",
            "arguments": [{"name": "text", "required": True}],
            "icons": [{"src": "https://example.com/icon.png", "sizes": ["48x48"]}],
        }
    ]
    async with get_prompt(client, "reverse", {"text": "ab"}) as resp:
        assert (await result_of(resp))["messages"][0]["content"]["text"] == "ba"


async def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError):
        PromptRegistry(plain, Prompt(plain))


async def test_registry_rejects_bad_signatures() -> None:
    async def bad_type(count: int) -> str:
        assert False

    async def var_args(*args: str) -> str:
        assert False

    async def positional_only(text: str, /) -> str:
        assert False

    for fn in (bad_type, var_args, positional_only):
        with pytest.raises(ValueError):
            PromptRegistry(fn)
    with pytest.raises(ValueError):
        PromptRegistry(plain, ttl=-1.0)


async def test_unannotated_params_are_strings(make_client: MakeClient) -> None:
    async def shout(text, punctuation="!") -> str:  # type: ignore[no-untyped-def]
        return f"{text.upper()}{punctuation}"

    client = await make_client(prompts=PromptRegistry(shout))
    async with post(client, "prompts/list") as resp:
        prompts = (await result_of(resp))["prompts"]
    assert prompts[0]["arguments"] == [
        {"name": "text", "required": True},
        {"name": "punctuation"},
    ]
    async with get_prompt(client, "shout", {"text": "hi"}) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "HI!"


async def test_unannotated_first_param_is_an_argument(make_client: MakeClient) -> None:
    """Only an explicit MCPRequest annotation makes a parameter the request."""

    async def echo(request, text) -> str:  # type: ignore[no-untyped-def]
        return f"{request}/{text}"

    client = await make_client(prompts=PromptRegistry(echo))
    async with post(client, "prompts/list") as resp:
        prompts = (await result_of(resp))["prompts"]
    assert [a["name"] for a in prompts[0]["arguments"]] == ["request", "text"]
    arguments = {"request": "a", "text": "b"}
    async with get_prompt(client, "echo", arguments) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "a/b"




def test_message_roles() -> None:
    assert Message(TextContent("hi"))._to_wire() == {
        "role": "user",
        "content": {"type": "text", "text": "hi"},
    }
    assert Message(TextContent("yo"), role="assistant")._to_wire() == {
        "role": "assistant",
        "content": {"type": "text", "text": "yo"},
    }
