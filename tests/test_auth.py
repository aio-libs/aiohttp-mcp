"""Authorization: RFC 9728 metadata, bearer enforcement, challenges."""

import functools
import re
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient
from multidict import CIMultiDict, MultiDict
from pytest_aiohttp import AiohttpClient

from _helpers import MakeClient, next_sse_data, request_body, request_headers
from aiohttp_mcp import (
    Authorization,
    Prompt,
    PromptRegistry,
    Resource,
    ResourceRegistry,
    TokenInfo,
    Tool,
    ToolRegistry,
    setup,
)
from aiohttp_mcp._protocol import InputResult
from aiohttp_mcp._transport import MCPRequest, Method

CONN_KEY = web.RequestKey("conn", object)
RESOURCE_BASE = "https://mcp.server.test"
RESOURCE = "https://mcp.server.test/mcp"
METADATA_URL = "https://mcp.server.test/.well-known/oauth-protected-resource/mcp"
TOKENS = {
    "good-token": TokenInfo(
        scopes=frozenset({"mcp:read", "mcp:write"}),
        subject="alice",
        claims={"iss": "https://auth.server.test"},
    ),
    "weak-token": TokenInfo(scopes=frozenset({"mcp:read"})),
    "org-token": TokenInfo(scopes=frozenset({"pr:aio-libs"}), subject="sam"),
    "broad-token": TokenInfo(scopes=frozenset({"pr"}), subject="sam"),
}


async def validator(token: str, request: web.Request) -> TokenInfo | None:
    if token == "crash-token":
        raise RuntimeError("introspection backend down")
    return TOKENS.get(token)


make_auth = functools.partial(
    Authorization,
    validator,
    resource=RESOURCE_BASE,
    auth_servers=("https://auth.server.test",),
    default_scopes=("mcp:read", "mcp:write"),
)


def challenge_params(resp: ClientResponse) -> dict[str, str]:
    header = resp.headers["WWW-Authenticate"]
    assert header.startswith("Bearer ")
    return dict(re.findall(r'([a-z_]+)="([^"]*)"', header))


def post(
    client: TestClient[web.Request, web.Application],
    method: str = "server/discover",
    params: dict[str, object] | None = None,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> AbstractAsyncContextManager[ClientResponse]:
    hdrs = request_headers(method)
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    return client.post("/mcp", json=request_body(method, params), headers=hdrs)


async def test_metadata_document(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth(
        resource_name="Test Server", resource_documentation="https://example.test/docs"
    ))
    async with client.get("/.well-known/oauth-protected-resource/mcp") as resp:
        assert resp.status == 200
        assert resp.content_type == "application/json"
        assert await resp.json() == {
            "resource": RESOURCE,
            "authorization_servers": ["https://auth.server.test"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp:read", "mcp:write"],
            "resource_name": "Test Server",
            "resource_documentation": "https://example.test/docs",
        }


async def test_metadata_at_root(make_client: MakeClient) -> None:
    """An endpoint at the root serves metadata at the root well-known."""
    client = await make_client(path="/", authorization=make_auth())
    async with client.get("/.well-known/oauth-protected-resource") as resp:
        assert (await resp.json())["resource"] == RESOURCE_BASE


async def test_no_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client) as resp:
        assert resp.status == 401
        params = challenge_params(resp)
        assert params["resource_metadata"] == METADATA_URL
        assert params["scope"] == "mcp:read mcp:write"  # the default_scopes hint
        # No authentication information was presented: no error code (RFC 6750 §3.1).
        assert "error" not in params
        body = await resp.json()
        assert body["error"]["code"] == -32600
        assert "id" not in body


async def test_valid_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client, token="good-token") as resp:
        assert resp.status == 200
        result = await resp.json()
        assert result["result"]["resultType"] == "complete"


async def test_invalid_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client, token="expired-token") as resp:
        assert resp.status == 401
        params = challenge_params(resp)
        assert params["error"] == "invalid_token"
        assert params["resource_metadata"] == METADATA_URL


@pytest.mark.parametrize(
    "header", ("Basic dXNlcjpwdw==", "Bearer", "Bearer  ", "Bearer two tokens")
)
async def test_malformed_authorization(make_client: MakeClient, header: str) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client, headers={"Authorization": header}) as resp:
        assert resp.status == 400
        assert challenge_params(resp)["error"] == "invalid_request"


