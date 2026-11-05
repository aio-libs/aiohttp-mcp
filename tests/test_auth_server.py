"""LocalAuthorization: the self-contained OAuth 2.1 authorization server."""

import asyncio
import functools
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import NotRequired, TypedDict
from unittest import mock

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient
from pytest_aiohttp import AiohttpClient
from yarl import URL

from _helpers import MakeClient, request_body, request_headers
from aiohttp_mcp import ConsentContext, LocalAuthorization, setup
from aiohttp_mcp._auth_server import _b64, _Signer

RESOURCE_BASE = "https://mcp.server.test"
RESOURCE = "https://mcp.server.test/mcp"
REDIRECT_URI = "http://127.0.0.1:9999/callback"
CLIENT_ID = "test-client"
SECRET = b"0" * 32

Client = TestClient[web.Request, web.Application]


async def check_credentials(username: str, password: str, request: web.Request) -> bool:
    return (username, password) == ("alice", "wonderland")


@pytest.fixture
def allow_local_cimd() -> Iterator[None]:
    """Lift the public-hosts-only CIMD fetch policy for loopback test servers."""

    with mock.patch(
        "aiohttp_mcp._auth_server._cimd_url_allowed", autospec=True, return_value=True
    ):
        yield


make_auth = functools.partial(
    LocalAuthorization,
    check_credentials=check_credentials,
    resource=RESOURCE_BASE,
    secret=SECRET,
    clients={CLIENT_ID: (REDIRECT_URI,)},
    default_scopes=("mcp:read",),
)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, urlsafe_b64encode(digest).decode().rstrip("=")


def authz_query(challenge: str, **overrides: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "scope": "mcp:read",
        "state": "xyz",
        **overrides
    }


def form_fields(body: str) -> dict[str, str]:
    """Hidden fields of a rendered form, entity-decoded like a browser."""
    return {
        name: html.unescape(value)
        for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', body)
    }


def token_payload(token: str) -> dict[str, object]:
    body = token.partition(".")[0]
    data = json.loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert isinstance(data, dict)
    return data


def login(
    client: Client,
    *,
    username: str = "alice",
    password: str = "wonderland",
    redirect: str = "/",
    param: str = "redirect",
) -> AbstractAsyncContextManager[ClientResponse]:
    return client.post(
        "/oauth2/login",
        data={"username": username, "password": password, param: redirect},
        allow_redirects=False,
    )


async def consent_page(
    client: Client, challenge: str, **overrides: str
) -> tuple[dict[str, str], str]:
    """GET the authorize endpoint, logging in through the built-in page if sent there."""
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge, **overrides),
        allow_redirects=False,
    ) as resp:
        if resp.status == 303:
            location = URL(resp.headers["Location"])
            assert location.path == "/oauth2/login"
            async with login(client, redirect=location.query["redirect"]) as login_resp:
                assert login_resp.status == 303
                resume = login_resp.headers["Location"]
            async with client.get(resume, allow_redirects=False) as page:
                assert page.status == 200, await page.text()
                body = await page.text()
        else:
            assert resp.status == 200, await resp.text()
            body = await resp.text()
    return form_fields(body), body


@asynccontextmanager
async def approve(
    client: Client, challenge: str, *, action: str = "approve", **overrides: str
) -> AsyncIterator[ClientResponse]:
    fields, _ = await consent_page(client, challenge, **overrides)
    fields["action"] = action
    async with client.post("/oauth2/authorize", data=fields, allow_redirects=False) as resp:
        yield resp


async def obtain_code(client: Client, challenge: str, **overrides: str) -> str:
    async with approve(client, challenge, **overrides) as resp:
        assert resp.status == 303, await resp.text()
        location = URL(resp.headers["Location"])
    assert location.query["state"] == "xyz"
    assert location.query["iss"] == "https://mcp.server.test"
    return location.query["code"]


def exchange(
    client: Client, code: str, verifier: str, **overrides: str
) -> AbstractAsyncContextManager[ClientResponse]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "resource": RESOURCE,
    }
    data.update(overrides)
    return client.post("/oauth2/token", data=data)


class Grant(TypedDict):
    """The token-endpoint response (draft-ietf-oauth-v2-1 section 4.1.3)."""

    access_token: str
    token_type: str
    expires_in: int
    scope: str
    refresh_token: NotRequired[str]


