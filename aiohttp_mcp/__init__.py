"""MCP (Model Context Protocol) server support for aiohttp."""

from collections.abc import Callable, Collection
from typing import Literal

from aiohttp import web

from ._protocol import PROTOCOL_VERSIONS
from ._server import MCPServer

__version__ = "0.1.0"
__all__ = ("PROTOCOL_VERSIONS", "setup")


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
    allowed_origins: Collection[str] | Callable[[str], bool] = ("localhost", "127.0.0.1", "::1"),
    sse_keepalive: float = 15.0,
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
        that host and replacing the policy. The default accepts allows localhost,
        change to your domain names for remote deployments.
    :param sse_keepalive: Interval in seconds between SSE keep-alive comments
        on response streams; also bounds how quickly a client disconnect is
        detected and the in-flight handler cancelled.
    """
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
    )
    app.router.add_post(path, server.handle)
    app.on_shutdown.append(server.shutdown)
    return server