async def test_multiple_authorization_headers(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    headers = CIMultiDict(request_headers("server/discover"))
    headers.add("Authorization", "Bearer good-token")
    headers.add("Authorization", "Bearer weak-token")
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 400
        assert challenge_params(resp)["error"] == "invalid_request"


async def test_token_in_query_string(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with client.post(
        "/mcp",
        params=MultiDict({"access_token": "good-token"}),
        json=request_body("server/discover"),
        headers=request_headers("server/discover"),
    ) as resp:
        assert resp.status == 400
        assert challenge_params(resp)["error"] == "invalid_request"


async def test_validator_crash(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client, token="crash-token") as resp:
        assert resp.status == 500
        assert "WWW-Authenticate" not in resp.headers
        result = await resp.json()
        assert result["error"]["code"] == -32603


async def test_streaming_requires_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client, "subscriptions/listen") as resp:
        assert resp.status == 401
        assert resp.content_type == "application/json"

    async with post(client, "subscriptions/listen", token="good-token") as resp:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        ack = await next_sse_data(resp)
        assert ack["method"] == "notifications/subscriptions/acknowledged"


async def test_origin_checked_before_auth(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with post(client, headers={"Origin": "https://evil.example"}) as resp:
        assert resp.status == 403
        assert "WWW-Authenticate" not in resp.headers


class _WhoamiResult(InputResult):
    subject: str | None
    claims: dict[str, object]


async def test_token_info_exposed_to_handlers(aiohttp_client: AiohttpClient) -> None:
    app = web.Application()
    server = setup(app, name="test-server", authorization=make_auth())

    async def whoami(request: MCPRequest) -> _WhoamiResult:
        assert request.auth is not None
        return {"subject": request.auth.subject, "claims": dict(request.auth.claims)}

    server._methods["test/whoami"] = Method(whoami)
    client = await aiohttp_client(app)
    async with post(client, "test/whoami", token="good-token") as resp:
        result = await resp.json()
    assert result["result"]["subject"] == "alice"
    assert result["result"]["claims"] == {"iss": "https://auth.server.test"}


async def test_no_authorization_configured(make_client: MakeClient) -> None:
    """Without authorization=, no enforcement and no metadata route."""
    client = await make_client()
    async with post(client) as resp:
        assert resp.status == 200
    async with client.get("/.well-known/oauth-protected-resource/mcp") as resp:
        assert resp.status == 404


@pytest.mark.parametrize(
    "kwargs",
    (
        {"resource": "mcp.server.test"},  # missing scheme
        {"resource": "https://mcp.server.test#frag"},
        {"resource": "https://mcp.server.test?x=1"},
        # Non-canonical forms yarl would silently normalise away from the
        # string clients compare against.
        {"resource": "https://mcp.server.test:443"},  # explicit default port
        {"resource": "https://MCP.server.test"},  # host case
        {"resource": "https://münchen.example"},  # IDN host (use punycode)
        {"resource": "https://mcp.server.test/My%20Prefix"},  # encoded path
        {"resource": "https://user@mcp.server.test"},  # userinfo
        {"auth_servers": ()},
        {"auth_servers": ("auth.server.test",)},
        # All authorization server endpoints MUST be served over HTTPS
        # (plain http only on localhost, for local development).
        {"auth_servers": ("http://auth.server.test",)},
        {"default_scopes": ('bad"scope',)},
        {"default_scopes": ("bad scope",)},
    ),
)
async def test_invalid_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make_auth(**kwargs)  # type: ignore[arg-type]


async def test_canonical_form_suggested() -> None:
    """The rejection names the exact canonical form to use instead."""
    with pytest.raises(ValueError, match=r"'https://mcp\.server\.test'"):
        make_auth(resource="https://MCP.server.test:443")


async def test_resource_trailing_slash_normalized(make_client: MakeClient) -> None:
    auth = make_auth(resource="https://mcp.server.test/")
    await make_client(authorization=auth)
    assert auth._resource == "https://mcp.server.test/mcp"
    assert auth._metadata_path == "/.well-known/oauth-protected-resource/mcp"
    assert auth._metadata_url == METADATA_URL


async def test_metadata_url_keeps_explicit_port(make_client: MakeClient) -> None:
    """A non-default port is part of the origin and survives derivation."""
    auth = make_auth(resource="https://mcp.server.test:8443")
    await make_client(authorization=auth)
    assert auth._resource == "https://mcp.server.test:8443/mcp"
    assert auth._metadata_path == "/.well-known/oauth-protected-resource/mcp"
    assert (
        auth._metadata_url
        == "https://mcp.server.test:8443/.well-known/oauth-protected-resource/mcp"
    )


def scoped_app() -> tuple[web.Application, Mapping[str, Method]]:
    """An app with injected methods carrying static and per-call scopes."""
    app = web.Application()
    server = setup(app, name="test-server", authorization=make_auth())

    async def noop(request: MCPRequest) -> None:
        return None

    async def org_scopes(request: MCPRequest) -> frozenset[str]:
        return frozenset({f"pr:{request.params['org']}"})

    async def broken_scopes(request: MCPRequest) -> frozenset[str]:
        raise RuntimeError("scope lookup failed")

    server._methods["test/write"] = Method(noop, scopes=("mcp:write",))
    server._methods["test/write-pr"] = Method(noop, scopes=org_scopes)
    server._methods["test/broken"] = Method(noop, scopes=broken_scopes)
    return app, server._methods


async def test_static_method_scopes(aiohttp_client: AiohttpClient) -> None:
    app, _ = scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write", token="good-token") as resp:
        assert resp.status == 200

    async with post(client, "test/write", token="weak-token") as resp:
        assert resp.status == 403
        params = challenge_params(resp)
        assert params["error"] == "insufficient_scope"
        # Exactly the operation's scopes, not the default guidance.
        assert params["scope"] == "mcp:write"
        assert params["resource_metadata"] == METADATA_URL


async def test_parameter_dependent_scopes(aiohttp_client: AiohttpClient) -> None:
    """Scopes computed from call arguments, e.g. per-org write permissions."""
    app, _ = scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}, token="org-token") as resp:
        assert resp.status == 200

    async with post(client, "test/write-pr", {"org": "foo"}, token="org-token") as resp:
        assert resp.status == 403
        assert challenge_params(resp)["scope"] == "pr:foo"