async def grant_tokens(client: Client, **auth_overrides: str) -> Grant:
    """Run the full code flow, returning the token-endpoint grant."""
    verifier, challenge = pkce()
    code = await obtain_code(client, challenge, **auth_overrides)
    async with exchange(client, code, verifier) as resp:
        assert resp.status == 200
        grant: Grant = await resp.json()
    assert isinstance(grant, dict)
    return grant


def refresh(
    client: Client, refresh_token: str, **overrides: str
) -> AbstractAsyncContextManager[ClientResponse]:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    data.update(overrides)
    return client.post("/oauth2/token", data=data)


# - Metadata ------------------------------------------------------------------


async def test_as_metadata(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with client.get("/.well-known/oauth-authorization-server") as resp:
        assert resp.status == 200
        doc = await resp.json()
    assert doc["issuer"] == "https://mcp.server.test"
    assert doc["authorization_endpoint"] == "https://mcp.server.test/oauth2/authorize"
    assert doc["token_endpoint"] == "https://mcp.server.test/oauth2/token"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["authorization_response_iss_parameter_supported"] is True
    assert doc["client_id_metadata_document_supported"] is True


async def test_resource_metadata_points_at_self(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with client.get("/.well-known/oauth-protected-resource/mcp") as resp:
        doc = await resp.json()
    assert doc["authorization_servers"] == ["https://mcp.server.test"]


# - Login ---------------------------------------------------------------------


async def test_authorize_redirects_to_builtin_login(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge), allow_redirects=False
    ) as resp:
        assert resp.status == 303
        location = URL(resp.headers["Location"])
    assert location.path == "/oauth2/login"
    back = URL(location.query["redirect"])
    assert back.path == "/oauth2/authorize"
    assert back.query["client_id"] == CLIENT_ID
    assert back.query["state"] == "xyz"


async def test_custom_redirect_param_name(make_client: MakeClient) -> None:
    client = await make_client(
        authorization=make_auth(login_redirect_param="next")
    )
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge), allow_redirects=False
    ) as resp:
        location = URL(resp.headers["Location"])
    assert "redirect" not in location.query
    back = location.query["next"]
    # The login form carries it under the same name, and the flow completes.
    async with client.get("/oauth2/login", params={"next": back}) as page:
        assert form_fields(await page.text())["next"] == back
    async with login(client, redirect=back, param="next") as resp:
        resume = resp.headers["Location"]
    async with client.get(resume, allow_redirects=False) as resp:
        assert resp.status == 200  # consent page


async def test_login_sets_scoped_cookie(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with login(client) as resp:
        assert resp.status == 303
        cookie = resp.cookies["aiohttp_mcp_session"]
    assert cookie["path"] == "/oauth2"
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert int(cookie["max-age"]) == 86400


async def test_login_wrong_password_reprompts(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with login(client, password="hearts") as resp:
        assert resp.status == 200
        assert "Invalid credentials" in await resp.text()
        assert "aiohttp_mcp_session" not in resp.cookies


async def test_login_open_redirect_blocked(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    for target in ("https://evil.example/", "//evil.example/x", "/other/path"):
        async with login(client, redirect=target) as resp:
            assert resp.status == 303
            assert resp.headers["Location"] == "/"


async def test_login_when_already_authenticated(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with login(client):
        pass
    _, challenge = pkce()
    back = str(URL("/oauth2/authorize").with_query(authz_query(challenge)))
    async with client.get(
        "/oauth2/login", params={"redirect": back}, allow_redirects=False
    ) as resp:
        assert resp.status == 303
        assert resp.headers["Location"] == back


async def test_session_expiry(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth(login_ttl=0.05))
    async with login(client):
        pass
    await asyncio.sleep(0.05)
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge), allow_redirects=False
    ) as resp:
        assert resp.status == 303
        assert URL(resp.headers["Location"]).path == "/oauth2/login"


async def test_app_login_url(make_client: MakeClient) -> None:
    """An app with its own login page: unauthenticated users go there."""

    async def authenticate_request(request: web.Request) -> str | None:
        return None

    client = await make_client(
        authorization=make_auth(
            check_credentials=None,
            login_url="/accounts/signin",
            authenticate_request=authenticate_request,
        )
    )
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge), allow_redirects=False
    ) as resp:
        assert resp.status == 303
        location = URL(resp.headers["Location"])
    assert location.path == "/accounts/signin"
    assert URL(location.query["redirect"]).path == "/oauth2/authorize"
    # Without check_credentials the built-in login page does not exist.
    async with client.get("/oauth2/login") as resp:
        assert resp.status == 404


