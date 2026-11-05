"""IntrospectionAuthorization (RFC 7662) and JWTAuthorization (RFC 9068)."""

import asyncio
import json
import time
from contextlib import AbstractAsyncContextManager
from typing import Any

import jwt
import pytest
from _helpers import MakeClient, request_body, request_headers
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, make_mocked_request
from cryptography.hazmat.primitives.asymmetric import rsa
from pytest_aiohttp import AiohttpClient

from aiohttp_mcp import IntrospectionAuthorization, setup
from aiohttp_mcp.jwt import JWTAuthorization

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
    fake_as.grant("short", exp=time.time() + 0.05)  # entry capped at token exp
    fake_as.grant("long")
    auth = introspection_auth(fake_as, cache_ttl=60)
    client = await make_client(authorization=auth)
    async with mcp_post(client, token="short") as resp:
        assert resp.status == 200
    async with mcp_post(client, token="long") as resp:
        assert resp.status == 200
    assert set(auth._cache) == {"short", "long"}

    await asyncio.sleep(0.05)
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


# - RFC 9068 JWT ---------------------------------------------------------------


class FakeJWTIssuer:
    """An authorization server publishing RFC 8414 metadata and a JWKS."""

    def __init__(self) -> None:
        self.jwks_content_type = "application/json"
        self.keys = {"key-1": rsa.generate_private_key(65537, 2048)}
        self.jwks_fetches = 0
        self.claimed_issuer: str | None = None  # None: claim the real issuer
        self.jwks_uri_override: str | None = None
        self.app = web.Application()
        self.app.router.add_get("/.well-known/oauth-authorization-server", self.metadata)
        self.app.router.add_get("/jwks", self.jwks)
        self.issuer = ""  # assigned once the server is bound

    async def metadata(self, request: web.Request) -> web.Response:
        claimed = self.issuer if self.claimed_issuer is None else self.claimed_issuer
        jwks_uri = (
            f"{self.issuer}/jwks" if self.jwks_uri_override is None else self.jwks_uri_override
        )
        return web.json_response({"issuer": claimed, "jwks_uri": jwks_uri})

    async def jwks(self, request: web.Request) -> web.Response:
        self.jwks_fetches += 1
        keys = [
            {**jwt.get_algorithm_by_name("RS256").to_jwk(key.public_key(), as_dict=True), "kid": kid}
            for kid, key in self.keys.items()
        ]
        return web.Response(
            text=json.dumps({"keys": keys}), content_type=self.jwks_content_type
        )

    def mint(
        self,
        *,
        kid: str = "key-1",
        typ: str = "at+jwt",
        alg: str = "RS256",
        key: object = None,
        **claims: object,
    ) -> str:
        claims.setdefault("iss", self.issuer)
        claims.setdefault("aud", RESOURCE)
        claims.setdefault("exp", time.time() + 300)
        claims.setdefault("iat", time.time())
        claims.setdefault("sub", "alice")
        claims.setdefault("scope", "mcp:read")
        return jwt.encode(
            claims,
            key if key is not None else self.keys["key-1"],  # type: ignore[arg-type]
            algorithm=alg,
            headers={"kid": kid, "typ": typ},
        )


@pytest.fixture
async def issuer(aiohttp_client: AiohttpClient) -> FakeJWTIssuer:
    fake = FakeJWTIssuer()
    client = await aiohttp_client(fake.app)
    fake.issuer = str(client.make_url("")).rstrip("/")
    return fake


def jwt_auth(issuer: FakeJWTIssuer, **kwargs: object) -> JWTAuthorization:
    kwargs.setdefault("resource", RESOURCE_BASE)
    return JWTAuthorization(issuer=issuer.issuer, **kwargs)  # type: ignore[arg-type]


async def test_jwt_valid_token(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    auth = jwt_auth(issuer)
    client = await make_client(authorization=auth)
    token = issuer.mint(scope="mcp:read mcp:write", email="alice@server.test")
    async with mcp_post(client, token=token) as resp:
        assert resp.status == 200

    info = await auth._validate_token(token, make_mocked_request("POST", "/mcp"))
    assert info is not None
    assert info.subject == "alice"
    assert info.scopes == frozenset({"mcp:read", "mcp:write"})
    assert info.claims["email"] == "alice@server.test"
    # Keys were discovered once and reused.
    assert issuer.jwks_fetches == 1


@pytest.mark.parametrize(
    "mint_kwargs",
    (
        {"aud": "https://other.example/api"},
        {"iss": "https://evil.example"},
        {"exp": time.time() - 10},
        {"typ": "JWT"},  # not an at+jwt access token (e.g. an OIDC ID token)
    ),
)
async def test_jwt_rejections(
    make_client: MakeClient, issuer: FakeJWTIssuer, mint_kwargs: dict[str, str]
) -> None:
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint(**mint_kwargs)) as resp:
        assert resp.status == 401


