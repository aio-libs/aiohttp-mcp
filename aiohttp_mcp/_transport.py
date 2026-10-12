"""Streamable HTTP transport binding."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from aiohttp import web
from aiohttp_sse import EventSourceResponse, sse_response

from ._protocol import (
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_HDR,
    METHOD_NOT_FOUND,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    NAME_HDR,
    NAME_HDR_METHODS,
    PARSE_ERROR,
    PROTOCOL_VERSION_HDR,
    PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    ClientCapabilities,
    Implementation,
    InputResult,
    JSONRPCErrorResponse,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResultResponse,
    MCPError,
    RequestId,
    RequestMeta,
    Result,
    ResultMeta,
    decode_header_value,
    error_envelope,
    validate_request_meta,
)

if TYPE_CHECKING:
    from ._server import MCPServer

logger = logging.getLogger("aiohttp_mcp")


class MCPRequest:
    """An in-flight MCP request, as seen by a method handler."""

    __slots__ = ("_resp", "http", "id", "meta", "method", "params")

    def __init__(
        self,
        method: str,
        request_id: RequestId,
        params: Mapping[str, object],
        meta: RequestMeta,
        http: web.Request,
    ) -> None:
        self.method = method
        self.id = request_id
        self.params = params
        self.meta = meta
        self.http = http
        self._resp: EventSourceResponse | None = None

    @property
    def client_info(self) -> Implementation | None:
        return self.meta.get(META_CLIENT_INFO)

    @property
    def client_capabilities(self) -> ClientCapabilities:
        return self.meta[META_CLIENT_CAPABILITIES]

    async def _emit(self, message: JSONRPCNotification) -> None:
        if self._resp is None:
            raise RuntimeError("Method is not registered as streaming")
        await self._resp.send(json.dumps(message, separators=(",", ":")))


Handler = Callable[[MCPRequest], Coroutine[None, None, InputResult | None]]


@dataclass(frozen=True, slots=True)
class Method:
    """A registered RPC method."""

    handler: Handler
    streaming: bool = False


async def handle_request(server: MCPServer, request: web.Request) -> web.StreamResponse:
    origin = request.headers.get("Origin")
    if origin is not None and not server.origin_allowed(origin):
        error = MCPError(INVALID_REQUEST, "Origin not allowed")
        return web.json_response(error_envelope(error), status=403)

    try:
        message = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        error = MCPError(PARSE_ERROR, "Parse error")
        return web.json_response(error_envelope(error), status=error.http_status)

    try:
        request_id, params = _validate_message(message)
    except MCPError as exc:
        return web.json_response(error_envelope(exc), status=exc.http_status)

    try:
        return await _handle_rpc_request(server, request, message, request_id, params)
    except MCPError as exc:
        return web.json_response(error_envelope(exc, request_id), status=exc.http_status)


def _validate_message(message: object) -> tuple[RequestId, dict[str, object]]:
    """Validate the JSON-RPC envelope; return the request id and params."""
    match message:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic#requests
        case {
            "jsonrpc": "2.0",
            "method": str(),
            "id": str() | int() as request_id,
        } if not isinstance(request_id, bool):
            # https://github.com/python/mypy/issues/17622
            params = message.get("params", {})  # type: ignore[attr-defined]
            if not isinstance(params, dict):
                raise MCPError(INVALID_REQUEST, "Invalid Request")
            return request_id, params
        case _:
            raise MCPError(INVALID_REQUEST, "Invalid Request")


def _validate_headers(
    request: web.Request, method: str, params: Mapping[str, object], meta_version: str
) -> None:
    # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-validation
    header_version = request.headers.get(PROTOCOL_VERSION_HDR)
    if header_version is None:
        raise MCPError(HEADER_MISMATCH, f"Missing {PROTOCOL_VERSION_HDR} header")
    if header_version != meta_version:
        raise MCPError(
            HEADER_MISMATCH,
            f"{PROTOCOL_VERSION_HDR} header value {header_version!r} does not "
            f"match body value {meta_version!r}",
        )
    if header_version not in PROTOCOL_VERSIONS:
        raise MCPError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            data={"supported": list(PROTOCOL_VERSIONS), "requested": header_version},
        )

    header_method = request.headers.get(METHOD_HDR)
    if header_method is None:
        raise MCPError(HEADER_MISMATCH, f"Missing {METHOD_HDR} header")
    if header_method != method:
        raise MCPError(
            HEADER_MISMATCH,
            f"{METHOD_HDR} header value {header_method!r} does not match "
            f"body value {method!r}",
        )

    if (name_field := NAME_HDR_METHODS.get(method)) is not None:
        header_name = request.headers.get(NAME_HDR)
        if header_name is None:
            raise MCPError(HEADER_MISMATCH, f"Missing {NAME_HDR} header")
        body_name = params.get(name_field)
        if not isinstance(body_name, str) or decode_header_value(header_name) != body_name:
            raise MCPError(
                HEADER_MISMATCH,
                f"{NAME_HDR} header value does not match body params.{name_field}",
            )


async def _handle_rpc_request(
    server: MCPServer,
    request: web.Request,
    message: JSONRPCRequest,
    request_id: str | int,
    params: dict[str, object],
) -> web.StreamResponse:
    if message["method"] == "initialize":
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning#backward-compatibility-with-initialization-based-versions
        raise MCPError(
            METHOD_NOT_FOUND,
            "The initialize handshake is not supported; supported protocol "
            f"versions: {', '.join(PROTOCOL_VERSIONS)}",
            data={"supported": list(PROTOCOL_VERSIONS)},
        )
    meta = validate_request_meta(params.pop("_meta", None))
    _validate_headers(request, message["method"], params, meta[META_PROTOCOL_VERSION])

    method = server.methods.get(message["method"])
    if method is None:
        raise MCPError(METHOD_NOT_FOUND, f"Unknown method {message['method']!r}")

    ctx = MCPRequest(message["method"], request_id, params, meta, request)
    if method.streaming:
        return await _streaming_request(server, ctx, method.handler)

    # Basic, non-SSE request.
    try:
        result = await method.handler(ctx)
    except MCPError:
        raise
    except Exception:
        logger.exception("Unhandled error in MCP method %r", ctx.method)
        raise MCPError(INTERNAL_ERROR, "Internal error") from None
    envelope: JSONRPCResultResponse = {
        "jsonrpc": "2.0",
        "id": ctx.id,
        "result": _finalize_result(server, result),
    }
    return web.json_response(envelope)


async def _streaming_request(
    server: MCPServer, ctx: MCPRequest, handler: Handler
) -> web.StreamResponse:
    async with sse_response(
        ctx.http,
        ping_interval=server.sse_keepalive,
        allow_all_methods=True,
    ) as resp:
        ctx._resp = resp
        task = asyncio.create_task(handler(ctx))

        try:
            # While task not done.
            while not (await asyncio.wait({task}, timeout=server.sse_keepalive))[0]:
                if not resp.is_connected():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    return resp
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise

        envelope: JSONRPCResultResponse | JSONRPCErrorResponse
        try:
            result = task.result()
        except MCPError as exc:
            envelope = error_envelope(exc, ctx.id)
        except ConnectionError:
            return resp
        except Exception:
            logger.exception("Unhandled error in MCP method %r", ctx.method)
            envelope = error_envelope(MCPError(INTERNAL_ERROR, "Internal error"), ctx.id)
        else:
            envelope = {
                "jsonrpc": "2.0",
                "id": ctx.id,
                "result": _finalize_result(server, result),
            }

        with suppress(ConnectionError):
            await resp.send(json.dumps(envelope, separators=(",", ":")))
        return resp


def _finalize_result(server: MCPServer, result: InputResult | None) -> Result:
    """Fill in the protocol-mandated result fields the handler left implicit."""
    if result is None:
        result = {}
    final: Result = {"resultType": "complete", **result}
    if server.include_server_info:
        meta: ResultMeta = {META_SERVER_INFO: server.server_info.copy()}
        if (handler_meta := final.get("_meta")) is not None:
            meta.update(handler_meta)
        final["_meta"] = meta
    return final