async def test_app_session_skips_login(make_client: MakeClient) -> None:
    """authenticate_request (e.g. aiohttp-security) goes straight to consent."""

    async def authenticate_request(request: web.Request) -> str | None:
        return "bob@app"

    client = await make_client(
        authorization=make_auth(authenticate_request=authenticate_request)
    )
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge), allow_redirects=False
    ) as resp:
        assert resp.status == 200  # consent page immediately, no login redirect
        assert "bob@app" in await resp.text()

    grant = await grant_tokens(client)
    assert token_payload(grant["access_token"])["sub"] == "bob@app"


# - Consent -------------------------------------------------------------------


async def test_consent_page_contents(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth(resource_name="My Server"))
    _, challenge = pkce()
    fields, body = await consent_page(client, challenge)
    assert "My Server" in body
    assert CLIENT_ID in body
    assert "mcp:read" in body
    assert "alice" in body  # signed in as
    assert "password" not in body  # consent page never asks for credentials
    assert fields["consent"]
    assert fields["state"] == "xyz"
    # MUST clearly display the redirect URI hostname, and SHOULD warn for local redirects.
    assert "127.0.0.1" in body
    assert "Warning:" in body


async def test_consent_remote_redirect_no_warning(make_client: MakeClient) -> None:
    """A remote HTTPS redirect shows its hostname, without the local warning."""
    uri = "https://app.server.test/cb"
    client = await make_client(authorization=make_auth(clients={CLIENT_ID: (uri,)}))
    _, challenge = pkce()
    _, body = await consent_page(client, challenge, redirect_uri=uri)
    assert "app.server.test" in body
    assert "Warning:" not in body


async def test_consent_deny(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    async with approve(client, challenge, action="deny") as resp:
        assert resp.status == 303
        query = URL(resp.headers["Location"]).query
    assert query["error"] == "access_denied"
    assert query["iss"] == "https://mcp.server.test"


async def test_consent_token_required(make_client: MakeClient) -> None:
    """A session cookie alone must not approve: CSRF defence."""
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    fields, _ = await consent_page(client, challenge)
    for consent in ("", "garbage", fields["consent"][:-2]):
        forged = dict(fields, action="approve", consent=consent)
        async with client.post(
            "/oauth2/authorize", data=forged, allow_redirects=False
        ) as resp:
            assert resp.status == 200  # re-rendered, no code issued
            assert "Location" not in resp.headers
            assert "confirm again" in await resp.text()


async def test_consent_token_bound_to_subject(make_client: MakeClient) -> None:
    """A consent token minted for another user does not authorise approval."""

    subject = "alice"

    async def authenticate_request(request: web.Request) -> str | None:
        return subject

    client = await make_client(
        authorization=make_auth(authenticate_request=authenticate_request)
    )
    _, challenge = pkce()
    fields, _ = await consent_page(client, challenge)
    subject = "mallory"  # session changes between page render and submit
    async with client.post(
        "/oauth2/authorize", data=dict(fields, action="approve"), allow_redirects=False
    ) as resp:
        assert resp.status == 200
        assert "Location" not in resp.headers


async def test_cross_origin_consent_rejected(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    fields, _ = await consent_page(client, challenge)
    async with client.post(
        "/oauth2/authorize",
        data=dict(fields, action="approve"),
        headers={"Origin": "https://evil.example"},
        allow_redirects=False,
    ) as resp:
        assert resp.status == 403
    # Our own origin is fine.
    async with client.post(
        "/oauth2/authorize",
        data=dict(fields, action="approve"),
        headers={"Origin": "https://mcp.server.test"},
        allow_redirects=False,
    ) as resp:
        assert resp.status == 303


async def test_consent_post_unauthenticated(make_client: MakeClient) -> None:
    """Consent POST without a session redirects to login, never issues a code."""
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    fields = dict(authz_query(challenge), action="approve", consent="x")
    async with client.post("/oauth2/authorize", data=fields, allow_redirects=False) as resp:
        assert resp.status == 303
        assert URL(resp.headers["Location"]).path == "/oauth2/login"


async def test_custom_consent_page(make_client: MakeClient) -> None:
    seen: list[ConsentContext] = []

    async def render_consent(request: web.Request, context: ConsentContext) -> web.Response:
        seen.append(context)
        return web.Response(
            text=(
                '<html><body data-brand="acme"><form method="post">'
                f"{context.hidden_fields_html}"
                '<button name="action" value="approve">Go</button>'
                "</form></body></html>"
            ),
            content_type="text/html",
        )

    client = await make_client(
        authorization=make_auth(render_consent=render_consent, resource_name="Acme")
    )
    verifier, challenge = pkce()
    _, body = await consent_page(client, challenge)
    assert 'data-brand="acme"' in body

    context = seen[0]
    assert context.server_name == "Acme"
    assert context.client_id == CLIENT_ID
    assert context.subject == "alice"
    assert context.scopes == ("mcp:read",)
    assert context.error is None
    assert context.redirect_host == "127.0.0.1"
    assert context.redirect_is_loopback is True

    code = await obtain_code(client, challenge)
    async with exchange(client, code, verifier) as resp:
        assert resp.status == 200


async def test_consent_fields_escaped(make_client: MakeClient) -> None:
    """Hostile state survives the round-trip and never lands raw in the HTML."""
    client = await make_client(authorization=make_auth())
    verifier, challenge = pkce()
    evil = 'xyz"><script>alert(1)</script>'
    fields, body = await consent_page(client, challenge, state=evil)
    assert "<script>alert(1)</script>" not in body
    assert fields["state"] == evil
    async with client.post(
        "/oauth2/authorize", data=dict(fields, action="approve"), allow_redirects=False
    ) as resp:
        assert resp.status == 303
        location = URL(resp.headers["Location"])
    assert location.query["state"] == evil
    async with exchange(client, location.query["code"], verifier) as resp:
        assert resp.status == 200


# - Authorization request validation -------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "error"),
    (
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"resource": "https://other.example"}, "invalid_target"),
        ({"scope": "mcp:admin"}, "invalid_scope"),
    ),
)
async def test_authorize_redirect_errors(
    make_client: MakeClient, overrides: dict[str, str], error: str
) -> None:
    client = await make_client(authorization=make_auth())
    async with login(client):  # error redirects require an authenticated user
        pass
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize",
        params=authz_query(challenge, **overrides),
        allow_redirects=False,
    ) as resp:
        assert resp.status == 303
        query = URL(resp.headers["Location"]).query
    assert query["error"] == error
    assert query["state"] == "xyz"


