import html
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp_mcp
from aiohttp import web

check_credentials: Callable[[str, str, web.Request], Awaitable[bool]]
introspect: Callable[[str], Mapping[str, Any]]


def self_contained(app: web.Application) -> None:
    async def check_credentials(username: str, password: str, request: web.Request) -> bool:
        return username == "admin" and secrets.compare_digest(
            password, os.environ["MCP_PASSWORD"]
        )

    aiohttp_mcp.setup(
        app,
        authorization=aiohttp_mcp.LocalAuthorization(
            check_credentials,
            resource="https://mcp.server.example",
            secret=bytes.fromhex(os.environ["MCP_TOKEN_KEY"]),
        ),
    )


def app_login(app: web.Application) -> None:
    from aiohttp_security import authorized_userid

    aiohttp_mcp.setup(
        app,
        authorization=aiohttp_mcp.LocalAuthorization(
            resource="https://mcp.server.example",
            secret=bytes.fromhex(os.environ["MCP_TOKEN_KEY"]),
            authenticate_request=authorized_userid,
            login_url=str(app.router["login"].url_for()),
        ),
    )


async def render_consent(request: web.Request, ctx: aiohttp_mcp.ConsentContext) -> web.StreamResponse:
    # The client name is self-asserted; where the code is sent is not. The
    # page must display ctx.redirect_host, and should warn when the code
    # goes to the local machine (any local program could be listening).
    warning = "<p><b>Warning:</b> code goes to a local app.</p>" if ctx.redirect_is_loopback else ""
    return web.Response(
        content_type="text/html",
        text=f"""
        <html><body class="my-brand">
          <h1>Authorize access to {html.escape(ctx.server_name)}</h1>
          <p>{html.escape(ctx.client_name)} is requesting access
             for <b>{html.escape(ctx.subject)}</b>.</p>
          <p>The code will be sent to <b>{html.escape(ctx.redirect_host)}</b>.</p>
          {warning}
          <form method="post">
            {ctx.hidden_fields_html}
            <button name="action" value="approve">Approve</button>
            <button name="action" value="deny">Deny</button>
          </form>
        </body></html>""",
    )


def jwt_authorization(app: web.Application) -> None:
    from aiohttp_mcp.jwt import JWTAuthorization

    aiohttp_mcp.setup(
        app,
        authorization=JWTAuthorization(
            issuer="https://auth.server.example",
            resource="https://mcp.server.example",
        ),
    )


def introspection_authorization(app: web.Application) -> None:
    aiohttp_mcp.setup(
        app,
        authorization=aiohttp_mcp.IntrospectionAuthorization(
            issuer="https://auth.server.example",
            client_id="my-mcp-server",
            client_secret=os.environ["INTROSPECTION_SECRET"],
            resource="https://mcp.server.example",
            cache_ttl=30.0,
        ),
    )


def external_authorization_server(app: web.Application) -> None:
    resource_name = "https://mcp.server.example/mcp"

    async def validate_token(token: str, request: web.Request) -> aiohttp_mcp.TokenInfo | None:
        claims = introspect(token)  # e.g. RFC 7662 token introspection
        if not claims.get("active") or resource_name not in claims.get("aud", ()):
            return None
        return aiohttp_mcp.TokenInfo(
            scopes=frozenset(claims.get("scope", "").split()),
            subject=claims.get("sub"),
            claims=claims,
        )

    aiohttp_mcp.setup(
        app,
        authorization=aiohttp_mcp.Authorization(
            validate_token,
            resource="https://mcp.server.example",
            auth_servers=("https://auth.server.example",),
            default_scopes=("mcp:read",),
        ),
    )


def sub_application(root: web.Application) -> None:
    api = web.Application()
    auth = aiohttp_mcp.LocalAuthorization(
        check_credentials,
        resource="https://mcp.server.example/api",
        secret=bytes.fromhex(os.environ["MCP_TOKEN_KEY"]),
    )
    aiohttp_mcp.setup(api, authorization=auth, well_known_routes=False)
    root.add_subapp("/api", api)
    auth.register_well_known(root)


def token_handlers(request: web.Request) -> None:
   token_info = request[aiohttp_mcp.TOKEN_INFO_KEY]
