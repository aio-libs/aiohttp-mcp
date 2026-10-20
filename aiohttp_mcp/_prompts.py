"""Prompt templates."""

import inspect
import sys
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, get_args, get_origin, get_type_hints

if sys.version_info >= (3, 12):
    from typing import TypeAliasType
else:
    from typing_extensions import TypeAliasType

from ._content import Content, Icon, Message, TextContent
from ._protocol import (
    INVALID_PARAMS,
    GetPromptResult,
    ListPromptsResult,
    MCPError,
    PromptArgument,
    PromptInfo,
    PromptMessage,
    ServerCapabilities,
)
from ._transport import MCPRequest

# TODO(PY312): use the `type` statement.
PromptResult = TypeAliasType("PromptResult", str | Sequence[Content | Message])
PromptHandler = TypeAliasType("PromptHandler", Callable[..., Awaitable[PromptResult]])


@dataclass(frozen=True, slots=True)
class Doc:
    """Documents a prompt argument.

    Used inside :data:`~typing.Annotated` metadata::

        code: Annotated[str, Doc("The code to review")]
    """

    documentation: str


@dataclass(frozen=True, slots=True)
class Prompt:
    """A prompt template, overriding details derived from ``handler``.

    Plain handler functions can be passed to :func:`aiohttp_mcp.setup`
    directly; wrap one in a ``Prompt`` to override the derived metadata or to
    restrict it to callers holding specific scopes.

    :param handler: The prompt function.
    :param name: Prompt identifier (often used as the slash command); the
        function name when omitted.
    :param title: Human-readable display name; the docstring's summary line
        when omitted (:pep:`257`).
    :param description: Human-readable description; the docstring's body
        (everything after the summary line) when omitted.
    :param icons: Optional icons for display in user interfaces.
    :param scopes: Scopes required to use this prompt.
    """

    handler: PromptHandler
    name: str | None = None
    title: str | None = None
    description: str | None = None
    icons: Sequence[Icon] | None = None
    scopes: Collection[str] = ()


@dataclass(frozen=True, slots=True)
class _PromptSpec:
    info: PromptInfo
    handler: PromptHandler
    wants_request: bool
    required: frozenset[str]
    known: frozenset[str]
    scopes: frozenset[str]


def _build_spec(prompt: Prompt) -> _PromptSpec:
    fn = prompt.handler
    name = prompt.name or getattr(fn, "__name__", "")
    if not name:
        raise ValueError(f"cannot derive a name for prompt handler {fn!r}")
    hints = get_type_hints(fn, include_extras=True)
    wants_request = False
    arguments: list[PromptArgument] = []
    required = set()
    known = set()
    for index, parameter in enumerate(inspect.signature(fn).parameters.values()):
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ValueError(f"prompt {name!r} must not use *args/**kwargs")
        hint = hints.get(parameter.name)
        description = None
        if get_origin(hint) is Annotated:
            hint, *metadata = get_args(hint)
            description = next((m.documentation for m in metadata if isinstance(m, Doc)), None)
        if index == 0 and hint is MCPRequest:
            wants_request = True
            continue
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise ValueError(
                f"prompt argument {parameter.name!r} of {name!r} must be"
                " addressable by keyword"
            )
        if hint not in {None, str}:
            raise ValueError(f"prompt argument {parameter.name!r} of {name!r} must be a str")
        argument: PromptArgument = {"name": parameter.name}
        if description is not None:
            argument["description"] = description
        if parameter.default is parameter.empty:
            argument["required"] = True
            required.add(parameter.name)
        known.add(parameter.name)
        arguments.append(argument)

    summary, _, body = (inspect.getdoc(fn) or "").partition("\n")
    info: PromptInfo = {"name": name}
    if title := prompt.title or summary.strip():
        info["title"] = title
    if description := prompt.description or body.strip():
        info["description"] = description
    if arguments:
        info["arguments"] = arguments
    if prompt.icons is not None:
        info["icons"] = tuple(icon._to_wire() for icon in prompt.icons)
    return _PromptSpec(
        info=info,
        handler=fn,
        wants_request=wants_request,
        required=frozenset(required),
        known=frozenset(known),
        scopes=frozenset(prompt.scopes),
    )