async def test_unauthenticated_challenge_names_call_scopes(
    aiohttp_client: AiohttpClient,
) -> None:
    app, _ = scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}) as resp:
        assert resp.status == 401
        assert challenge_params(resp)["scope"] == "pr:aio-libs"

    async with post(client, "test/write") as resp:
        assert challenge_params(resp)["scope"] == "mcp:write"


async def test_unauthenticated_challenge_fallbacks(aiohttp_client: AiohttpClient) -> None:
    app, _ = scoped_app()
    client = await aiohttp_client(app)
    for method in ("no/such-method", "server/discover", "test/broken"):
        async with post(client, method) as resp:
            assert resp.status == 401
            assert challenge_params(resp)["scope"] == "mcp:read mcp:write"

    headers = request_headers("test/write")
    async with client.post("/mcp", data=b"{not json", headers=headers) as resp:
        assert resp.status == 401
        assert challenge_params(resp)["scope"] == "mcp:read mcp:write"


async def test_scope_callable_crash_is_internal_error(aiohttp_client: AiohttpClient) -> None:
    app, _ = scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/broken", token="good-token") as resp:
        assert resp.status == 500
        assert (await resp.json())["error"]["code"] == -32603


async def test_scopes_ignored_without_authorization(aiohttp_client: AiohttpClient) -> None:
    """Method scopes cannot be enforced when no authorization is configured."""
    app = web.Application()
    server = setup(app, name="test-server")

    async def noop(request: MCPRequest) -> None:
        return None

    server._methods["test/write"] = Method(noop, scopes=("mcp:write",))
    client = await aiohttp_client(app)
    async with post(client, "test/write") as resp:
        assert resp.status == 200


def prefix_matcher(required: str, held: frozenset[str]) -> bool:
    """A broad scope implies its parameterised narrowings: pr > pr:{org}."""
    return required in held or required.partition(":")[0] in held


