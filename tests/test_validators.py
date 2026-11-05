"""IntrospectionAuthorization (RFC 7662)."""

import asyncio
import time
from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest import mock

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, make_mocked_request
from pytest_aiohttp import AiohttpClient

from _helpers import MakeClient, request_body, request_headers
from aiohttp_mcp import IntrospectionAuthorization, setup

RESOURCE_BASE = "https://mcp.server.test"
RESOURCE = "https://mcp.server.test/mcp"

Client = TestClient[web.Request, web.Application]


def mcp_post(
    client: Client, *, token: str | None = None
) -> AbstractAsyncContextManager[ClientResponse]:
    headers = request_headers("server/discover")
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=request_body("server/discover"), headers=headers)


# - RFC 7662 introspection ----------------------------------------------------


class FakeIntrospection:
    """An authorization server with introspection and RFC 8414 metadata."""

    client: Client

    def __init__(self, advertise_endpoint: bool = True) -> None:
        self.responses: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str | None, str | None]] = []
        self.advertise_endpoint = advertise_endpoint
        self.endpoint_override: str | None = None
        self.issuer = ""  # assigned once the server is bound
        self.app = web.Application()
        self.app.router.add_post("/introspect", self.handle)
        self.app.router.add_get("/.well-known/oauth-authorization-server", self.metadata)

    async def metadata(self, request: web.Request) -> web.Response:
        doc: dict[str, object] = {"issuer": self.issuer}
        if self.endpoint_override is not None:
            doc["introspection_endpoint"] = self.endpoint_override
        elif self.advertise_endpoint:
            doc["introspection_endpoint"] = f"{self.issuer}/introspect"
        return web.json_response(doc)

    async def handle(self, request: web.Request) -> web.Response:
        form = await request.post()
        token = form.get("token")
        self.requests.append(
            (request.headers.get("Authorization"), str(token) if token else None)
        )
        assert isinstance(token, str)
        return web.json_response(self.responses.get(token, {"active": False}))

    def grant(self, token: str, **claims: object) -> None:
        claims.setdefault("active", True)
        claims.setdefault("aud", [RESOURCE])
        claims.setdefault("exp", time.time() + 300)
        claims.setdefault("sub", "alice")
        claims.setdefault("scope", "mcp:read")
        self.responses[token] = claims


@pytest.fixture
async def fake_as(aiohttp_client: AiohttpClient) -> FakeIntrospection:
    fake = FakeIntrospection()
    fake.client = await aiohttp_client(fake.app)
    fake.issuer = str(fake.client.make_url("")).rstrip("/")
    return fake


def introspection_auth(fake: FakeIntrospection, **kwargs: Any) -> IntrospectionAuthorization:
    kwargs.setdefault("resource", RESOURCE_BASE)
    kwargs.setdefault("auth_servers", ("https://auth.server.test",))
    return IntrospectionAuthorization(
        str(fake.client.make_url("/introspect")),
        client_id="mcp-server",
        client_secret="hunter2",
        **kwargs,
    )


async def test_introspection_valid_token(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    fake_as.grant("tok", scope="mcp:read mcp:write", email="alice@server.test")
    client = await make_client(authorization=introspection_auth(fake_as))
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200

    # The introspection request carried our credentials and the token.
    authorization, token = fake_as.requests[0]
    assert authorization is not None and authorization.startswith("Basic ")
    assert token == "tok"


async def test_introspection_token_info(
    aiohttp_client: AiohttpClient, fake_as: FakeIntrospection
) -> None:
    fake_as.grant("tok", scope="mcp:read", email="alice@server.test")
    auth = introspection_auth(fake_as)
    app = web.Application()
    setup(app, name="test-server", authorization=auth)
    client = await aiohttp_client(app)
    async with mcp_post(client, token="tok"):
        pass

    info = await auth._validate_token("tok", make_mocked_request("POST", "/mcp"))
    assert info is not None
    assert info.subject == "alice"
    assert info.scopes == frozenset({"mcp:read"})
    assert info.claims["email"] == "alice@server.test"


@pytest.mark.parametrize(
    "claims",
    (
        {"active": False},
        {"aud": ["https://other.example/api"]},  # wrong audience
        {"aud": "https://other.example/api"},  # wrong audience, string form
        {"aud": None},  # no audience: cannot confirm binding
        {"exp": time.time() - 10},  # expired
    ),
)
async def test_introspection_rejections(
    make_client: MakeClient, fake_as: FakeIntrospection, claims: dict[str, object]
) -> None:
    fake_as.grant("tok", **claims)
    client = await make_client(authorization=introspection_auth(fake_as))
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 401
        assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]