async def test_error_redirect_requires_authentication(
    make_client: MakeClient,
) -> None:
    # OAuth 2.1 section 7.12.2
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    query = authz_query(challenge, scope="mcp:admin")  # will error-redirect

    # Unauthenticated: sent to login, not to the client's redirect URI.
    async with client.get(
        "/oauth2/authorize", params=query, allow_redirects=False
    ) as resp:
        assert resp.status == 303
        location = URL(resp.headers["Location"])
    assert location.path == "/oauth2/login"

    # Completing the login resumes the request, which then error-redirects.
    async with login(client, redirect=location.query["redirect"]) as resp:
        resume = resp.headers["Location"]
    async with client.get(resume, allow_redirects=False) as resp:
        assert resp.status == 303
        query_out = URL(resp.headers["Location"]).query
    assert query_out["error"] == "invalid_scope"
    assert query_out["state"] == "xyz"

    # Same rule on the POST handler.
    other = await make_client(authorization=make_auth())
    fields = dict(authz_query(challenge, scope="mcp:admin"), action="approve")
    async with other.post("/oauth2/authorize", data=fields, allow_redirects=False) as resp:
        assert resp.status == 303
        assert URL(resp.headers["Location"]).path == "/oauth2/login"


@pytest.mark.parametrize(
    "overrides",
    (
        {"client_id": "nobody"},
        {"redirect_uri": "http://evil.example/callback"},
        {"redirect_uri": ""},
    ),
)
async def test_authorize_page_errors_no_redirect(
    make_client: MakeClient, overrides: dict[str, str]
) -> None:
    """Invalid client/redirect_uri must never redirect (RFC 6749 section 4.1.2.1)."""
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize",
        params=authz_query(challenge, **overrides),
        allow_redirects=False,
    ) as resp:
        assert resp.status == 400


