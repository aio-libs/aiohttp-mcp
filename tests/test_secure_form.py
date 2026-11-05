import asyncio
import functools
import heapq
import logging
import time
from collections.abc import Awaitable, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest import mock

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient
from multidict import MultiDict
from pytest_aiohttp import AiohttpClient
from yarl import URL

import aiohttp_mcp
from _helpers import MakeClient, request_body, request_headers
from aiohttp_mcp import (
    AuthenticateRequest,
    Authorization,
    BooleanSchema,
    EnumSchema,
    FormContext,
    IntegerSchema,
    LocalAuthorization,
    MCPRequest,
    MultiSelectEnumSchema,
    PromptRegistry,
    RenderForm,
    SecureFormElicitation,
    StringSchema,
    TokenInfo,
)
from aiohttp_mcp._elicitation import _PendingForm

ALL_CAPS: dict[str, object] = {"elicitation": {"form": {}, "url": {}}}
TOKENS = {
    "alice-token": TokenInfo(scopes=frozenset({"mcp"}), subject="alice"),
    "bob-token": TokenInfo(scopes=frozenset({"mcp"}), subject="bob"),
}

API_FORM = SecureFormElicitation(
    {
        "api_key": StringSchema(min_length=8),
        "region": EnumSchema(options={"eu": "Europe", "us": "United States"}, required=False),
        "retries": IntegerSchema(minimum=0, maximum=5, required=False),
        "sandbox": BooleanSchema(required=False),
        "tags": MultiSelectEnumSchema(options=("a", "b"), min_items=1, required=False),
    },
    message="Enter your API credentials.",
)


async def set_key(request: MCPRequest) -> str:
    result = await request.elicit("creds", API_FORM)
    if result.action != "accept":
        return f"No credentials ({result.action})."
    assert result.content is not None
    return repr(dict(result.content))


async def validator(token: str, request: web.Request) -> TokenInfo | None:
    return TOKENS.get(token)


async def browser_user(request: web.Request) -> str | None:
    return request.headers.get("X-Test-User")


make_auth = functools.partial(
    Authorization,
    validator,
    resource="https://mcp.server.test",
    auth_servers=("https://auth.server.test",),
    default_scopes=("mcp",),
)


