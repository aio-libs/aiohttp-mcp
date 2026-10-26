"""Tools: functions the language model can discover and invoke."""

import heapq
import inspect
import json
import math
import re
import sys
import time
import types
from types import EllipsisType
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    Generic,
    Literal,
    NotRequired,
    Required,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

if sys.version_info >= (3, 12):
    from typing import TypeAliasType
else:
    from typing_extensions import TypeAliasType

from ._content import Content, Icon
from ._handlers import Doc, Header, _Parameter, _apply_details, _derive_name, _inspect_handler
from ._protocol import (
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolResult,
    ContentBlock,
    ListToolsResult,
    MCPError,
    ToolAnnotations,
    ToolInfo,
    _InputSchema,
    _OutputSchema,
    decode_header_value,
)
from ._transport import MCPRequest, logger

_T = TypeVar("_T")
# TODO(PY312): use the `type` statement.
ToolHandler = TypeAliasType("ToolHandler", Callable[..., Awaitable[object]])

# https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# HTTP field-name token syntax (1*tchar, RFC 9110 section 5.1).
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# The safe integer range of IEEE 754 double precision.
_SAFE_INT = 2**53 - 1


@dataclass(frozen=True, slots=True)
class ToolResult(Generic[_T]):
    """A fully specified tool result.

    Simple tools return a :class:`str`, :class:`.Content`, or (with a data-typed
    return annotation) a plain value delivered as structured content. Return
    a :class:`ToolResult` to combine unstructured :attr:`.content` with
    :attr:`.structured` data explicitly (e.g. to return structured raw values
    to the LLM, while also sending an image of a chart to the user).

    Annotating a handler ``-> ToolResult[Data]`` advertises ``Data`` as the
    output schema and lets the type checker verify :attr:`.structured` payloads.
    """

    content: Sequence[Content] = ()
    structured: _T | None = None
    is_error: bool = False


class ToolError(Exception):
    """A tool execution error, reported to the model for self-correction.

    The message should contain actionable feedback.
    e.g. ``"Invalid departure date: must be in the future."``.
    """


class RateLimiter:
    """Rate limiting of tool invocations.

    Callers are told how long to back off when they exceed the limit, inside
    an error tool result, so a model can self-correct.

    Subclass it to how rate limiting functions: override :meth:`key` to change how
    callers are identified, or :meth:`acquire` to replace the strategy entirely.

    :param limit: Invocations allowed per period.
    :param period: Length of the period in seconds.
    """

    __slots__ = ("_buckets", "_capacity", "_expiries", "_period", "_rate")

    def __init__(self, limit: float = 20.0, period: float = 60.0) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if period <= 0:
            raise ValueError("period must be > 0")
        self._capacity = limit
        self._period = period
        self._rate = limit / period  # Tokens per second
        self._buckets: dict[str, tuple[float, float]] = {}  # (tokens, updated)
        self._expiries: list[tuple[float, str]] = []  # (expiry, bucket_key)

    def key(self, request: MCPRequest) -> str:
        """The identity a request's invocations are counted against.

        The authenticated user when authorization is configured, otherwise
        the peer address (meaningful only when the connection is direct;
        behind a reverse proxy, configure something like
        :mod:`aiohttp_remotes` so :attr:`~aiohttp.web.BaseRequest.remote`
        reflects the client).

        Override to change how callers are grouped.
        """
        if request.auth is not None and request.auth.subject is not None:
            return f"sub:{request.auth.subject}"
        return f"ip:{request.http.remote}"

    async def acquire(self, request: MCPRequest) -> float:
        """Return number of seconds the caller must wait (0 if call allowed).

        Override to change the rate limit algorithm.
        """
        now = time.monotonic()
        while self._expiries and self._expiries[0][0] <= now:
            _, exp_key = heapq.heappop(self._expiries)
            bucket = self._buckets.get(exp_key)
            if bucket is None:
                continue
            expiry = bucket[1] + self._period
            if expiry <= now:
                del self._buckets[exp_key]
            else:
                heapq.heappush(self._expiries, (expiry, exp_key))

        key = self.key(request)
        if (bucket := self._buckets.get(key)) is None:
            tokens = self._capacity
            heapq.heappush(self._expiries, (now + self._period, key))
        else:
            tokens = min(self._capacity, bucket[0] + (now - bucket[1]) * self._rate)
            if tokens < 1.0:
                return (1.0 - tokens) / self._rate
        self._buckets[key] = (tokens - 1.0, now)
        return 0.0