async def test_loopback_redirect_port_may_vary(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    verifier, challenge = pkce()
    uri = "http://127.0.0.1:54321/callback"  # registered port is 9999
    code = await obtain_code(client, challenge, redirect_uri=uri)
    async with exchange(client, code, verifier, redirect_uri=uri) as resp:
        assert resp.status == 200


async def test_cimd_client(aiohttp_client: AiohttpClient, allow_local_cimd: None) -> None:
    """A client identified by a Client ID Metadata Document URL."""
    app = web.Application()

    async def metadata(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "client_id": client_id,
                "client_name": "CIMD Test Client",
                "redirect_uris": [REDIRECT_URI],
            }
        )

    app.router.add_get("/client.json", metadata)
    setup(app, name="test-server", authorization=make_auth())
    client = await aiohttp_client(app)
    client_id = f"http://127.0.0.1:{client.port}/client.json"

    verifier, challenge = pkce()
    fields, body = await consent_page(client, challenge, client_id=client_id)
    assert "CIMD Test Client" in body
    async with client.post(
        "/oauth2/authorize", data=dict(fields, action="approve"), allow_redirects=False
    ) as resp:
        code = URL(resp.headers["Location"]).query["code"]
    async with exchange(client, code, verifier, client_id=client_id) as resp:
        assert resp.status == 200


async def test_cimd_cache_bounded(aiohttp_client: AiohttpClient, allow_local_cimd: None) -> None:
    async def metadata(request: web.Request) -> web.Response:
        cid = f"http://127.0.0.1:{client.port}/client/{request.match_info['n']}"
        return web.json_response({"client_id": cid, "redirect_uris": [REDIRECT_URI]})

    app = web.Application()
    app.router.add_get("/client/{n}", metadata)
    authorization = make_auth()
    setup(app, name="test-server", authorization=authorization)
    client = await aiohttp_client(app)

    client_ids = [f"http://127.0.0.1:{client.port}/client/{n}" for n in range(3)]
    with mock.patch("aiohttp_mcp._auth_server._CIMD_CACHE_MAX", 2):
        for client_id in client_ids:
            await authorization._resolve_client(client_id)
    assert len(authorization._cimd_cache) == 2
    assert client_ids[0] not in authorization._cimd_cache  # Oldest evicted.
    assert client_ids[2] in authorization._cimd_cache


# - Token endpoint --------------------------------------------------------------


async def test_full_flow(make_client: MakeClient) -> None:
    """401 challenge -> login -> consent -> code -> token -> MCP request."""
    client = await make_client(authorization=make_auth())

    async with client.post(
        "/mcp",
        json=request_body("server/discover"),
        headers=request_headers("server/discover"),
    ) as resp:
        assert resp.status == 401

    grant = await grant_tokens(client)
    assert grant["token_type"] == "Bearer"
    assert grant["scope"] == "mcp:read"
    assert grant["expires_in"] == 3600
    assert token_payload(grant["access_token"])["sub"] == "alice"

    headers = request_headers("server/discover")
    headers["Authorization"] = f"Bearer {grant['access_token']}"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 200
        assert (await resp.json())["result"]["resultType"] == "complete"


