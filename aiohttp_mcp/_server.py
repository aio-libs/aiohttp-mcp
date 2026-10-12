"""Internal server core.

The logic for an MCP server, sans-IO.
"""

import asyncio
from collections.abc import Callable, Collection, Mapping
from ipaddress import ip_address
from typing import Literal

from aiohttp import web
from yarl import URL

from ._protocol import (
    INVALID_PARAMS,
    META_SUBSCRIPTION_ID,
    PROTOCOL_VERSIONS,
    DiscoverResult,
    Implementation,
    JSONRPCNotification,
    MCPError,
    RequestId,
    SubscriptionFilter,
    SubscriptionsListenResult
)
from ._transport import MCPRequest, Method, handle_request


class Subscription:
    """A live ``subscriptions/listen`` stream."""

    __slots__ = ("closing", "id")

    def __init__(self, request_id: RequestId) -> None:
        self.id = request_id
        # Set to end the subscription gracefully (server-initiated closure).
        self.closing = asyncio.Event()


class MCPServer:
    """Internal per-endpoint state."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        title: str | None,
        description: str | None,
        website_url: str | None,
        instructions: str | None,
        include_server_info: bool,
        discover_ttl: float,
        discover_cache_scope: Literal["public", "private"],
        allowed_origins: Collection[str] | Callable[[str], bool],
        sse_keepalive: float,
    ) -> None:
        if not name:
            raise ValueError("name must be a non-empty string")
        if not version:
            raise ValueError("version must be a non-empty string")
        if discover_ttl < 0:
            raise ValueError("discover_ttl must be >= 0")
        if discover_cache_scope not in {"public", "private"}:
            raise ValueError("discover_cache_scope must be 'public' or 'private'")
        if sse_keepalive <= 0:
            raise ValueError("sse_keepalive must be > 0")

        self.server_info: Implementation = {"name": name, "version": version}
        if title is not None:
            self.server_info["title"] = title
        if description is not None:
            self.server_info["description"] = description
        if website_url is not None:
            self.server_info["websiteUrl"] = website_url
        self.include_server_info = include_server_info
        self.sse_keepalive = sse_keepalive
        self._instructions = instructions
        self._discover_ttl_ms = round(discover_ttl * 1000)
        self._discover_cache_scope = discover_cache_scope
        self._allowed_origins = allowed_origins
        self.subscriptions: set[Subscription] = set()
        self.methods: Mapping[str, Method] = {
            "server/discover": Method(self._discover),
            "subscriptions/listen": Method(self._listen, streaming=True),
        }

    def origin_allowed(self, origin: str) -> bool:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#security-&-endpoint

        # Normalise origin
        try:
            host = URL(origin).host
        except ValueError:
            return False

        if host is None:
            return False
        if callable(self._allowed_origins):
            return self._allowed_origins(host)
        return host in self._allowed_origins

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """The MCP endpoint."""
        return await handle_request(self, request)

    async def shutdown(self, app: web.Application) -> None:
        for subscription in self.subscriptions:
            subscription.closing.set()

    async def _discover(self, request: MCPRequest) -> DiscoverResult:
        """server/discover endpoint."""
        result: DiscoverResult = {
            "supportedVersions": PROTOCOL_VERSIONS,
            "capabilities": {},
            "ttlMs": self._discover_ttl_ms,
            "cacheScope": self._discover_cache_scope,
        }
        if self._instructions is not None:
            result["instructions"] = self._instructions
        return result

    async def _listen(self, request: MCPRequest) -> SubscriptionsListenResult:
        """subscriptions/listen endpoint."""
        notifications = request.params.get("notifications", {})
        if not isinstance(notifications, Mapping):
            raise MCPError(INVALID_PARAMS, "notifications must be an object")

        subscription = Subscription(request.id)
        self.subscriptions.add(subscription)
        notification: JSONRPCNotification = {
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "_meta": {META_SUBSCRIPTION_ID: request.id},
                "notifications": {},
            },
        }
        try:
            # https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions#acknowledgment
            await request._emit(notification)
            await subscription.closing.wait()
        finally:
            self.subscriptions.discard(subscription)

        # https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions#graceful-closure
        return {"_meta": {META_SUBSCRIPTION_ID: request.id}}