async def test_introspection_discovery(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    """The endpoint is discovered from the issuer's RFC 8414 metadata."""
    fake_as.grant("tok")
    auth = IntrospectionAuthorization(
        issuer=fake_as.issuer,
        client_id="mcp-server",
        client_secret="hunter2",
        resource=RESOURCE_BASE,
    )
    client = await make_client(authorization=auth)
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200
    # auth_servers defaulted to the issuer.
    async with client.get("/.well-known/oauth-protected-resource/mcp") as resp:
        assert (await resp.json())["authorization_servers"] == [fake_as.issuer]


async def test_introspection_discovery_no_endpoint(
    make_client: MakeClient, aiohttp_client: AiohttpClient
) -> None:
    """An issuer not advertising introspection_endpoint fails closed."""
    fake = FakeIntrospection(advertise_endpoint=False)
    fake.client = await aiohttp_client(fake.app)
    fake.issuer = str(fake.client.make_url("")).rstrip("/")
    auth = IntrospectionAuthorization(
        issuer=fake.issuer,
        client_id="mcp-server",
        client_secret="hunter2",
        resource=RESOURCE_BASE,
    )
    client = await make_client(authorization=auth)
    fake.grant("tok")
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 500
        assert "WWW-Authenticate" not in resp.headers


@pytest.mark.parametrize(
    "kwargs",
    (
        {},  # neither introspection_url nor issuer
        {"introspection_url": "https://a/i", "issuer": "https://a"},  # both
        {"introspection_url": "https://a/i"},  # url without auth_servers
    ),
)
async def test_introspection_config_errors(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        IntrospectionAuthorization(
            kwargs.get("introspection_url"),
            issuer=kwargs.get("issuer"),
            client_id="x",
            client_secret="y",
            resource=RESOURCE_BASE,
        )


async def test_introspection_string_audience(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    fake_as.grant("tok", aud=RESOURCE)  # aud may be a bare string
    client = await make_client(authorization=introspection_auth(fake_as))
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200


async def test_introspection_unknown_token(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    client = await make_client(authorization=introspection_auth(fake_as))
    async with mcp_post(client, token="nope") as resp:
        assert resp.status == 401


async def test_introspection_as_failure_is_500(
    make_client: MakeClient, aiohttp_client: AiohttpClient
) -> None:
    """An unreachable authorization server fails closed as a server error."""
    broken = web.Application()

    async def boom(request: web.Request) -> web.Response:
        return web.Response(status=503)

    broken.router.add_post("/introspect", boom)
    fake = FakeIntrospection()
    fake.client = await aiohttp_client(broken)
    client = await make_client(authorization=introspection_auth(fake))
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 500
        assert "WWW-Authenticate" not in resp.headers


async def test_introspection_cache(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    fake_as.grant("tok")
    client = await make_client(authorization=introspection_auth(fake_as, cache_ttl=60))
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200
    assert len(fake_as.requests) == 1  # second request served from the cache

    client = await make_client(authorization=introspection_auth(fake_as))
    async with mcp_post(client, token="tok"):
        assert resp.status == 200
    async with mcp_post(client, token="tok"):
        assert resp.status == 200
    assert len(fake_as.requests) == 3  # no caching by default


async def test_introspection_cache_eviction(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    """Expired entries are evicted from the cache as new ones are inserted."""
    with mock.patch(
        "aiohttp_mcp._validators.time.time", autospec=True, spec_set=True, return_value=10
    ) as mock_time:
        fake_as.grant("short", exp=time.time() + 5)  # entry capped at token exp
        fake_as.grant("long")
        auth = introspection_auth(fake_as, cache_ttl=60)
        client = await make_client(authorization=auth)
        async with mcp_post(client, token="short") as resp:
            assert resp.status == 200
        async with mcp_post(client, token="long") as resp:
            assert resp.status == 200
        assert set(auth._cache) == {"short", "long"}

        mock_time.return_value = 20.0  # "short" is now past its exp; "long" is still live
        fake_as.grant("another")
        async with mcp_post(client, token="another") as resp:
            assert resp.status == 200
    # The insert popped the expired entry; the live one survives.
    assert set(auth._cache) == {"long", "another"}


async def test_introspection_cache_refresh(
    make_client: MakeClient, fake_as: FakeIntrospection
) -> None:
    """A re-validated token is re-cached; its stale heap record is harmless."""
    fake_as.grant("tok")
    auth = introspection_auth(fake_as, cache_ttl=0.05)
    client = await make_client(authorization=auth)
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200
    await asyncio.sleep(0.05)
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200  # re-introspected
    assert len(fake_as.requests) == 2
    assert "tok" in auth._cache  # the refreshed entry was not evicted
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 200  # cache hit
    assert len(fake_as.requests) == 2


async def test_https_enforcement_at_construction() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        IntrospectionAuthorization(
            "http://auth.server.test/introspect",
            client_id="x",
            client_secret="y",
            resource=RESOURCE_BASE,
            auth_servers=("https://auth.server.test",),
        )
    with pytest.raises(ValueError, match="HTTPS"):
        IntrospectionAuthorization(
            issuer="http://auth.server.test",
            client_id="x",
            client_secret="y",
            resource=RESOURCE_BASE,
        )


async def test_discovered_insecure_endpoint_rejected(
    make_client: MakeClient, aiohttp_client: AiohttpClient
) -> None:
    # introspection_endpoint over http (non-loopback): never used.
    fake = FakeIntrospection()
    fake.client = await aiohttp_client(fake.app)
    fake.issuer = str(fake.client.make_url("")).rstrip("/")
    fake.endpoint_override = "http://introspect.server.test/i"
    auth = IntrospectionAuthorization(
        issuer=fake.issuer, client_id="x", client_secret="y", resource=RESOURCE_BASE
    )
    client = await make_client(authorization=auth)
    fake.grant("tok")
    async with mcp_post(client, token="tok") as resp:
        assert resp.status == 500