async def test_code_single_use(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    verifier, challenge = pkce()
    code = await obtain_code(client, challenge)
    async with exchange(client, code, verifier) as resp:
        assert resp.status == 200
    async with exchange(client, code, verifier) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_grant"


async def test_bad_verifier(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    code = await obtain_code(client, challenge)
    async with exchange(client, code, "not-the-verifier") as resp:
        assert (await resp.json())["error"] == "invalid_grant"


async def test_code_bound_to_client_and_redirect(make_client: MakeClient) -> None:
    client = await make_client(
        authorization=make_auth(
            clients={CLIENT_ID: (REDIRECT_URI,), "other": (REDIRECT_URI,)}
        )
    )
    verifier, challenge = pkce()
    code = await obtain_code(client, challenge)
    async with exchange(client, code, verifier, client_id="other") as resp:
        assert (await resp.json())["error"] == "invalid_grant"
    code = await obtain_code(client, challenge)
    async with exchange(client, code, verifier, redirect_uri="http://127.0.0.1/x") as resp:
        assert (await resp.json())["error"] == "invalid_grant"


async def test_expired_code(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth(code_ttl=0.05))
    verifier, challenge = pkce()
    code = await obtain_code(client, challenge)
    await asyncio.sleep(0.05)
    async with exchange(client, code, verifier) as resp:
        assert (await resp.json())["error"] == "invalid_grant"


async def test_wrong_resource_at_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    verifier, challenge = pkce()
    code = await obtain_code(client, challenge)
    async with exchange(client, code, verifier, resource="https://other.example") as resp:
        assert (await resp.json())["error"] == "invalid_target"


async def test_tampered_token_rejected(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client)
    headers = request_headers("server/discover")
    headers["Authorization"] = "Bearer " + grant["access_token"][:-2]
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 401


async def test_unsupported_grant_type(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    async with client.post("/oauth2/token", data={"grant_type": "client_credentials"}) as resp:
        assert resp.status == 400
        assert (await resp.json())["error"] == "unsupported_grant_type"


async def test_default_scope_grant(make_client: MakeClient) -> None:
    """Omitting scope grants everything in default_scopes."""
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client, scope="")
    assert grant["scope"] == "mcp:read"


# - Refresh tokens ---------------------------------------------------------------


async def test_refresh_grant(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client)
    assert "refresh_token" in grant

    async with refresh(client, grant["refresh_token"]) as resp:
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-store"
        renewed = await resp.json()
    assert renewed["token_type"] == "Bearer"
    assert renewed["scope"] == "mcp:read"
    assert renewed["refresh_token"] != grant["refresh_token"]

    headers = request_headers("server/discover")
    headers["Authorization"] = f"Bearer {renewed['access_token']}"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 200


async def test_refresh_reuse_revokes_family(make_client: MakeClient) -> None:
    """Replaying a rotated-out refresh token kills the whole grant chain."""
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client)
    async with refresh(client, grant["refresh_token"]) as resp:
        renewed = await resp.json()

    async with refresh(client, grant["refresh_token"]) as resp:  # replay the old one
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_grant"

    async with refresh(client, renewed["refresh_token"]) as resp:  # newest is dead too
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_grant"


async def test_refresh_scope_narrowing(make_client: MakeClient) -> None:
    client = await make_client(
        authorization=make_auth(default_scopes=("mcp:read", "mcp:write"))
    )
    grant = await grant_tokens(client, scope="mcp:read mcp:write")
    async with refresh(client, grant["refresh_token"], scope="mcp:read") as resp:
        renewed = await resp.json()
    assert renewed["scope"] == "mcp:read"

    async with refresh(client, renewed["refresh_token"], scope="mcp:admin") as resp:
        assert (await resp.json())["error"] == "invalid_scope"
    async with refresh(client, renewed["refresh_token"]) as resp:
        assert (await resp.json())["scope"] == "mcp:read mcp:write"


async def test_refresh_family_expiry(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth(refresh_ttl=0.1))
    grant = await grant_tokens(client)
    async with refresh(client, grant["refresh_token"]) as resp:
        renewed = await resp.json()
    await asyncio.sleep(0.1)
    async with refresh(client, renewed["refresh_token"]) as resp:
        assert (await resp.json())["error"] == "invalid_grant"


async def test_refresh_token_not_an_access_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client)
    headers = request_headers("server/discover")
    headers["Authorization"] = f"Bearer {grant['refresh_token']}"
    async with client.post(
        "/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 401


async def test_access_token_not_a_refresh_token(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client)
    async with refresh(client, grant["access_token"]) as resp:
        assert (await resp.json())["error"] == "invalid_grant"


async def test_refresh_disabled(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth(refresh_ttl=None))
    async with client.get("/.well-known/oauth-authorization-server") as resp:
        assert (await resp.json())["grant_types_supported"] == ["authorization_code"]
    grant = await grant_tokens(client)
    assert "refresh_token" not in grant
    async with refresh(client, "anything") as resp:
        assert resp.status == 400
        assert (await resp.json())["error"] == "unsupported_grant_type"


async def test_refresh_survives_restart_amnesty(make_client: MakeClient) -> None:
    """An unknown family (fresh process, stable secret) is still accepted."""
    client = await make_client(authorization=make_auth())
    grant = await grant_tokens(client)
    client2 = await make_client(authorization=make_auth())
    async with refresh(client2, grant["refresh_token"]) as resp:
        assert resp.status == 200


# - Configuration ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    (
        {"check_credentials": None},  # no way to log in
        {"check_credentials": None, "login_url": "/login"},  # no way to see it
        {"login_redirect_param": ""},
        {"login_ttl": 0},
    ),
)
async def test_invalid_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make_auth(**kwargs)  # type: ignore[arg-type]


