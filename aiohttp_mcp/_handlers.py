"""Shared introspection of handler functions."""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Protocol, get_args, get_origin, get_type_hints

from ._content import Icon
from ._protocol import PromptInfo, ResourceInfo, ResourceTemplateInfo, ToolInfo
from ._transport import MCPRequest


@dataclass(frozen=True, slots=True)
class Doc:
    """Documents a handler argument.

    Used inside :data:`~typing.Annotated` metadata::

        code: Annotated[str, Doc("The code to review")]
    """

    documentation: str


@dataclass(frozen=True, slots=True)
class Header:
    """Mirrors a tool argument into an ``Mcp-Param-{name}`` HTTP header.

    Used inside :data:`~typing.Annotated` metadata on :class:`str`, :class:`int`, or
    :class:`bool` tool parameters, so network intermediaries can route on the
    value without parsing the body::

        region: Annotated[str, Header("Region")]
    """

    name: str


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    required: bool
    documentation: str | None
    # An ``Mcp-Param-*`` header name from :class:`.Header` metadata.
    header: str | None = None
    # The unwrapped type hint, :attr:`inspect.Parameter.empty` when unannotated.
    hint: object = inspect.Parameter.empty


def _derive_name(handler: Callable[..., object], override: str | None, kind: str) -> str:
    name = override or getattr(handler, "__name__", "")
    if not name:
        raise ValueError(f"cannot derive a name for {kind} handler {handler!r}")
    return name


def _split_docstring(handler: Callable[..., object]) -> tuple[str, str]:
    """The (summary line, body) of the docstring, as (title, description)."""
    summary, _, body = (inspect.getdoc(handler) or "").partition("\n")
    return summary.strip(), body.strip()


def _inspect_handler(
    handler: Callable[..., object], *, name: str, kind: str, typed: bool = False
) -> tuple[bool, list[_Parameter]]:
    """Validate a handler's signature and describe its arguments.

    Returns whether the first parameter receives the :class:`MCPRequest`,
    and the remaining parameters, which must be keyword-addressable. Unless
    ``typed``, they must be ``str``s (optionally ``Annotated`` with
    :class:`Doc` metadata); typed callers interpret the hints themselves.
    """
    hints = get_type_hints(handler, include_extras=True)
    wants_request = False
    parameters: list[_Parameter] = []
    for index, parameter in enumerate(inspect.signature(handler).parameters.values()):
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ValueError(f"{kind} {name!r} must not use *args/**kwargs")
        hint = hints.get(parameter.name, inspect.Parameter.empty)
        doc = None
        header = None
        if get_origin(hint) is Annotated:
            hint, *metadata = get_args(hint)
            doc = next((m.documentation for m in metadata if isinstance(m, Doc)), None)
            header = next((m.name for m in metadata if isinstance(m, Header)), None)
        if index == 0 and hint is MCPRequest:
            wants_request = True
            continue
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise ValueError(
                f"{kind} argument {parameter.name!r} of {name!r} must be"
                " addressable by keyword"
            )
        if not typed and hint not in {inspect.Parameter.empty, str}:
            raise ValueError(f"{kind} argument {parameter.name!r} of {name!r} must be a str")
        parameters.append(
            _Parameter(
                name=parameter.name,
                required=parameter.default is parameter.empty,
                documentation=doc,
                header=header,
                hint=hint,
            )
        )
    return wants_request, parameters


class _Described(Protocol):
    """A descriptor deriving display metadata from its handler function."""

    @property
    def handler(self) -> Callable[..., object]: ...
    @property
    def title(self) -> str | None: ...
    @property
    def description(self) -> str | None: ...
    @property
    def icons(self) -> Sequence[Icon] | None: ...


def _apply_details(
    info: "PromptInfo | ResourceInfo | ResourceTemplateInfo | ToolInfo",
    descriptor: _Described,
) -> None:
    """Fill in the display metadata shared by the feature descriptors."""
    summary, body = _split_docstring(descriptor.handler)
    if title := descriptor.title or summary:
        info["title"] = title
    if description := descriptor.description or body:
        info["description"] = description
    if descriptor.icons is not None:
        info["icons"] = tuple(icon._to_wire() for icon in descriptor.icons)