class PromptRegistry:
    """A collection of prompts for an MCP server.

    :param prompts: The prompts: plain ``async`` functions, or :class:`Prompt`
        descriptors wrapping them to override derived metadata.
    :param ttl: Seconds a client is recommended to cache the list of prompts for.
    :param cache_scope: Whether shared intermediaries may cache the list of
        prompts (``"public"``) or only the requesting client
        (``"private"``, the safe default: with authorization configured the
        list may vary by the caller's scopes).
    """

    def __init__(
        self,
        *prompts: Prompt | PromptHandler,
        ttl: float = 600.0,
        cache_scope: Literal["public", "private"] = "private",
    ) -> None:
        if ttl < 0:
            raise ValueError("ttl must be >= 0")
        if cache_scope not in {"public", "private"}:
            raise ValueError("cache_scope must be 'public' or 'private'")
        self._ttl_ms = round(ttl * 1000)
        self._cache_scope: Literal["public", "private"] = cache_scope
        self._specs: dict[str, _PromptSpec] = {}
        for prompt in prompts:
            spec = _build_spec(prompt if isinstance(prompt, Prompt) else Prompt(prompt))
            if spec.info["name"] in self._specs:
                raise ValueError(f"duplicate prompt name {spec.info['name']!r}")
            self._specs[spec.info["name"]] = spec

    @property
    def _scoped(self) -> bool:
        """Whether any prompt is scope-restricted (then authorization is required)."""
        return any(spec.scopes for spec in self._specs.values())

    async def _get_scopes(self, ctx: MCPRequest) -> Collection[str]:
        """Scope requirements of a ``prompts/get`` call (``Method.scopes``)."""
        name = ctx.params.get("name")
        spec = self._specs.get(name) if isinstance(name, str) else None
        return () if spec is None else spec.scopes

    def _visible(self, ctx: MCPRequest) -> Sequence[_PromptSpec]:
        """The prompt set for this caller's authorization.

        The list MAY vary by the authorization presented on the request.
        """
        auth = ctx.auth
        return [
            spec
            for spec in self._specs.values()
            if not spec.scopes or (auth is not None and spec.scopes <= auth.scopes)
        ]

    async def _list(self, ctx: MCPRequest) -> ListPromptsResult:
        """prompts/list endpoint."""
        if "cursor" in ctx.params:
            # (https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination#error-handling).
            raise MCPError(INVALID_PARAMS, "Invalid cursor")
        return {
            "prompts": [spec.info for spec in self._visible(ctx)],
            "ttlMs": self._ttl_ms,
            "cacheScope": self._cache_scope,
        }

    async def _get(self, ctx: MCPRequest) -> GetPromptResult:
        """prompts/get endpoint."""
        name = ctx.params.get("name")
        spec = self._specs.get(name) if isinstance(name, str) else None
        if spec is None:
            raise MCPError(INVALID_PARAMS, f"Unknown prompt {name!r}")
        arguments = ctx.params.get("arguments", {})
        if not isinstance(arguments, Mapping) or not all(
            isinstance(value, str) for value in arguments.values()
        ):
            raise MCPError(INVALID_PARAMS, "arguments must be an object of strings")
        if missing := spec.required - arguments.keys():
            raise MCPError(
                INVALID_PARAMS,
                f"Missing required arguments: {', '.join(sorted(missing))}",
            )
        kwargs = {k: v for k, v in arguments.items() if k in spec.known}
        if spec.wants_request:
            result = await spec.handler(ctx, **kwargs)
        else:
            result = await spec.handler(**kwargs)

        if isinstance(result, str):
            result = (TextContent(result),)
        messages = tuple((r if isinstance(r, Message) else Message(r))._to_wire() for r in result)
        final: GetPromptResult = {"messages": messages}
        if (description := spec.info.get("description")) is not None:
            final["description"] = description
        return final
