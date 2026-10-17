"""Streamable HTTP transport binding."""

import asyncio
import inspect
import json
import logging
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Mapping,
    Sequence,
)
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, TypedDict, cast

from aiohttp import web
from aiohttp_sse import EventSourceResponse, sse_response

from ._auth import TOKEN_INFO_KEY, TokenInfo, _AuthError
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

    @property
    def auth(self) -> TokenInfo | None:
        """Validated access-token information, if authorization is enabled."""
        return self.http.get(TOKEN_INFO_KEY)

    async def _emit(self, message: JSONRPCNotification) -> None:
        if self._resp is None:
            raise RuntimeError("Method is not registered as streaming")
        await self._resp.send(json.dumps(message, separators=(",", ":")))


Handler: TypeAlias = Callable[[MCPRequest], Coroutine[None, None, InputResult | None]]
Scopes: TypeAlias = (
    Sequence[str]
    | Callable[[MCPRequest], Awaitable[Collection[str]]]
    | Callable[[MCPRequest], AsyncGenerator[Collection[str], None]]
)


@dataclass(frozen=True, slots=True)
class Method:
    """A registered RPC method."""

    handler: Handler
    streaming: bool = False
    scopes: Scopes = ()


async def _resolve_scopes(
    method: Method, ctx: MCPRequest
) -> tuple[Collection[str], AsyncGenerator[Collection[str], None] | None]:
    """Resolve a method's scope requirements for one call.

    For the async-generator form the started generator is returned alongside
    the scopes; the caller owns closing or resuming it.
    """
    scopes = method.scopes
    if isinstance(scopes, Sequence):
        return scopes, None
    if inspect.isasyncgenfunction(scopes):
        gen = scopes(ctx)
        try:
            return await anext(gen), gen
        except StopAsyncIteration:
            raise RuntimeError(
                "scopes generator finished without yielding a scope set"
            ) from None
    fn = cast("Callable[[MCPRequest], Awaitable[Collection[str]]]", scopes)
    return await fn(ctx), None


async def _unwind_scopes(
    gen: "AsyncGenerator[Collection[str], None]", exc: BaseException
) -> None:
    """Push the handler's exception into the scopes generator.

    Ensures that ``async with``/``finally`` blocks around its ``yield`` roll
    back, if needed. The original exception is never suppressed
    (the caller re-raises it).
    """
    try:
        await gen.athrow(exc)
    except BaseException as cleanup_exc:
        if cleanup_exc is not exc and not isinstance(cleanup_exc, StopAsyncIteration):
            logger.exception("Scope resource cleanup failed")
    else:
        logger.error("Scopes generator yielded more than once")
        await gen.aclose()


async def _finish_scopes(gen: "AsyncGenerator[Collection[str], None]") -> None:
    """Resume the scopes generator past its ``yield`` after a successful call."""
    try:
        await anext(gen)
    except StopAsyncIteration:
        return
    await gen.aclose()
    raise RuntimeError("scopes generator yielded more than once")


async def handle_request(server: MCPServer, request: web.Request) -> web.StreamResponse:
    origin = request.headers.get("Origin")
    if origin is not None and not server._origin_allowed(origin):
        origin_error = MCPError(INVALID_REQUEST, "Origin not allowed")
        return web.json_response(error_envelope(origin_error), status=403)

    try:
        return await _serve_request(server, request)
    except _AuthError as exc:
        return exc.response
    except MCPError as exc:
        return web.json_response(error_envelope(exc, exc.request_id), status=exc.http_status)
    except Exception:
        logger.exception("Unhandled error serving MCP request")
        error = MCPError(INTERNAL_ERROR, "Internal error")
        return web.json_response(error_envelope(error), status=error.http_status)


async def _serve_request(server: MCPServer, request: web.Request) -> web.StreamResponse:
    request_id: RequestId | None = None
    ctx: MCPRequest | None = None
    method: Method | None = None
    scopes: Collection[str] = ()
    scopes_gen: AsyncGenerator[Collection[str], None] | None = None
    error: MCPError | None = None
    try:
        try:
            message = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MCPError(PARSE_ERROR, "Parse error") from None
        request_id, params = _validate_message(message)
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
        method = server._methods.get(message["method"])
        if method is None:
            raise MCPError(METHOD_NOT_FOUND, f"Unknown method {message['method']!r}")
        ctx = MCPRequest(message["method"], request_id, params, meta, request)
        try:
            scopes, scopes_gen = await _resolve_scopes(method, ctx)
        except Exception:
            # Not an error backstop: converting to a deferred MCPError keeps
            # the 401-first behaviour for unauthenticated callers whose input
            # crashed the scopes callable.
            logger.exception("Scope resolution failed for MCP method %r", ctx.method)
            raise MCPError(INTERNAL_ERROR, "Internal error") from None
    except MCPError as exc:
        error = exc

    if server._authorization is not None:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#access-token-usage
        try:
            info = await server._authorization._authenticate(request, scopes or None)
        except BaseException:
            if scopes_gen is not None:
                await scopes_gen.aclose()
            raise
        request[TOKEN_INFO_KEY] = info

    if error is not None:
        # Raise any deferred exceptions after authentication succeeded.
        assert scopes_gen is None
        error.request_id = request_id
        raise error
    assert ctx is not None and method is not None  # Errors were handled above

    try:
        if method.streaming:
            response = await _streaming_request(server, ctx, method.handler)
        else:
            response = await _basic_request(server, ctx, method.handler)
    except BaseException as exc:
        if scopes_gen is not None:
            await _unwind_scopes(scopes_gen, exc)
        if isinstance(exc, MCPError) and exc.request_id is None:
            exc.request_id = request_id
        raise
    if scopes_gen is not None:
        if method.streaming:
            # The response is already on the wire; nothing can be returned.
            try:
                await _finish_scopes(scopes_gen)
            except Exception:
                logger.exception("Scope resource cleanup failed")
        else:
            await _finish_scopes(scopes_gen)
    return response


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


async def _basic_request(
    server: MCPServer, ctx: MCPRequest, handler: Handler
) -> web.StreamResponse:
    """A plain request/response method call (no SSE)."""
    result = await handler(ctx)
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
        ping_interval=server._sse_keepalive,
        allow_all_methods=True,
    ) as resp:
        ctx._resp = resp
        task = asyncio.create_task(handler(ctx))

        try:
            # While task not done.
            while not (await asyncio.wait({task}, timeout=server._sse_keepalive))[0]:
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
    if server._include_server_info:
        meta: ResultMeta = {META_SERVER_INFO: server._server_info.copy()}
        if (handler_meta := final.get("_meta")) is not None:
            meta.update(handler_meta)
        final["_meta"] = meta
    return final