def _schema_error(context: str, hint: object) -> ValueError:
    return ValueError(f"Unsupported type hint {hint!r} for {context}")


def _json_schema(hint: object, context: str) -> dict[str, object]:
    """Derive the JSON Schema fragment for a supported type hint."""
    description = None
    if get_origin(hint) in {Required, NotRequired}:
        hint = get_args(hint)[0]
    if get_origin(hint) is Annotated:
        hint, *metadata = get_args(hint)
        description = next((m.documentation for m in metadata if isinstance(m, Doc)), None)
    schema = _json_schema_bare(hint, context)
    if description is not None:
        schema["description"] = description
    return schema


def _json_type(value: object) -> str:
    """The JSON Schema type name of a Literal member (bool before int)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return "string"


def _json_schema_bare(hint: object, context: str) -> dict[str, object]:
    if hint is Any or hint is object:
        # Any JSON value: the empty schema places no constraints.
        return {}
    origin = get_origin(hint)
    if origin is None:
        # A bare type (no subscription).
        if not isinstance(hint, type):
            raise _schema_error(context, hint)
        if hint is str:
            return {"type": "string"}
        if hint is int:
            return {"type": "integer"}
        if hint is float:
            return {"type": "number"}
        if hint is bool:
            return {"type": "boolean"}
        if hint is types.NoneType:
            return {"type": "null"}
        if is_typeddict(hint):
            properties = {
                k: _json_schema(v, f"{context}.{k}")
                for k, v in get_type_hints(hint, include_extras=True).items()
            }
            schema: dict[str, object] = {"type": "object", "properties": properties}
            # https://github.com/python/typing/issues/1395
            if required := sorted(hint.__required_keys__):  # type: ignore[attr-defined]
                schema["required"] = required
            return schema
        raise _schema_error(context, hint)
    if origin is Literal:
        values = get_args(hint)
        if not all(isinstance(v, (str, int, bool)) or v is None for v in values):
            raise _schema_error(context, hint)
        enum_schema: dict[str, object] = {"enum": tuple(values)}
        kinds = {_json_type(v) for v in values}
        if len(kinds) == 1:
            enum_schema = {"type": next(iter(kinds)), **enum_schema}
        return enum_schema
    if origin is types.UnionType:
        return {"anyOf": [_json_schema(arg, context) for arg in get_args(hint)]}
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": _json_schema(args[0], context)}
        if args and Ellipsis not in args:
            return {
                "type": "array",
                "prefixItems": [
                    _json_schema(arg, f"{context}[{i}]") for i, arg in enumerate(args)
                ],
                "items": False,
                "minItems": len(args),
                "maxItems": len(args),
            }
        raise _schema_error(context, hint)
    if isinstance(origin, type):
        if issubclass(origin, Sequence):
            args = get_args(hint)
            if len(args) != 1:
                raise _schema_error(context, hint)
            return {"type": "array", "items": _json_schema(args[0], context)}
        if issubclass(origin, Mapping):
            args = get_args(hint)
            if len(args) != 2 or args[0] is not str:
                raise _schema_error(context, hint)
            return {"type": "object", "additionalProperties": _json_schema(args[1], context)}
    raise _schema_error(context, hint)


def _validate_json(hint: object, value: object, context: str) -> None:
    """Check ``value`` against the schema grammar; raises ValueError."""
    if get_origin(hint) in {Required, NotRequired}:
        hint = get_args(hint)[0]
    if get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    if hint is Any or hint is object:
        pass  # Any JSON value.
    elif hint is str:
        if not isinstance(value, str):
            raise ValueError(f"{context}: expected a string")
    elif hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{context}: expected an integer")
    elif hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{context}: expected a number")
    elif hint is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{context}: expected a boolean")
    elif hint is types.NoneType or hint is None:
        if value is not None:
            raise ValueError(f"{context}: expected null")
    elif is_typeddict(hint):
        assert isinstance(hint, type)
        if not isinstance(value, Mapping):
            raise ValueError(f"{context}: expected an object")
        hints = get_type_hints(hint, include_extras=True)
        # https://github.com/python/typing/issues/1395
        if missing := hint.__required_keys__ - value.keys():  # type: ignore[attr-defined]
            raise ValueError(f"{context}: missing {', '.join(sorted(missing))}")
        for key, key_hint in hints.items():
            if key in value:
                _validate_json(key_hint, value[key], f"{context}.{key}")
    elif (origin := get_origin(hint)) is Literal:
        # Type-aware membership: 1 == True in Python, but not in JSON Schema.
        if not any(
            type(value) is type(option) and value == option
            for option in get_args(hint)
        ):
            raise ValueError(f"{context}: not one of the allowed values")
    elif origin is types.UnionType:
        for arg in get_args(hint):
            try:
                _validate_json(arg, value, context)
            except ValueError:
                continue
            return
        raise ValueError(f"{context}: does not match any allowed type")
    elif origin is tuple and not (
        len(tuple_args := get_args(hint)) == 2 and tuple_args[1] is Ellipsis
    ):
        # A fixed-shape tuple (prefixItems).
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"{context}: expected an array")
        if len(value) != len(tuple_args):
            raise ValueError(f"{context}: expected {len(tuple_args)} items")
        for index, (item_hint, item) in enumerate(zip(tuple_args, value)):
            _validate_json(item_hint, item, f"{context}[{index}]")
    elif origin is tuple or (isinstance(origin, type) and issubclass(origin, Sequence)):
        # Handlers may legitimately return tuples for array-typed results.
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"{context}: expected an array")
        item_hint = get_args(hint)[0]
        for index, item in enumerate(value):
            _validate_json(item_hint, item, f"{context}[{index}]")
    elif isinstance(origin, type) and issubclass(origin, Mapping):
        if not isinstance(value, Mapping):
            raise ValueError(f"{context}: expected an object")
        value_hint = get_args(hint)[1]
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{context}: object keys must be strings")
            _validate_json(value_hint, item, f"{context}.{key}")
    else:
        raise ValueError(f"{context}: unsupported type")


@dataclass(frozen=True, slots=True)
class Tool:
    """An MCP tool.

    Plain handler functions can be passed to :class:`ToolRegistry` directly;
    wrap one in a :class:`.Tool` to override the derived metadata, declare hints,
    or restrict it to callers holding specific scopes.

    :param handler: The tool function.
    :param name: Tool identifier; the function name when omitted. Must be 1-128
        characters from ``A-Z``, ``a-z``, ``0-9``, underscore, hyphen, and dot.
    :param title: Human-readable display name; the docstring's summary line
        when omitted (:pep:`257`).
    :param description: Description for the model; the docstring's body
        (everything after the summary line) when omitted.
    :param icons: Optional icons for display in user interfaces.
    :param read_only: Hint that the tool does not modify its environment.
    :param destructive: Hint that the tool may perform destructive actions.
    :param idempotent: Hint that repeated calls with the same arguments have
        no additional effect.
    :param open_world: Hint that the tool interacts with external entities.
    :param scopes: Scopes required to use this tool.
    """

    handler: ToolHandler
    name: str | None = None
    title: str | None = None
    description: str | None = None
    icons: Sequence[Icon] | None = None
    # The hint defaults mirror the spec's.
    read_only: bool = field(default=False, kw_only=True)
    destructive: bool = field(default=True, kw_only=True)
    idempotent: bool = field(default=False, kw_only=True)
    open_world: bool = field(default=True, kw_only=True)
    scopes: Collection[str] = ()

    def _annotations(self) -> ToolAnnotations:
        annotations: ToolAnnotations = {}
        if self.read_only:
            annotations["readOnlyHint"] = True
        if not self.destructive:
            annotations["destructiveHint"] = False
        if self.idempotent:
            annotations["idempotentHint"] = True
        if not self.open_world:
            annotations["openWorldHint"] = False
        return annotations


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    tool: Tool
    info: ToolInfo
    wants_request: bool
    arguments: Mapping[str, object]
    required: frozenset[str]
    structured: object | None
    # Parameter name to ``Mcp-Param-*`` header name (``x-mcp-header``).
    headers: Mapping[str, str]
    scopes: frozenset[str]


def _is_content_hint(hint: object) -> bool:
    """Whether a return hint describes unstructured content blocks."""
    origin = get_origin(hint)
    if origin is Annotated:
        hint = get_args(hint)[0]
    if isinstance(hint, type) and issubclass(hint, Content):
        return True
    if isinstance(origin, type) and issubclass(origin, (types.UnionType, Sequence)):
        return any(_is_content_hint(arg) for arg in get_args(hint))
    return False


def _build_spec(tool: Tool) -> _ToolSpec:
    name = _derive_name(tool.handler, tool.name, "tool")
    if not _TOOL_NAME.fullmatch(name):
        # https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names
        raise ValueError(
            f"invalid tool name {name!r}: must be 1-128 characters from"
            " A-Z, a-z, 0-9, underscore, hyphen, and dot"
        )
    wants_request, parameters = _inspect_handler(
        tool.handler, name=name, kind="tool", typed=True
    )
    hints = get_type_hints(tool.handler, include_extras=True)

    properties: dict[str, object] = {}
    arguments: dict[str, object] = {}
    required = set()
    headers: dict[str, str] = {}
    signature_defaults = _defaults(tool.handler)
    for parameter in parameters:
        hint = Any if parameter.hint is inspect.Parameter.empty else parameter.hint
        schema = _json_schema(hint, f"{name}.{parameter.name}")
        if parameter.documentation is not None:
            schema["description"] = parameter.documentation
        if parameter.header is not None:
            _check_header_name(name, parameter, hint, headers)
            headers[parameter.name] = parameter.header
            schema["x-mcp-header"] = parameter.header
        if not parameter.required:
            default = signature_defaults.get(parameter.name)
            try:
                json.dumps(default)
            except (TypeError, ValueError):
                pass  # Not representable; omit from the schema.
            else:
                schema["default"] = default
        properties[parameter.name] = schema
        arguments[parameter.name] = hint
        if parameter.required:
            required.add(parameter.name)
    # https://github.com/python/mypy/issues/18176
    input_schema: _InputSchema = {"type": "object", "additionalProperties": False}  # type: ignore[typeddict-unknown-key]
    if properties:
        input_schema["properties"] = properties  # type: ignore[typeddict-unknown-key]
    if required:
        input_schema["required"] = sorted(required)  # type: ignore[typeddict-unknown-key]

    return_hint = hints.get("return", inspect.Parameter.empty)
    if get_origin(return_hint) is Annotated:
        return_hint = get_args(return_hint)[0]
    if get_origin(return_hint) is ToolResult:
        (return_hint,) = get_args(return_hint)
    if get_origin(return_hint) is types.UnionType:
        union_args = get_args(return_hint)
        # `-> Data | ToolResult` keeps the derived output schema while
        # allowing custom content alongside the structured data, and
        # `-> Data | None` means the tool may produce no output - consistent
        # with `-> None` - rather than a null output schema.
        remaining = []
        for arg in union_args:
            if arg is ToolResult or arg is types.NoneType:
                continue
            if get_origin(arg) is ToolResult:
                arg = get_args(arg)[0]
                if arg is Any or arg is object:
                    continue
            remaining.append(arg)
        if remaining:
            return_hint = Union[tuple(remaining)]  # noqa: UP007
        else:
            return_hint = ToolResult if ToolResult in union_args else types.NoneType
    structured: object | None = None
    if return_hint not in {
        str, inspect.Parameter.empty, types.NoneType, ToolResult, Any, object
    } and not _is_content_hint(return_hint):
        structured = return_hint

    info: ToolInfo = {"name": name, "inputSchema": input_schema}
    _apply_details(info, tool)
    if structured is not None:
        # https://github.com/python/mypy/issues/18176
        info["outputSchema"] = _json_schema(structured, f"{name} return value")  # type: ignore[typeddict-item]
    if annotations := tool._annotations():
        info["annotations"] = annotations
    return _ToolSpec(
        tool=tool,
        info=info,
        wants_request=wants_request,
        arguments=arguments,
        required=frozenset(required),
        structured=structured,
        headers=headers,
        scopes=frozenset(tool.scopes),
    )


def _check_header_name(
    tool_name: str, parameter: _Parameter, hint: object, headers: Mapping[str, str]
) -> None:
    """Enforce the registration constraints of ``x-mcp-header``.

    https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header
    """
    context = f"{tool_name}.{parameter.name}"
    assert parameter.header is not None
    if not _HEADER_NAME.fullmatch(parameter.header):
        raise ValueError(f"invalid x-mcp-header name {parameter.header!r} for {context}:")
    if parameter.header.lower() in {header.lower() for header in headers.values()}:
        raise ValueError(f"duplicate x-mcp-header name {parameter.header!r} for {context}")
    if get_origin(hint) is types.UnionType:
        remaining = [a for a in get_args(hint) if a is not types.NoneType]
        hint = remaining[0] if len(remaining) == 1 else hint
    if hint not in {str, int, bool}:
        raise ValueError(f"x-mcp-header on {context} requires a str, int, or bool parameter")


def _defaults(handler: Callable[..., object]) -> dict[str, object]:
    return {
        p.name: p.default for p in inspect.signature(handler).parameters.values()
        if p.default is not p.empty
    }


def _enforce_param_headers(
    ctx: MCPRequest, spec: _ToolSpec, arguments: Mapping[str, object]
) -> None:
    """Cross-validate ``Mcp-Param-*`` headers against the call arguments."""
    for parameter, header in spec.headers.items():
        header_name = f"Mcp-Param-{header}"
        values = ctx.http.headers.getall(header_name, [])
        value = arguments.get(parameter)
        if value is None:
            # A null or absent parameter must not have its header sent.
            if values:
                raise MCPError(
                    HEADER_MISMATCH,
                    f"Unexpected {header_name} header for an absent parameter",
                )
            continue
        if len(values) != 1:
            raise MCPError(HEADER_MISMATCH, f"Missing or repeated {header_name} header")
        decoded = decode_header_value(values[0])
        if isinstance(value, bool):
            matches = decoded == ("true" if value else "false")
        elif isinstance(value, int):
            # Compared numerically: 42.0 and 42 are considered equal.
            try:
                matches = abs(value) <= _SAFE_INT and float(decoded) == value
            except ValueError:
                matches = False
        else:
            matches = isinstance(value, str) and decoded == value
        if not matches:
            raise MCPError(
                HEADER_MISMATCH,
                f"{header_name} header value does not match the body value",
            )


def _error_result(message: str) -> CallToolResult:
    # https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling
    return {"content": [{"type": "text", "text": message}], "isError": True}


class ToolRegistry:
    """A collection of tools for an MCP server.

    Tools are listed in registration order; keep it stable so clients can
    cache the list.

    :param tools: The tools to serve: plain ``async`` functions, or :class:`Tool`
        descriptors wrapping them.
    :param ttl: Seconds a client is recommended to cache the tool list for.
    :param cache_scope: Whether shared intermediaries may cache the list
        (``"public"``) or only the requesting client (``"private"``, the safe
        default: with authorization configured the list may vary by the
        caller's scopes).
    :param rate_limiter: Limits the tool invocations of each caller, counted
        across all this registry's tools; see :class:`RateLimiter`, which can
        also be a subclass for custom keying or strategies. Exceeding the
        limit produces an error tool result telling the model how long to
        back off. Pass :data:`None` to disable rate limiting (e.g. if rate
        limiting in a proxy).
    """

    def __init__(
        self,
        *tools: Tool | ToolHandler,
        ttl: float = 300.0,
        cache_scope: Literal["public", "private"] = "private",
        # TODO(PY315): Use sentinel()
        rate_limiter: RateLimiter | None | EllipsisType = ...,
    ) -> None:
        if ttl < 0:
            raise ValueError("ttl must be >= 0")
        if cache_scope not in {"public", "private"}:
            raise ValueError("cache_scope must be 'public' or 'private'")
        self._ttl_ms = round(ttl * 1000)
        self._cache_scope: Literal["public", "private"] = cache_scope
        self._limiter = RateLimiter() if rate_limiter is Ellipsis else rate_limiter
        self._specs: dict[str, _ToolSpec] = {}
        for tool in tools:
            spec = _build_spec(tool if isinstance(tool, Tool) else Tool(tool))
            if spec.info["name"] in self._specs:
                raise ValueError(f"duplicate tool name {spec.info['name']!r}")
            self._specs[spec.info["name"]] = spec

    @property
    def _scoped(self) -> bool:
        return any(spec.scopes for spec in self._specs.values())

    async def _get_scopes(self, ctx: MCPRequest) -> Collection[str]:
        """Scope requirements of a ``tools/call`` (``Method.scopes``)."""
        name = ctx.params.get("name")
        spec = self._specs.get(name) if isinstance(name, str) else None
        return spec.scopes if spec is not None else ()

    async def _list(self, ctx: MCPRequest) -> ListToolsResult:
        """tools/list endpoint."""
        if "cursor" in ctx.params:
            raise MCPError(INVALID_PARAMS, "Invalid cursor")
        return {
            "tools": [
                s.info for s in self._specs.values()
                if not s.scopes or (ctx.auth is not None and s.scopes <= ctx.auth.scopes)
            ],
            "ttlMs": self._ttl_ms,
            "cacheScope": self._cache_scope,
        }

    async def _call(self, ctx: MCPRequest) -> CallToolResult:
        """tools/call endpoint."""
        name = ctx.params.get("name")
        spec = self._specs.get(name) if isinstance(name, str) else None
        if spec is None:
            # https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling
            raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = ctx.params.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise MCPError(INVALID_PARAMS, "arguments must be an object")

        # https://modelcontextprotocol.io/specification/2026-07-28/server/tools#security-considerations
        if self._limiter is not None and (wait := await self._limiter.acquire(ctx)):
            return _error_result(f"Rate limit exceeded. Retry in {math.ceil(wait)} seconds.")

        # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-behavior-for-custom-headers
        _enforce_param_headers(ctx, spec, arguments)
        # Value-level problems are execution errors reported in the result,
        # giving the model actionable feedback to self-correct with.
        if unknown := arguments.keys() - spec.arguments.keys():
            return _error_result(f"Unexpected arguments: {', '.join(sorted(unknown))}")
        if missing := spec.required - arguments.keys():
            return _error_result(f"Missing required arguments: {', '.join(sorted(missing))}")
        for key, value in arguments.items():
            try:
                _validate_json(spec.arguments[key], value, key)
            except ValueError as exc:
                return _error_result(f"Invalid arguments: {exc}")

        try:
            if spec.wants_request:
                result = await spec.tool.handler(ctx, **arguments)
            else:
                result = await spec.tool.handler(**arguments)
        except ToolError as exc:
            return _error_result(str(exc))

        return self._finish(spec, result)

    def _finish(self, spec: _ToolSpec, result: object) -> CallToolResult:
        if spec.structured is not None:
            if result is None:
                return {"content": []}
            if isinstance(result, ToolResult):
                if result.is_error:
                    return {
                        "content": [c._to_wire() for c in result.content], "isError": True
                    }
                structured_value = result.structured
            else:
                structured_value = result

            content = []
            if isinstance(result, ToolResult) and result.content:
                content = [c._to_wire() for c in result.content]
            return {"content": content, "structuredContent": structured_value}

        if isinstance(result, ToolResult):
            final: CallToolResult = {"content": [c._to_wire() for c in result.content]}
            if result.structured is not None:
                final["structuredContent"] = result.structured
            if result.is_error:
                final["isError"] = True
            return final

        if result is None:
            return {"content": []}
        if isinstance(result, str):
            return {"content": [{"type": "text", "text": result}]}
        if isinstance(result, Content):
            return {"content": [result._to_wire()]}
        if isinstance(result, Sequence) and all(isinstance(c, Content) for c in result):
            return {"content": [c._to_wire() for c in result]}
        logger.error(
            "Tool %r returned an unsupported value of type %s",
            spec.info["name"],
            type(result).__name__,
        )
        raise MCPError(INTERNAL_ERROR, "Internal error")
