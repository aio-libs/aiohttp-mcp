"""MCP (Model Context Protocol) server support for aiohttp."""

from collections.abc import Callable, Collection
from typing import Final, Literal

from aiohttp import web
from yarl import URL


from ._auth import TOKEN_INFO_KEY, Authorization, ScopeMatcher, TokenInfo, TokenValidator
from ._auth_server import (
    AuthenticateRequest,
    CheckCredentials,
    ConsentContext,
    LocalAuthorization,
    RenderConsent,
)
from ._protocol import PROTOCOL_VERSIONS
from ._server import MCPServer
from ._validators import IntrospectionAuthorization

__version__ = "0.1.0"

# https://github.com/sphinx-doc/sphinx/issues/6495
#: Protocol revisions implemented, newest first.
PROTOCOL_VERSIONS: tuple[str, ...]  # type: ignore[no-redef]
#: The :class:`~aiohttp.web.Request` key under which the validated
#: :class:`TokenInfo` is stored for authenticated requests.
TOKEN_INFO_KEY: web.RequestKey[TokenInfo]  # type: ignore[no-redef]

__all__ = (
    "PROTOCOL_VERSIONS",
    "TOKEN_INFO_KEY",
    "AuthenticateRequest",
    "Authorization",
    "CheckCredentials",
    "ConsentContext",
    "IntrospectionAuthorization",
    "LocalAuthorization",
    "RenderConsent",
    "ScopeMatcher",
    "TokenInfo",
    "TokenValidator",
    "setup",
)


def setup(
    app: web.Application,
    *,
    name: str = "aiohttp-mcp server",
    version: str = __version__,
    path: str = "/mcp",
    title: str | None = None,
    description: str | None = None,
    website_url: str | None = None,
    instructions: str | None = None,
    include_server_info: bool = True,
    discover_ttl: float = 3600.0,
    discover_cache_scope: Literal["public", "private"] = "public",
    allowed_origins: Collection[str] | Callable[[str], bool] | None = None,
    sse_keepalive: float = 15.0,
    authorization: Authorization | None = None,
    well_known_routes: bool = True,
) -> MCPServer:
    """Serve an MCP endpoint at ``path`` on ``app``.

    :param name: Server name, reported as ``serverInfo`` and by ``server/discover``.
    :param version: Version of the server implementation.
    :param path: path for the MCP endpoint.
    :param title: Optional human-readable display name.
    :param description: Optional human-readable description of the server.
    :param website_url: Optional URL of the server's website.
    :param instructions: Optional natural-language guidance for LLMs, returned
        by ``server/discover``.
    :param include_server_info: Include ``serverInfo`` in every result's
        ``_meta`` (recommended by the specification).
    :param discover_ttl: Client-side cache lifetime of the ``server/discover``
        result, in seconds.
    :param discover_cache_scope: Whether shared intermediaries may cache the
        ``server/discover`` result (``"public"``) or only the requesting
        client (``"private"``).
    :param allowed_origins: Hosts allowed to appear in the ``Origin`` header
        (compared against the header's parsed host), or a callable receiving
        that host and replacing the policy. Defaults to the host of
        ``authorization``'s resource when authorization is configured, and to
        ``localhost`` otherwise.
    :param sse_keepalive: Interval in seconds between SSE keep-alive comments
        on response streams; also bounds how quickly a client disconnect is
        detected.
    :param authorization: Enable authorization using :brand:`OAuth` tokens.
    :param well_known_routes: Register the well-known discovery
        documents on ``app``.  Set to :data:`False` when ``app`` is mounted as a
        sub-application - those documents must be served from the origin
        root, so call :meth:`Authorization.register_well_known` on the
        root application instead.
    """
    if authorization is not None:
        authorization._bind(path)
    if allowed_origins is None:
        if authorization is not None:
            host = URL(authorization._resource).host
            assert host is not None  # enforced by Authorization
            allowed_origins = (host,)
        else:
            allowed_origins = ("localhost", "127.0.0.1", "::1")
    server = MCPServer(
        name=name,
        version=version,
        title=title,
        description=description,
        website_url=website_url,
        instructions=instructions,
        include_server_info=include_server_info,
        discover_ttl=discover_ttl,
        discover_cache_scope=discover_cache_scope,
        allowed_origins=allowed_origins,
        sse_keepalive=sse_keepalive,
        authorization=authorization,
    )
    app.router.add_post(path, server._handle)
    if authorization is not None:
        authorization._register_routes(app)
        if well_known_routes:
            authorization.register_well_known(app)
    app.on_shutdown.append(server._shutdown)
    return server
