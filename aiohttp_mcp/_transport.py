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
from typing import TYPE_CHECKING, TypeAlias, cast

from aiohttp import web
from aiohttp_sse import EventSourceResponse, sse_response
from pydantic_core import from_json

from ._auth import TOKEN_INFO_KEY, TokenInfo, _AuthError
from ._content import EmbeddedResource
from ._elicitation import (
    Elicitation,
    ElicitResult,
    InputRequired,
    SecureFormElicitation,
    _input_required_result,
    _load_request_state,
    _parse_result,
    _secure_form_result,
)
from ._protocol import (
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    METHOD_HDR,
    METHOD_NOT_FOUND,
    NAME_HDR,
    NAME_HDR_METHODS,
    PARSE_ERROR,
    PROTOCOL_VERSION_HDR,
    PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    InputResult,
    JSONRPCErrorResponse,
    JSONRPCNotification,
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

    __slots__ = {
        "_body": None,
        "_form_tickets": None,
        "_input_responses": None,
        "_request_state": None,
        "_resp": None,
        "_responses": None,
        "_server": None,
        "http": "The underlying :class:`aiohttp.web.Request`.",
        "id": "The JSON-RPC request ID.",
        "meta": "The validated ``_meta`` object of the request.",
        "method": 'The JSON-RPC method name (e.g. ``"prompts/get"``).',
        "params": "The request parameters, with protocol fields removed.",
        "state": (
            "Verified application state from a previous ``input_required``"
            " result of this request, as passed to :class:`InputRequired`;"
            " :data:`None` on the first attempt."
        ),
    }

    def __init__(
        self,
        method: str,
        request_id: RequestId,
        params: Mapping[str, object],
        meta: RequestMeta,
        http: web.Request,
        *,
        server: "MCPServer",
        body: bytes,
        input_responses: Mapping[str, object] | None = None,
        request_state: str | None = None,
    ) -> None:
        self.method = method
        self.id = request_id
        self.params = params
        self.meta = meta
        self.http = http
        self.state: object = None
        self._server = server
        # The raw request body, so that typed validation (tool arguments)
        # can read the wire JSON itself rather than a reserialisation.
        self._body = body
        self._input_responses = input_responses
        self._request_state = request_state
        self._responses: Mapping[str, object] = input_responses or {}
        self._form_tickets: Mapping[str, str] = {}
        self._resp: EventSourceResponse | None = None

    @property
    def auth(self) -> TokenInfo | None:
        """Validated access-token information, if authorization is enabled."""
        return self.http.get(TOKEN_INFO_KEY)

    async def elicit(
        self,
        key: str,
        elicitation: Elicitation,
        *,
        state: Mapping[str, object] | None = None,
    ) -> ElicitResult:
        """Return the user's response to ``elicitation``, requesting it if absent.

        When the response identified by ``key`` was provided, it is validated and
        returned. Otherwise :class:`InputRequired` is raised; the client will then
        request the input from the user and retry. Consecutive ``elicit`` calls
        therefore accumulate one round trip at a time, with earlier responses
        carried in the signed request state.

        :param key: Identifies this elicitation within the request.
        :param state: Application state echoed by the client and available
            as :attr:`state` on the retry; see :class:`InputRequired`.
        """
        response = self._responses.get(key)
        if response is None:
            raise InputRequired({key: elicitation}, state=state)
        if isinstance(elicitation, SecureFormElicitation):
            return await _secure_form_result(self, key, elicitation, response, state)
        return _parse_result(key, response, elicitation)

    async def embed_resource(self, uri: str) -> list[EmbeddedResource]:
        """Embed the contents of a registered resource into a result.

        Reads the resource (or resource template) matching ``uri`` from the
        registry passed to :func:`.setup` and returns its
        contents as :class:`.EmbeddedResource` blocks.

        Raises :exc:`LookupError` if no resources are configured or nothing
        matches ``uri``.
        """
        registry = self._server._resources
        if registry is None:
            raise LookupError("no resources are configured")
        return await registry._embed(self, uri)

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
    input_required: bool = False


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


async def handle_request(server: "MCPServer", request: web.Request) -> web.StreamResponse:
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


async def _serve_request(server: "MCPServer", request: web.Request) -> web.StreamResponse:
    request_id: RequestId | None = None
    ctx: MCPRequest | None = None
    method: Method | None = None
    scopes: Collection[str] = ()
    scopes_gen: AsyncGenerator[Collection[str], None] | None = None
    error: MCPError | None = None
    try:
        try:
            body = await request.read()
            message = from_json(body)
        except ValueError:
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

        input_responses = params.pop("inputResponses", None)
        request_state = params.pop("requestState", None)
        if input_responses is not None and not isinstance(input_responses, dict):
            raise MCPError(INVALID_PARAMS, "inputResponses must be an object")
        if request_state is not None and not isinstance(request_state, str):
            raise MCPError(INVALID_PARAMS, "requestState must be a string")
        ctx = MCPRequest(
            message["method"],
            request_id,
            params,
            meta,
            request,
            server=server,
            body=body,
            input_responses=input_responses,
            request_state=request_state,
        )
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
        if method.input_required:
            # Needs the authenticated principal, so cannot happen earlier.
            _load_request_state(server._state_signer, ctx)
        if method.streaming:
            response = await _streaming_request(server, ctx, method)
        else:
            response = await _basic_request(server, ctx, method)
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
    server: "MCPServer", ctx: MCPRequest, method: Method
) -> web.StreamResponse:
    """A plain request/response method call (no SSE)."""
    try:
        result = await method.handler(ctx)
    except InputRequired as exc:
        result = _input_required_result(ctx, method.input_required, exc)
    envelope: JSONRPCResultResponse = {
        "jsonrpc": "2.0",
        "id": ctx.id,
        "result": _finalize_result(server, result),
    }
    return web.json_response(envelope)


async def _streaming_request(
    server: "MCPServer", ctx: MCPRequest, method: Method
) -> web.StreamResponse:
    async with sse_response(
        ctx.http,
        ping_interval=server._sse_keepalive,
        allow_all_methods=True,
    ) as resp:
        ctx._resp = resp
        task = asyncio.create_task(method.handler(ctx))

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
        except InputRequired as mrtr:
            try:
                result = _input_required_result(ctx, method.input_required, mrtr)
            except MCPError as exc:
                envelope = error_envelope(exc, ctx.id)
            else:
                envelope = {
                    "jsonrpc": "2.0",
                    "id": ctx.id,
                    "result": _finalize_result(server, result),
                }
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


def _finalize_result(server: "MCPServer", result: InputResult | None) -> Result:
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
