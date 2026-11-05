"""Internal server core.

The logic for an MCP server, sans-IO.
"""

import asyncio
import secrets
from collections.abc import Callable, Collection, Mapping
from typing import Literal, NoReturn

from aiohttp import web
from yarl import URL

from ._auth import Authorization
from ._auth_server import AuthenticateRequest, _Signer
from ._elicitation import _PendingForm
from ._prompts import PromptRegistry
from ._protocol import (
    INVALID_PARAMS,
    META_SUBSCRIPTION_ID,
    PROTOCOL_VERSIONS,
    DiscoverResult,
    Implementation,
    JSONRPCNotification,
    MCPError,
    RequestId,
    ServerCapabilities,
    SubscriptionFilter,
    SubscriptionsListenResult,
)
from ._resources import ResourceRegistry
from ._secure_form import RenderForm, handle_form, render_form_default
from ._tools import ToolRegistry
from ._transport import MCPRequest, Method, handle_request


class Subscription:
    """A live ``subscriptions/listen`` stream."""

    __slots__ = ("_queue", "id", "resource_uris")

    def __init__(self, request_id: RequestId, resource_uris: frozenset[str]) -> None:
        self.id = request_id
        self.resource_uris = resource_uris
        self._queue: asyncio.Queue[JSONRPCNotification | None] = asyncio.Queue()

    def _deliver(self, notification: JSONRPCNotification) -> None:
        self._queue.put_nowait(notification)

    def _close(self) -> None:
        """End the subscription gracefully (server-initiated closure)."""
        self._queue.put_nowait(None)


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
        authorization: Authorization | None,
        prompts: PromptRegistry | None,
        resources: ResourceRegistry | None,
        tools: ToolRegistry | None,
        state_secret: bytes | None,
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

        self._server_info: Implementation = {"name": name, "version": version}
        if title is not None:
            self._server_info["title"] = title
        if description is not None:
            self._server_info["description"] = description
        if website_url is not None:
            self._server_info["websiteUrl"] = website_url
        self._include_server_info = include_server_info
        self._sse_keepalive = sse_keepalive
        self._instructions = instructions
        self._discover_ttl_ms = round(discover_ttl * 1000)
        self._discover_cache_scope = discover_cache_scope
        self._allowed_origins = allowed_origins
        self._authorization = authorization
        # Signs multi round-trip request state (elicitation).
        self._state_signer = _Signer(
            state_secret if state_secret is not None else secrets.token_bytes(32)
        )
        self._subscriptions: set[Subscription] = set()
        # Secure-form elicitations
        self._forms: dict[str, _PendingForm] = {}
        self._form_expiries: list[tuple[float, str]] = []
        self._form_identity: AuthenticateRequest | None = None
        self._form_login: Callable[[web.Request], NoReturn] | None = None
        self._form_render: RenderForm = render_form_default
        self._form_resource: web.Resource | None = None
        self._methods: dict[str, Method] = {
            "server/discover": Method(self._discover),
            "subscriptions/listen": Method(self._listen, streaming=True),
        }
        self._capabilities: ServerCapabilities = {}

        if prompts is not None:
            if authorization is None and prompts._scoped:
                raise ValueError("prompts with scopes require authorization to be configured")
            self._capabilities["prompts"] = {}
            self._methods["prompts/list"] = Method(prompts._list)
            self._methods["prompts/get"] = Method(
                prompts._get, scopes=prompts._get_scopes, input_required=True
            )

        self._resources = resources
        if resources is not None:
            if authorization is None and resources._scoped:
                raise ValueError("resources with scopes require authorization")
            self._capabilities["resources"] = {"subscribe": True}
            self._methods["resources/list"] = Method(resources._list)
            self._methods["resources/templates/list"] = Method(resources._templates_list)
            self._methods["resources/read"] = Method(
                resources._read, scopes=resources._get_scopes, input_required=True
            )

        if tools is not None:
            if authorization is None and tools._scoped:
                raise ValueError("tools with scopes require authorization")
            self._capabilities["tools"] = {}
            self._methods["tools/list"] = Method(tools._list)
            self._methods["tools/call"] = Method(
                tools._call, scopes=tools._get_scopes, input_required=True
            )

    def host_allowed(self, host: str | None) -> bool:
        """Whether ``host`` is one this server is legitimately addressed as.

        The ``allowed_origins`` policy from :func:`~aiohttp_mcp.setup`.
        Application handlers should check this before building an absolute
        URL on the request's origin - the ``Host`` header behind
        :attr:`~aiohttp.web.BaseRequest.url` is client-controlled.
        """
        if host is None:
            return False
        if callable(self._allowed_origins):
            return self._allowed_origins(host)
        return host in self._allowed_origins

    def _origin_allowed(self, origin: str) -> bool:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#security-&-endpoint

        # Normalise origin
        try:
            host = URL(origin).host
        except ValueError:
            return False
        return self.host_allowed(host)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """The MCP endpoint."""
        return await handle_request(self, request)

    async def _handle_form(self, request: web.Request) -> web.StreamResponse:
        """A secure-form elicitation page (GET renders, POST submits)."""
        return await handle_form(self, request)

    async def _shutdown(self, app: web.Application) -> None:
        for subscription in self._subscriptions:
            subscription._close()

    def resource_updated(self, uri: str) -> None:
        """Notify subscribed clients that a resource's contents changed."""
        for subscription in self._subscriptions:
            if uri in subscription.resource_uris:
                subscription._deliver(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/resources/updated",
                        "params": {
                            "_meta": {META_SUBSCRIPTION_ID: subscription.id},
                            "uri": uri,
                        },
                    }
                )

    async def _discover(self, request: MCPRequest) -> DiscoverResult:
        """server/discover endpoint."""
        result: DiscoverResult = {
            "supportedVersions": PROTOCOL_VERSIONS,
            "capabilities": self._capabilities,
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

        # The acknowledged filter reflects the subset the server will honor.
        honored: SubscriptionFilter = {}
        resource_uris = frozenset[str]()
        requested = notifications.get("resourceSubscriptions")
        if requested is not None:
            if not isinstance(requested, list) or not all(
                isinstance(uri, str) for uri in requested
            ):
                raise MCPError(INVALID_PARAMS, "resourceSubscriptions must be a list of URIs")
            if self._resources is not None and requested:
                resource_uris = frozenset(requested)
                honored["resourceSubscriptions"] = sorted(resource_uris)

        subscription = Subscription(request.id, resource_uris)
        self._subscriptions.add(subscription)
        notification: JSONRPCNotification = {
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "_meta": {META_SUBSCRIPTION_ID: request.id},
                "notifications": honored,
            },
        }
        try:
            # https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions#acknowledgment
            await request._emit(notification)
            while (update := await subscription._queue.get()) is not None:
                await request._emit(update)
        finally:
            self._subscriptions.discard(subscription)

        # https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions#graceful-closure
        return {"_meta": {META_SUBSCRIPTION_ID: request.id}}