async def test_jwt_wrong_key(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    client = await make_client(authorization=jwt_auth(issuer))
    forged = issuer.mint(key=rsa.generate_private_key(65537, 2048))
    async with mcp_post(client, token=forged) as resp:
        assert resp.status == 401


async def test_jwt_plain_typ_opt_in(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    """Servers typing access tokens as plain JWT work with typ=None."""
    client = await make_client(authorization=jwt_auth(issuer, typ=None))
    async with mcp_post(client, token=issuer.mint(typ="JWT")) as resp:
        assert resp.status == 200


async def test_jwt_key_rotation(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    """An unknown kid triggers a JWKS refetch (rotated keys keep working)."""
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint()) as resp:
        assert resp.status == 200

    issuer.keys["key-2"] = rsa.generate_private_key(65537, 2048)
    rotated = issuer.mint(kid="key-2", key=issuer.keys["key-2"])
    async with mcp_post(client, token=rotated) as resp:
        assert resp.status == 200
    assert issuer.jwks_fetches == 2

    # A kid the issuer never published fails even after a refetch.
    unknown = issuer.mint(kid="key-9")
    async with mcp_post(client, token=unknown) as resp:
        assert resp.status == 401


async def test_jwt_garbage_token(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token="not.a.jwt") as resp:
        assert resp.status == 401


async def test_jwt_issuer_in_prm(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    """auth_servers defaults to the issuer."""
    client = await make_client(authorization=jwt_auth(issuer))
    async with client.get("/.well-known/oauth-protected-resource/mcp") as resp:
        result = await resp.json()
        assert result["authorization_servers"] == [issuer.issuer]


async def test_jwt_metadata_issuer_mismatch(
    make_client: MakeClient, issuer: FakeJWTIssuer
) -> None:
    # RFC 8414 section 3.3
    issuer.claimed_issuer = "https://honest.example"
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint()) as resp:
        assert resp.status == 500  # configuration/trust failure, not invalid_token
        assert "WWW-Authenticate" not in resp.headers


async def test_jwt_oidc_discovery_fallback(
    make_client: MakeClient, aiohttp_client: AiohttpClient
) -> None:
    """An AS publishing only OpenID Connect Discovery metadata still works."""
    fake = FakeJWTIssuer()
    oidc_only = web.Application()
    oidc_only.router.add_get("/.well-known/openid-configuration", fake.metadata)
    oidc_only.router.add_get("/jwks", fake.jwks)
    client_as = await aiohttp_client(oidc_only)
    fake.issuer = str(client_as.make_url("")).rstrip("/")

    client = await make_client(authorization=jwt_auth(fake))
    async with mcp_post(client, token=fake.mint()) as resp:
        assert resp.status == 200


async def test_jwt_jwks_media_type(make_client: MakeClient, issuer: FakeJWTIssuer) -> None:
    issuer.jwks_content_type = "application/jwk-set+json"
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint()) as resp:
        assert resp.status == 200


async def test_jwt_jwks_wrong_content_type(
    make_client: MakeClient, issuer: FakeJWTIssuer
) -> None:
    issuer.jwks_content_type = "text/html"
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint()) as resp:
        assert resp.status == 500
        assert "WWW-Authenticate" not in resp.headers


async def test_https_enforcement_at_construction() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        JWTAuthorization(issuer="http://auth.server.test", resource=RESOURCE_BASE)
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


async def test_discovered_insecure_endpoints_rejected(
    make_client: MakeClient, aiohttp_client: AiohttpClient, issuer: FakeJWTIssuer
) -> None:
    # jwks_uri over http (non-loopback): never fetched.
    issuer.jwks_uri_override = "http://keys.server.test/jwks"
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint()) as resp:
        assert resp.status == 500
        assert "WWW-Authenticate" not in resp.headers

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