async def test_sub_application(aiohttp_client: AiohttpClient) -> None:
    resource = "https://mcp.server.test/api/mcp"
    auth = make_auth(resource="https://mcp.server.test/api")
    api = web.Application()
    setup(api, name="test-server", authorization=auth, well_known_routes=False)
    root = web.Application()
    root.add_subapp("/api", api)
    auth.register_well_known(root)
    client = await aiohttp_client(root)

    # A path-bearing issuer: its RFC 8414 document lives at the origin root,
    # path-inserted, and advertises the endpoints under the prefix.
    async with client.get("/.well-known/oauth-authorization-server/api") as resp:
        assert resp.status == 200
        doc = await resp.json()
    assert doc["issuer"] == "https://mcp.server.test/api"
    assert doc["authorization_endpoint"] == "https://mcp.server.test/api/oauth2/authorize"
    assert doc["token_endpoint"] == "https://mcp.server.test/api/oauth2/token"
    async with client.get("/.well-known/oauth-protected-resource/api/mcp") as resp:
        doc = await resp.json()
    assert doc["resource"] == resource
    assert doc["authorization_servers"] == ["https://mcp.server.test/api"]
    async with client.get("/api/.well-known/oauth-protected-resource/api/mcp") as resp:
        assert resp.status == 404  # Never under the sub-application prefix

    # The endpoints exist under the prefix only.
    async with client.get("/api/oauth2/login") as resp:
        assert resp.status == 200
    async with client.get("/oauth2/login") as resp:
        assert resp.status == 404

    # The challenge from the sub-app endpoint points at the root metadata document.
    async with client.post(
        "/api/mcp",
        json=request_body("server/discover"),
        headers=request_headers("server/discover"),
    ) as resp:
        assert resp.status == 401
        expected = "https://mcp.server.test/.well-known/oauth-protected-resource/api/mcp"
        assert f'resource_metadata="{expected}"' in resp.headers["WWW-Authenticate"]

    # Full flow through the prefixed endpoints.
    verifier, challenge = pkce()
    async with client.get(
        "/api/oauth2/authorize",
        params=authz_query(challenge, resource=resource),
        allow_redirects=False,
    ) as resp:
        assert resp.status == 303
        location = URL(resp.headers["Location"])
    assert location.path == "/api/oauth2/login"
    async with client.post(
        "/api/oauth2/login",
        data={
            "username": "alice",
            "password": "wonderland",
            "redirect": location.query["redirect"],
        },
        allow_redirects=False,
    ) as resp:
        assert resp.status == 303
        assert resp.headers["Location"].startswith("/api/oauth2/authorize")
        cookie = resp.cookies["aiohttp_mcp_session"]
        assert cookie["path"] == "/api/oauth2"  # still scoped to the endpoints
        resume = resp.headers["Location"]

    async with client.get(resume, allow_redirects=False) as resp:
        assert resp.status == 200  # consent page
        fields = form_fields(await resp.text())
    fields["action"] = "approve"
    async with client.post(
        "/api/oauth2/authorize", data=fields, allow_redirects=False
    ) as resp:
        assert resp.status == 303
        location = URL(resp.headers["Location"])
    assert location.query["iss"] == "https://mcp.server.test/api"

    async with client.post(
        "/api/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": location.query["code"],
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "resource": resource,
        },
    ) as resp:
        assert resp.status == 200
        grant = await resp.json()
    headers = request_headers("server/discover")
    headers["Authorization"] = f"Bearer {grant['access_token']}"
    async with client.post(
        "/api/mcp", json=request_body("server/discover"), headers=headers
    ) as resp:
        assert resp.status == 200
        assert (await resp.json())["result"]["resultType"] == "complete"


async def test_insecure_redirect_uris_rejected(
    aiohttp_client: AiohttpClient, allow_local_cimd: None
) -> None:
    """Redirect URIs MUST be either localhost or use HTTPS."""
    # Statically registered clients: rejected at construction.
    with pytest.raises(ValueError, match="localhost or HTTPS"):
        make_auth(clients={CLIENT_ID: ("http://app.example.com/cb",)})
    # https anywhere and http on loopback are both fine.
    make_auth(clients={CLIENT_ID: ("https://app.example.com/cb",)})
    make_auth(clients={CLIENT_ID: ("http://localhost:1234/cb",)})

    # CIMD documents: an insecure redirect URI invalidates the document.
    app = web.Application()

    async def metadata(request: web.Request) -> web.Response:
        return web.json_response(
            {"client_id": client_id, "redirect_uris": ["http://evil.example/cb"]}
        )

    app.router.add_get("/client.json", metadata)
    setup(app, name="test-server", authorization=make_auth())
    client = await aiohttp_client(app)
    client_id = f"http://127.0.0.1:{client.port}/client.json"
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize",
        params=authz_query(challenge, client_id=client_id),
        allow_redirects=False,
    ) as resp:
        assert resp.status == 400
        assert "Invalid client metadata" in await resp.text()