@pytest.fixture(autouse=True)
def _no_form_wait(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answer pending-form retries immediately; waiting tests override this."""
    with mock.patch("aiohttp_mcp._elicitation._FORM_WAIT", 0):
        yield


def make_secure_client(
    make_client: MakeClient,
    *,
    allowed_origins: tuple[str, ...] = ("127.0.0.1",),
    authenticate_request: AuthenticateRequest | None = browser_user,
    prompts: PromptRegistry | None = None,
    render_form: RenderForm | None = None,
) -> Awaitable[TestClient[web.Request, web.Application]]:
    return make_client(
        authorization=make_auth(),
        authenticate_request=authenticate_request,
        prompts=prompts if prompts is not None else PromptRegistry(set_key),
        # The TestClient connects via the loopback address, not the resource host.
        allowed_origins=allowed_origins,
        render_form=render_form,
    )


def get_prompt(
    client: TestClient[web.Request, web.Application],
    *,
    capabilities: dict[str, object] | None = None,
    input_responses: dict[str, object] | None = None,
    request_state: str | None = None,
    token: str = "alice-token",
) -> AbstractAsyncContextManager[ClientResponse]:
    params: dict[str, object] = {"name": "set_key"}
    if input_responses is not None:
        params["inputResponses"] = input_responses
    if request_state is not None:
        params["requestState"] = request_state
    headers = request_headers("prompts/get")
    headers["Mcp-Name"] = "set_key"
    headers["Authorization"] = f"Bearer {token}"
    body = request_body(
        "prompts/get",
        params,
        capabilities=capabilities if capabilities is not None else ALL_CAPS,
    )
    return client.post("/mcp", json=body, headers=headers)


async def result_of(resp: ClientResponse) -> dict[str, Any]:
    assert resp.status == 200, await resp.text()
    return dict((await resp.json())["result"])


async def start_flow(client: TestClient[web.Request, web.Application]) -> tuple[str, str]:
    """First attempt: returns the form page path and the request state."""
    async with get_prompt(client) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    request = result["inputRequests"]["creds"]
    params = request["params"]
    assert params["mode"] == "url"
    assert params["message"] == "Enter your API credentials."
    url = URL(params["url"])
    # Built on the request's (allowed) origin, so every legitimate origin of
    # a multi-origin deployment keeps working.
    assert url.origin() == client.make_url("/").origin()
    path = url.path
    assert path.startswith("/mcp/elicit/")
    return path, result["requestState"]


ACCEPT: dict[str, object] = {"creds": {"action": "accept"}}
VALID_FORM = MultiDict(
    [
        ("api_key", "longenough"),
        ("region", "eu"),
        ("retries", "3"),
        ("sandbox", "on"),
        ("tags", "a"),
    ]
)


async def test_full_flow(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    path, state = await start_flow(client)

    browser = {"X-Test-User": "alice"}
    async with client.get(path, headers=browser) as page:
        assert page.status == 200
        body = await page.text()
    assert "Enter your API credentials." in body
    for control in (
        'name="api_key"',
        'name="region"',
        '<option value="eu">Europe</option>',
        'type="checkbox"',
        'type="number"',
        "multiple",
    ):
        assert control in body

    async with client.post(path, data=VALID_FORM, headers=browser) as resp:
        assert "return to your agent" in await resp.text()

    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "complete"
    text = result["messages"][0]["content"]["text"]
    assert text == repr(
        {"api_key": "longenough", "region": "eu", "retries": 3, "sandbox": True, "tags": ("a",)}
    )


async def test_poll_until_submitted(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    path, state = await start_flow(client)

    # Accepted in the client, but nothing submitted in the browser yet.
    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    assert "inputRequests" not in result  # Poll: same form, no new requests.

    browser = {"X-Test-User": "alice"}
    async with client.post(path, data=VALID_FORM, headers=browser):
        pass
    async with get_prompt(
        client, input_responses=ACCEPT, request_state=result["requestState"]
    ) as resp:
        assert (await result_of(resp))["resultType"] == "complete"


async def test_retry_held_until_submission(
    make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry with a pending form blocks and completes on submission."""
    monkeypatch.setattr("aiohttp_mcp._elicitation._FORM_WAIT", 30)
    attempting = asyncio.Event()

    async def set_key(request: MCPRequest) -> str:
        # Entering the wait after set() is synchronous, so a submission
        # released by the event is guaranteed to find the request held open.
        attempting.set()
        result = await request.elicit("creds", API_FORM)
        assert result.content is not None
        return repr(dict(result.content))

    client = await make_secure_client(make_client, prompts=PromptRegistry(set_key))
    path, state = await start_flow(client)
    attempting.clear()  # Set by the first attempt; rearm for the retry.

    async def submit_when_held() -> None:
        await attempting.wait()
        browser = {"X-Test-User": "alice"}
        async with client.post(path, data=VALID_FORM, headers=browser):
            pass

    submission = asyncio.create_task(submit_when_held())
    try:
        # One round trip: held open until the browser submission arrives.
        async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
            result = await result_of(resp)
    finally:
        await submission
    assert result["resultType"] == "complete"


async def test_retry_wait_times_out(
    make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aiohttp_mcp._elicitation._FORM_WAIT", 0.05)
    client = await make_secure_client(make_client)
    _, state = await start_flow(client)

    loop = asyncio.get_running_loop()
    start = loop.time()
    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        result = await result_of(resp)
    # asyncio timers may fire up to one clock tick early (BaseEventLoop._run_once);
    # Windows' monotonic tick is ~16ms, so allow that much slack below _FORM_WAIT.
    assert loop.time() - start >= 0.05 - 0.016
    assert result["resultType"] == "input_required"
    assert "inputRequests" not in result


async def test_single_use(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    path, state = await start_flow(client)
    browser = {"X-Test-User": "alice"}
    async with client.post(path, data=VALID_FORM, headers=browser):
        pass
    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        assert (await result_of(resp))["resultType"] == "complete"

    # The submission was consumed: a replay gets a fresh form, not the values.
    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        result = await result_of(resp)
    assert result["resultType"] == "input_required"
    assert "creds" in result["inputRequests"]

    # And the old page is gone.
    async with client.get(path, headers=browser) as page:
        assert page.status == 404


async def test_wrong_user_forbidden(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    path, _state = await start_flow(client)
    async with client.get(path, headers={"X-Test-User": "bob"}) as page:
        assert page.status == 403
    async with client.post(path, data=VALID_FORM, headers={"X-Test-User": "bob"}) as page:
        assert page.status == 403


async def test_unauthenticated_browser(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    path, _state = await start_flow(client)
    async with client.get(path) as page:
        assert page.status == 401
        assert "Sign in" in await page.text()


async def test_decline_discards_form(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    _path, state = await start_flow(client)
    declined: dict[str, object] = {"creds": {"action": "decline"}}
    async with get_prompt(client, input_responses=declined, request_state=state) as resp:
        result = await result_of(resp)
    assert result["messages"][0]["content"]["text"] == "No credentials (decline)."


async def test_validation_rerenders(make_client: MakeClient) -> None:
    client = await make_secure_client(make_client)
    path, state = await start_flow(client)
    browser = {"X-Test-User": "alice"}

    bad = MultiDict([("api_key", "short")])
    async with client.post(path, data=bad, headers=browser) as resp:
        assert resp.status == 200
        assert "api_key:" in await resp.text()

    missing = MultiDict([("region", "eu")])
    async with client.post(path, data=missing, headers=browser) as resp:
        assert "api_key is required" in await resp.text()

    not_a_number = MultiDict([("api_key", "longenough"), ("retries", "lots")])
    async with client.post(path, data=not_a_number, headers=browser) as resp:
        assert "retries: expected a number" in await resp.text()

    # Nothing was stored: the client still polls.
    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        assert (await result_of(resp))["resultType"] == "input_required"


async def test_custom_form_page(make_client: MakeClient) -> None:
    async def branded(request: web.Request, context: FormContext) -> web.Response:
        body = (
            f"<html><body class='brand'><h1>ACME - {context.server_name}</h1>"
            f"{'<p>' + context.error + '</p>' if context.error else ''}"
            f"<form method='post'>{context.fields_html}"
            "<button>Go</button></form></body></html>"
        )
        return web.Response(text=body, content_type="text/html")

    client = await make_secure_client(make_client, render_form=branded)
    path, state = await start_flow(client)
    browser = {"X-Test-User": "alice"}

    async with client.get(path, headers=browser) as page:
        body = await page.text()
    assert "ACME" in body
    assert 'name="api_key"' in body  # fields_html provides the controls.

    # Validation errors re-render through the custom page too.
    async with client.post(
        path, data=MultiDict([("api_key", "short")]), headers=browser
    ) as resp:
        body = await resp.text()
    assert "ACME" in body and "api_key:" in body

    async with client.post(path, data=VALID_FORM, headers=browser):
        pass
    async with get_prompt(client, input_responses=ACCEPT, request_state=state) as resp:
        assert (await result_of(resp))["resultType"] == "complete"


async def test_not_configured_is_internal_error(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a browser identity, secure forms cannot be issued."""
    client = await make_secure_client(make_client, authenticate_request=None)
    async with get_prompt(client) as resp:
        assert resp.status == 500
        assert (await resp.json())["error"]["code"] == -32603
    assert any(
        r.name == "aiohttp_mcp"
        and r.levelno == logging.ERROR
        and "browser identity" in r.message
        for r in caplog.records
    )


async def test_unrecognised_host_rejected(make_client: MakeClient) -> None:
    """A spoofed Host cannot steer the elicitation link off-origin."""
    # Only the resource host is allowed; the TestClient connects via the
    # loopback address, standing in for a spoofed Host header.
    client = await make_secure_client(make_client, allowed_origins=("mcp.server.test",))
    async with get_prompt(client) as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32600
    assert "Unrecognised host" in error["message"]


async def test_pending_forms_bounded(aiohttp_client: AiohttpClient) -> None:
    app = web.Application()
    server = aiohttp_mcp.setup(
        app,
        authorization=make_auth(),
        authenticate_request=browser_user,
        prompts=PromptRegistry(set_key),
        allowed_origins=("127.0.0.1",),
    )
    client = await aiohttp_client(app)

    # An expired entry is pruned by the next mint. Inserts always pair a
    # dict entry with a heap entry.
    server._forms["stale"] = _PendingForm("alice", API_FORM, time.time() - 1)
    heapq.heappush(server._form_expiries, (time.time() - 1, "stale"))

    paths = []
    with mock.patch("aiohttp_mcp._elicitation._MAX_PENDING_FORMS", 3):
        for _ in range(5):  # Each first attempt mints a fresh ticket.
            path, _state = await start_flow(client)
            paths.append(path)
    assert "stale" not in server._forms
    assert len(server._forms) == 3

    browser = {"X-Test-User": "alice"}
    async with client.get(paths[0], headers=browser) as page:
        assert page.status == 404  # Evicted to make room.
    async with client.get(paths[-1], headers=browser) as page:
        assert page.status == 200  # The newest survive.


async def test_local_authorization_login_redirect(aiohttp_client: "AiohttpClient") -> None:
    """Unauthenticated users are sent through LocalAuthorization's login."""

    async def check_credentials(username: str, password: str, request: web.Request) -> bool:
        return False

    authorization = LocalAuthorization(
        check_credentials, resource="https://mcp.server.test", secret=b"s" * 32
    )
    app = web.Application()
    server = aiohttp_mcp.setup(
        app, authorization=authorization, prompts=PromptRegistry(set_key)
    )
    client = await aiohttp_client(app)

    # Mint a pending form directly; obtaining an MCP token via the full OAuth
    # flow is covered by the auth-server tests.
    expires = time.time() + 60
    server._forms["test-ticket"] = _PendingForm("alice", API_FORM, expires)
    heapq.heappush(server._form_expiries, (expires, "test-ticket"))

    async with client.get("/mcp/elicit/test-ticket", allow_redirects=False) as page:
        assert page.status == 303
        location = URL(page.headers["Location"])
    assert location.path == "/oauth2/login"
    assert location.query["redirect"] == "/mcp/elicit/test-ticket"

    async with client.get(location.path_qs) as login_page:
        assert login_page.status == 200
        assert "/mcp/elicit/test-ticket" in await login_page.text()


async def test_login_redirect_under_sub_application(aiohttp_client: "AiohttpClient") -> None:
    async def check_credentials(username: str, password: str, request: web.Request) -> bool:
        return False

    authorization = LocalAuthorization(
        check_credentials, resource="https://mcp.server.test/api", secret=b"s" * 32
    )
    api = web.Application()
    server = aiohttp_mcp.setup(
        api,
        authorization=authorization,
        prompts=PromptRegistry(set_key),
        well_known_routes=False,
    )
    root = web.Application()
    root.add_subapp("/api", api)
    client = await aiohttp_client(root)

    expires = time.time() + 60
    server._forms["test-ticket"] = _PendingForm("alice", API_FORM, expires)
    heapq.heappush(server._form_expiries, (expires, "test-ticket"))

    async with client.get("/api/mcp/elicit/test-ticket", allow_redirects=False) as page:
        assert page.status == 303
        location = URL(page.headers["Location"])
    assert location.path == "/api/oauth2/login"
    assert location.query["redirect"] == "/api/mcp/elicit/test-ticket"

    # The prefixed return URL passes the allowlist.
    async with client.get(location.path_qs) as login_page:
        assert login_page.status == 200
        assert "/api/mcp/elicit/test-ticket" in await login_page.text()
