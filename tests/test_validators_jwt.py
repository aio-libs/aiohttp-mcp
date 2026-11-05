"""JWTAuthorization (RFC 9068).

Needs PyJWT with working ``cryptography`` wheels; skipped where they are
unavailable or broken at import time (e.g. PyPy).
"""

import json
import time
from contextlib import AbstractAsyncContextManager

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, make_mocked_request
from pytest_aiohttp import AiohttpClient

from _helpers import MakeClient, request_body, request_headers

pytest.importorskip("jwt", exc_type=ImportError)
pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.rsa", exc_type=ImportError
)

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

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


async def test_jwt_https_enforcement_at_construction() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        JWTAuthorization(issuer="http://auth.server.test", resource=RESOURCE_BASE)


async def test_jwt_discovered_insecure_jwks_rejected(
    make_client: MakeClient, issuer: FakeJWTIssuer
) -> None:
    # jwks_uri over http (non-loopback): never fetched.
    issuer.jwks_uri_override = "http://keys.server.test/jwks"
    client = await make_client(authorization=jwt_auth(issuer))
    async with mcp_post(client, token=issuer.mint()) as resp:
        assert resp.status == 500
        assert "WWW-Authenticate" not in resp.headers