async def test_scope_hierarchy(aiohttp_client: AiohttpClient) -> None:
    """A broader scope satisfies narrower requirements via the matcher."""
    app = web.Application()
    auth = make_auth(scope_matcher=prefix_matcher)
    server = setup(app, name="test-server", authorization=auth)

    async def noop(request: MCPRequest) -> None:
        return None

    async def org_scopes(request: MCPRequest) -> frozenset[str]:
        return frozenset({f"pr:{request.params['org']}"})

    server._methods["test/write-pr"] = Method(noop, scopes=org_scopes)
    server._methods["test/write"] = Method(noop, scopes=("mcp:write",))
    client = await aiohttp_client(app)

    # "broad-token" holds the bare "pr" scope: it implies every pr:{org},
    # which no expansion into TokenInfo.scopes could enumerate.
    for org in ("aio-libs", "foo"):
        async with post(client, "test/write-pr", {"org": org}, token="broad-token") as resp:
            assert resp.status == 200

    # No held scope implies mcp:write; the challenge still names it exactly.
    async with post(client, "test/write", token="broad-token") as resp:
        assert resp.status == 403
        assert challenge_params(resp)["scope"] == "mcp:write"


async def test_no_hierarchy_by_default(aiohttp_client: AiohttpClient) -> None:
    """Without a matcher, scope comparison is exact membership."""
    app, _ = scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}, token="broad-token") as resp:
        assert resp.status == 403


def generator_scoped_app(
    *, fail_cleanup: bool = False, yield_twice: bool = False
) -> tuple[web.Application, list[str]]:
    events: list[str] = []
    app = web.Application()
    server = setup(app, name="test-server", authorization=make_auth())

    async def pr_scopes(req: MCPRequest) -> "AsyncGenerator[frozenset[str], None]":
        req.http[CONN_KEY] = conn = object()
        events.append("acquire")
        try:
            yield frozenset({f"pr:{req.params['org']}"})
            if yield_twice:
                yield frozenset()
            if fail_cleanup:
                raise RuntimeError("commit failed")
            assert req.http[CONN_KEY] is conn
            events.append("commit")
        except BaseException as exc:
            events.append(f"rollback:{type(exc).__name__}")
            raise
        finally:
            events.append("release")

    async def write_pr(req: MCPRequest) -> None:
        # The handler reuses what the scopes generator acquired.
        assert req.http[CONN_KEY] is not None
        events.append("handler")
        if req.params.get("boom"):
            raise RuntimeError("handler failed")

    async def stream_pr(req: MCPRequest) -> None:
        assert req.http[CONN_KEY] is not None
        events.append("handler")

    server._methods["test/write-pr"] = Method(write_pr, scopes=pr_scopes)
    server._methods["test/stream-pr"] = Method(stream_pr, streaming=True, scopes=pr_scopes)
    return app, events


async def test_scopes_generator_shares_resource(aiohttp_client: AiohttpClient) -> None:
    app, events = generator_scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}, token="org-token") as resp:
        assert resp.status == 200
    # Acquired before the check, alive during the handler, released after.
    assert events == ["acquire", "handler", "commit", "release"]


async def test_scopes_generator_closed_on_403(aiohttp_client: AiohttpClient) -> None:
    app, events = generator_scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "foo"}, token="org-token") as resp:
        assert resp.status == 403
    # The generator is closed without running the handler; its BaseException
    # hook observes the GeneratorExit before the resource is released.
    assert events == ["acquire", "rollback:GeneratorExit", "release"]


async def test_scopes_generator_closed_on_401_hint(aiohttp_client: AiohttpClient) -> None:
    app, events = generator_scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}) as resp:
        assert resp.status == 401
        assert challenge_params(resp)["scope"] == "pr:aio-libs"
    assert events == ["acquire", "rollback:GeneratorExit", "release"]


async def test_scopes_generator_sees_handler_exception(aiohttp_client: AiohttpClient) -> None:
    app, events = generator_scoped_app()
    client = await aiohttp_client(app)
    async with post(
        client, "test/write-pr", {"org": "aio-libs", "boom": True}, token="org-token"
    ) as resp:
        assert resp.status == 500
        result = await resp.json()
        assert result["error"]["code"] == -32603
    # The generator sees the handler's actual exception, not a converted one.
    assert events == ["acquire", "handler", "rollback:RuntimeError", "release"]


async def test_scopes_generator_streaming(aiohttp_client: AiohttpClient) -> None:
    app, events = generator_scoped_app()
    client = await aiohttp_client(app)
    async with post(client, "test/stream-pr", {"org": "aio-libs"}, token="org-token") as resp:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        assert await resp.text()  # stream fully consumed and closed
    assert events == ["acquire", "handler", "commit", "release"]