def test_signer_payload_tripwire(caplog: pytest.LogCaptureFixture) -> None:
    signer = _Signer(SECRET)
    with caplog.at_level(logging.ERROR, logger="aiohttp_mcp"):
        assert signer.verify("garbage") is None
        assert signer.verify("Z2FyYmFnZQ.wrong-signature") is None
        assert not caplog.records

        for payload in (b"not json", b"[1, 2]"):
            body = _b64(payload)
            forged = f"{body}.{_b64(hmac.digest(SECRET, body.encode(), 'sha256'))}"
            assert signer.verify(forged) is None
        assert len(caplog.records) == 2
        assert all("signed token" in r.message for r in caplog.records)


async def test_grantable_broader_than_default(make_client: MakeClient) -> None:
    client = await make_client(
        authorization=make_auth(
            default_scopes=("mcp:read",),
            grantable_scopes=("mcp:read", "mcp:write"),
        )
    )
    # The RS document advertises the starting set, the AS document the full set.
    async with client.get("/.well-known/oauth-protected-resource/mcp") as resp:
        prm = await resp.json()
    assert prm["scopes_supported"] == ["mcp:read"]
    async with client.get("/.well-known/oauth-authorization-server") as resp:
        asm = await resp.json()
    assert asm["scopes_supported"] == ["mcp:read", "mcp:write"]

    # An unspecific request grants the default set, not everything.
    grant = await grant_tokens(client, scope="")
    assert grant["scope"] == "mcp:read"
    # The broader scope is grantable when requested (step-up).
    grant = await grant_tokens(client, scope="mcp:read mcp:write")
    assert grant["scope"] == "mcp:read mcp:write"


async def test_grantable_scope_config_errors() -> None:
    with pytest.raises(ValueError, match="grantable"):
        make_auth(default_scopes=("mcp:read",), grantable_scopes=("mcp:write",))
    with pytest.raises(ValueError, match="scope token"):
        make_auth(default_scopes=(), grantable_scopes=("bad scope",))


async def test_cimd_redirects_not_followed(
    aiohttp_client: AiohttpClient, allow_local_cimd: None
) -> None:
    app = web.Application()

    async def bounce(request: web.Request) -> web.Response:
        raise web.HTTPFound(f"http://127.0.0.1:{client.port}/evil.json")

    async def evil(request: web.Request) -> web.Response:
        return web.json_response({"client_id": client_id, "redirect_uris": [REDIRECT_URI]})

    app.router.add_get("/client.json", bounce)
    app.router.add_get("/evil.json", evil)
    setup(app, name="test-server", authorization=make_auth())
    client = await aiohttp_client(app)
    client_id = f"http://127.0.0.1:{client.port}/client.json"

    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize",
        params=authz_query(challenge, client_id=client_id),
        allow_redirects=False,
    ) as resp:
        assert resp.status == 400
        assert "client metadata" in await resp.text()


@pytest.mark.parametrize(
    "client_id",
    (
        "http://public.example/client.json",  # not https
        "https://127.0.0.1/client.json",  # loopback
        "https://[::1]/client.json",  # loopback, v6
        "https://10.0.0.5/client.json",  # private
        "https://192.168.1.10/client.json",  # private
        "https://169.254.1.1/client.json",  # link-local
        "https://localhost/client.json",  # hostname resolving to loopback
    ),
)
async def test_cimd_private_targets_rejected(make_client: MakeClient, client_id: str) -> None:
    """SSRF guard: client_id documents are only fetched from public hosts."""
    client = await make_client(authorization=make_auth())
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize",
        params=authz_query(challenge, client_id=client_id),
        allow_redirects=False,
    ) as resp:
        assert resp.status == 400
        assert "Unknown client_id" in await resp.text()


async def test_consent_never_skipped(make_client: MakeClient) -> None:
    client = await make_client(authorization=make_auth())
    verifier, challenge = pkce()
    code = await obtain_code(client, challenge)
    async with exchange(client, code, verifier) as resp:
        assert resp.status == 200

    # Same user, same client, fresh authorization request:
    # skip the login page, but the consent page must be rendered again
    _, challenge = pkce()
    async with client.get(
        "/oauth2/authorize", params=authz_query(challenge), allow_redirects=False
    ) as resp:
        assert resp.status == 200
        assert "Approve" in await resp.text()