async def test_scopes_generator_cleanup_failure(aiohttp_client: AiohttpClient) -> None:
    """A failed commit must not look like success on a basic request."""
    app, events = generator_scoped_app(fail_cleanup=True)
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}, token="org-token") as resp:
        assert resp.status == 500
    assert events == ["acquire", "handler", "rollback:RuntimeError", "release"]


async def test_scopes_generator_yields_twice(aiohttp_client: AiohttpClient) -> None:
    app, events = generator_scoped_app(yield_twice=True)
    client = await aiohttp_client(app)
    async with post(client, "test/write-pr", {"org": "aio-libs"}, token="org-token") as resp:
        assert resp.status == 500
    assert events[-1] == "release"


# - Scope-restricted feature items (prompts and resources) --------------------


async def open_item() -> str:
    return "open"


async def secret_item() -> str:
    return "secret"


async def secret_template(path: str) -> str:
    return path


@dataclass(frozen=True)
class ScopedFeature:
    """A feature registry with one open item and ``mcp:write``-scoped items."""

    kwargs: "Callable[[], dict[str, object]]"
    # List methods: (method, result key, names visible to weak/good tokens).
    lists: "tuple[tuple[str, str, list[str], list[str]], ...]"
    # Calls hitting a scoped item: (method, params, Mcp-Name header value).
    calls: "tuple[tuple[str, dict[str, object], str], ...]"


FEATURES = {
    "prompts": ScopedFeature(
        kwargs=lambda: {
            "prompts": PromptRegistry(
                open_item, Prompt(secret_item, scopes=("mcp:write",))
            )
        },
        lists=(
            ("prompts/list", "prompts", ["open_item"], ["open_item", "secret_item"]),
        ),
        calls=(("prompts/get", {"name": "secret_item"}, "secret_item"),),
    ),
    "resources": ScopedFeature(
        kwargs=lambda: {
            "resources": ResourceRegistry(
                Resource(open_item, "docs://open"),
                Resource(secret_item, "docs://secret", scopes=("mcp:write",)),
                Resource(secret_template, "files://{+path}", scopes=("mcp:write",)),
            )
        },
        lists=(
            (
                "resources/list",
                "resources",
                ["open_item"],
                ["open_item", "secret_item"],
            ),
            ("resources/templates/list", "resourceTemplates", [], ["secret_template"]),
        ),
        calls=(
            ("resources/read", {"uri": "docs://secret"}, "docs://secret"),
            # The scopes of a template apply to every URI it matches.
            ("resources/read", {"uri": "files://a/b"}, "files://a/b"),
        ),
    ),
    "tools": ScopedFeature(
        kwargs=lambda: {
            "tools": ToolRegistry(open_item, Tool(secret_item, scopes=("mcp:write",)))
        },
        lists=(
            ("tools/list", "tools", ["open_item"], ["open_item", "secret_item"]),
        ),
        calls=(("tools/call", {"name": "secret_item"}, "secret_item"),),
    ),
}
scoped_features = pytest.mark.parametrize("feature", FEATURES.values(), ids=FEATURES.keys())


@scoped_features
async def test_scoped_items_hidden_from_lists(
    make_client: MakeClient, feature: ScopedFeature
) -> None:
    client = await make_client(
        authorization=make_auth(default_scopes=("mcp:read",)), **feature.kwargs()
    )
    for method, key, weak_names, good_names in feature.lists:
        for token, expected in (("weak-token", weak_names), ("good-token", good_names)):
            async with post(client, method, token=token) as resp:
                assert resp.status == 200
                body = await resp.json()
            assert [item["name"] for item in body["result"][key]] == expected


@scoped_features
async def test_scoped_items_challenge(
    make_client: MakeClient, feature: ScopedFeature
) -> None:
    client = await make_client(
        authorization=make_auth(default_scopes=("mcp:read",)), **feature.kwargs()
    )
    for method, params, name in feature.calls:
        headers = {"Mcp-Name": name}
        async with post(
            client, method, dict(params), token="weak-token", headers=headers
        ) as resp:
            assert resp.status == 403
            challenge = challenge_params(resp)
        assert challenge["error"] == "insufficient_scope"
        assert "mcp:write" in challenge["scope"]
        async with post(
            client, method, dict(params), token="good-token", headers=headers
        ) as resp:
            assert resp.status == 200


@scoped_features
async def test_scoped_items_require_authorization(
    make_client: MakeClient, feature: ScopedFeature
) -> None:
    with pytest.raises(ValueError, match="require authorization"):
        await make_client(**feature.kwargs())
