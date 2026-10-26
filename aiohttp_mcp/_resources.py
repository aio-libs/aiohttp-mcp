"""Resources: data exposed to clients, identified by URIs."""

import re
import sys
from base64 import b64encode
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Literal, TypeVar, overload
from urllib.parse import unquote

if sys.version_info >= (3, 12):
    from typing import TypeAliasType
else:
    from typing_extensions import TypeAliasType

from ._content import EmbeddedResource, Icon
from ._handlers import _apply_details, _derive_name, _inspect_handler
from ._protocol import (
    INVALID_PARAMS,
    Annotations,
    BlobResourceContents,
    ListResourcesResult,
    ListResourceTemplatesResult,
    MCPError,
    ReadResourceResult,
    ResourceInfo,
    ResourceTemplateInfo,
    Role,
    TextResourceContents,
)
from ._transport import MCPRequest

_ResourceT = TypeVar("_ResourceT", ResourceInfo, ResourceTemplateInfo)


@dataclass(frozen=True, slots=True)
class ResourceContents:
    """The contents of one resource, as returned from a resource function.

    Exactly one of ``text`` (a text resource) or ``blob`` (a binary resource)
    must be given.  Simple functions can return a plain :class:`str` or
    :class:`bytes` instead; a sequence of :class:`.ResourceContents` returns
    several contents at once (e.g. the files of a directory resource).

    :param uri: URI of these contents; the requested URI when omitted.
    """

    uri: str | None = None
    text: str | None = field(default=None, kw_only=True)
    blob: bytes | None = field(default=None, kw_only=True)
    mime_type: str | None = field(default=None, kw_only=True)

    if TYPE_CHECKING:
        # Static form of the __post_init__ check: exactly one of text/blob.
        @overload
        def __init__(
            self,
            uri: str | None = None,
            *,
            text: str,
            mime_type: str | None = None,
        ) -> None: ...
        @overload
        def __init__(
            self,
            uri: str | None = None,
            *,
            blob: bytes,
            mime_type: str | None = None,
        ) -> None: ...
        def __init__(
            self,
            uri: str | None = None,
            *,
            text: str | None = None,
            blob: bytes | None = None,
            mime_type: str | None = None,
        ) -> None: ...

    def __post_init__(self) -> None:
        if (self.text is None) == (self.blob is None):
            raise ValueError("exactly one of text or blob is required")

    def _to_wire(self, uri: str) -> TextResourceContents | BlobResourceContents:
        wire: TextResourceContents | BlobResourceContents
        if self.text is not None:
            wire = {"uri": self.uri or uri, "text": self.text}
        else:
            assert self.blob is not None
            wire = {"uri": self.uri or uri, "blob": b64encode(self.blob).decode()}
        if self.mime_type is not None:
            wire["mimeType"] = self.mime_type
        return wire


# TODO(PY312): use the `type` statement.
ResourceResult = TypeAliasType(
    "ResourceResult", str | bytes | ResourceContents | Sequence[ResourceContents]
)
ResourceHandler = TypeAliasType("ResourceHandler", Callable[..., Awaitable[ResourceResult]])

_TEMPLATE_EXPR = re.compile(r"\{([^{}]*)\}")
_VAR_NAME = re.compile(r"\+?[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Resource:
    """A resource description.

    :param handler: The resource function returning the contents.
    :param uri: The resource's URI, or an :rfc:`6570` URI template whose
        variables are the ``handler``'s parameters.
    :param name: Resource identifier; the function name when omitted.
    :param title: Human-readable display name; the docstring's summary line
        when omitted (:pep:`257`).
    :param description: Human-readable description; the docstring's body
        (everything after the summary line) when omitted.
    :param mime_type: MIME type of the contents, when fixed.
    :param size: Size of the contents in bytes, when known (not available on templates).
    :param icons: Optional icons for display in user interfaces.
    :param audience: Hint of the intended audience, as for message content.
    :param priority: How important this resource is, from 0 to 1.
    :param last_modified: ISO 8601 timestamp of the last modification.
    :param scopes: Scopes required to use this resource.
    :param ttl: Seconds a client is recommended to cache the contents for,
        overriding the :class:`.ResourceRegistry`'s ``read_ttl``.
    :param cache_scope: Whether shared intermediaries may cache the contents,
        overriding the :class:`ResourceRegistry`'s ``read_cache_scope``.
    """

    handler: ResourceHandler
    uri: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None
    icons: Sequence[Icon] | None = None
    audience: Sequence[Role] | None = field(default=None, kw_only=True)
    priority: float | None = field(default=None, kw_only=True)
    last_modified: str | None = field(default=None, kw_only=True)
    scopes: Collection[str] = ()
    ttl: float | None = None
    cache_scope: Literal["public", "private"] | None = None

    def _annotations(self) -> Annotations:
        annotations: Annotations = {}
        if self.audience is not None:
            annotations["audience"] = list(self.audience)
        if self.priority is not None:
            annotations["priority"] = self.priority
        if self.last_modified is not None:
            annotations["lastModified"] = self.last_modified
        return annotations


@dataclass(frozen=True, slots=True)
class _ResourceSpec(Generic[_ResourceT]):
    resource: Resource
    info: _ResourceT
    wants_request: bool
    pattern: re.Pattern[str] | None
    decode: frozenset[str]
    scopes: frozenset[str]


_AnyResourceSpec = _ResourceSpec[ResourceInfo] | _ResourceSpec[ResourceTemplateInfo]


def _parse_template(uri: str) -> tuple[re.Pattern[str], frozenset[str]] | None:
    """Compile a URI template to a matching regex; ``None`` if not a template."""
    pattern = ""
    decode = set()
    names = set()
    position = 0
    for expression in _TEMPLATE_EXPR.finditer(uri):
        variable = expression.group(1)
        if not _VAR_NAME.fullmatch(variable):
            raise ValueError(
                f"unsupported URI template expression {{{variable}}} in {uri!r};"
                " only {var} and {+var} are supported"
            )
        pattern += re.escape(uri[position:expression.start()])
        position = expression.end()
        if variable.startswith("+"):
            variable = variable[1:]
            pattern += f"(?P<{variable}>.+)"
        else:
            decode.add(variable)
            pattern += f"(?P<{variable}>[^/?#]+)"
        if variable in names:
            raise ValueError(f"duplicate variable {variable!r} in {uri!r}")
        names.add(variable)

    if not pattern:  # Not a templated URI
        return None
    pattern += re.escape(uri[position:])
    return re.compile(pattern), frozenset(decode)


def _inspect_resource(resource: Resource, variables: frozenset[str]) -> tuple[str, bool]:
    """Validate the handler against ``variables``. Return (name, wants_request)."""
    name = _derive_name(resource.handler, resource.name, "resource")
    wants_request, params = _inspect_handler(resource.handler, name=name, kind="resource")
    if {parameter.name for parameter in params} != variables:
        raise ValueError(
            f"resource {name!r} parameters {sorted(p.name for p in params)} do not match"
            f" the URI template variables {sorted(variables)}"
        )
    return name, wants_request


def _apply_resource_details(
    resource: Resource, info: ResourceInfo | ResourceTemplateInfo
) -> None:
    """Fill in the metadata fields shared by resources and templates."""
    _apply_details(info, resource)
    if resource.mime_type is not None:
        info["mimeType"] = resource.mime_type
    if annotations := resource._annotations():
        info["annotations"] = annotations


def _template_index_key(uri: str) -> str:
    """The index key for a template: its fixed part up to a ``/`` boundary.

    As in aiohttp.web.UrlDispatcher._get_resource_index_key.
    """
    return uri.partition("{")[0].rpartition("/")[0].rstrip("/")


def _build_static(resource: Resource) -> _ResourceSpec[ResourceInfo]:
    name, wants_request = _inspect_resource(resource, frozenset())
    info: ResourceInfo = {"uri": resource.uri, "name": name}
    if resource.size is not None:
        info["size"] = resource.size
    _apply_resource_details(resource, info)
    return _ResourceSpec(
        resource=resource,
        info=info,
        wants_request=wants_request,
        pattern=None,
        decode=frozenset(),
        scopes=frozenset(resource.scopes),
    )


def _build_template(
    resource: Resource, pattern: re.Pattern[str], decode: frozenset[str]
) -> _ResourceSpec[ResourceTemplateInfo]:
    if resource.size is not None:
        raise ValueError(f"size is not applicable to template {resource.uri!r}")
    name, wants_request = _inspect_resource(resource, frozenset(pattern.groupindex))
    info: ResourceTemplateInfo = {"uriTemplate": resource.uri, "name": name}
    _apply_resource_details(resource, info)
    return _ResourceSpec(
        resource=resource,
        info=info,
        wants_request=wants_request,
        pattern=pattern,
        decode=decode,
        scopes=frozenset(resource.scopes),
    )


class ResourceRegistry:
    """A collection of resources for an MCP server.

    :param resources: The resources to serve.
    :param ttl: Seconds a client is recommended to cache the resource and
        template lists for.
    :param cache_scope: Whether shared intermediaries may cache the lists
        (``"public"``) or only the requesting client (``"private"``, the safe
        default: with authorization configured the lists may vary by the
        caller's scopes).
    :param read_ttl: Seconds a client is recommended to cache resource
        contents for; resources can override it individually.
    :param read_cache_scope: Cache scope of resource contents; resources can
        override it individually.
    """

    def __init__(
        self,
        *resources: Resource,
        ttl: float = 300.0,
        cache_scope: Literal["public", "private"] = "private",
        read_ttl: float = 60.0,
        read_cache_scope: Literal["public", "private"] = "private",
    ) -> None:
        if ttl < 0 or read_ttl < 0:
            raise ValueError("ttl must be >= 0")
        for scope in (cache_scope, read_cache_scope):
            if scope not in {"public", "private"}:
                raise ValueError("cache_scope must be 'public' or 'private'")
        self._ttl_ms = round(ttl * 1000)
        self._cache_scope: Literal["public", "private"] = cache_scope
        self._read_ttl_ms = round(read_ttl * 1000)
        self._read_cache_scope: Literal["public", "private"] = read_cache_scope
        self._static: dict[str, _ResourceSpec[ResourceInfo]] = {}
        self._templates: dict[str, _ResourceSpec[ResourceTemplateInfo]] = {}
        self._template_index: dict[str, list[_ResourceSpec[ResourceTemplateInfo]]] = {}
        for resource in resources:
            if resource.uri in self._static or resource.uri in self._templates:
                raise ValueError(f"duplicate resource URI {resource.uri!r}")
            if (template := _parse_template(resource.uri)) is None:
                self._static[resource.uri] = _build_static(resource)
            else:
                spec = self._templates[resource.uri] = _build_template(resource, *template)
                self._template_index.setdefault(
                    _template_index_key(resource.uri), []
                ).append(spec)

    @property
    def _scoped(self) -> bool:
        """Whether any resource is scope-restricted (then authorization is required)."""
        return any(
            spec.scopes
            for registry in (self._static, self._templates)
            for spec in registry.values()
        )

    def _resolve(self, uri: str) -> tuple[_AnyResourceSpec, dict[str, str]] | None:
        """Find the resource serving ``uri`` and its template arguments."""
        if (static := self._static.get(uri)) is not None:
            return static, {}
        # Walk the URI parts looking for template candidates, as in
        # aiohttp.UrlDispatcher.resolve().
        uri_part = uri
        while True:
            for spec in self._template_index.get(uri_part, ()):
                assert spec.pattern is not None
                if m := spec.pattern.fullmatch(uri):
                    args = {
                        k: unquote(v) if k in spec.decode else v
                        for k, v in m.groupdict().items()
                    }
                    return spec, args
            if not uri_part:
                return None
            uri_part = uri_part.rpartition("/")[0]

    def _visible(
        self, ctx: MCPRequest, specs: dict[str, _ResourceSpec[_ResourceT]]
    ) -> list[_ResourceT]:
        """The visible resource definitions for this caller's authorization."""
        auth = ctx.auth
        return [
            s.info for s in specs.values()
            if not s.scopes or (auth is not None and s.scopes <= auth.scopes)
        ]

    async def _get_scopes(self, ctx: MCPRequest) -> Collection[str]:
        """Scope requirements of a ``resources/read`` call (``Method.scopes``)."""
        uri = ctx.params.get("uri")
        resolved = self._resolve(uri) if isinstance(uri, str) else None
        return () if resolved is None else resolved[0].scopes

    async def _list(self, ctx: MCPRequest) -> ListResourcesResult:
        """resources/list endpoint."""
        if "cursor" in ctx.params:
            raise MCPError(INVALID_PARAMS, "Invalid cursor")
        return {
            "resources": self._visible(ctx, self._static),
            "ttlMs": self._ttl_ms,
            "cacheScope": self._cache_scope,
        }

    async def _templates_list(self, ctx: MCPRequest) -> ListResourceTemplatesResult:
        """resources/templates/list endpoint."""
        if "cursor" in ctx.params:
            raise MCPError(INVALID_PARAMS, "Invalid cursor")
        return {
            "resourceTemplates": self._visible(ctx, self._templates),
            "ttlMs": self._ttl_ms,
            "cacheScope": self._cache_scope,
        }

    async def _fetch(
        self, ctx: MCPRequest, spec: _AnyResourceSpec, arguments: dict[str, str]
    ) -> tuple[ResourceContents, ...]:
        """Invoke a resource's handler and normalise the contents."""
        if spec.wants_request:
            result = await spec.resource.handler(ctx, **arguments)
        else:
            result = await spec.resource.handler(**arguments)
        if isinstance(result, str):
            return (ResourceContents(text=result, mime_type=spec.resource.mime_type),)
        if isinstance(result, bytes):
            return (ResourceContents(blob=result, mime_type=spec.resource.mime_type),)
        if isinstance(result, ResourceContents):
            return (result,)
        return tuple(result)

    async def _read(self, ctx: MCPRequest) -> ReadResourceResult:
        """resources/read endpoint."""
        uri = ctx.params.get("uri")
        if not isinstance(uri, str):
            raise MCPError(INVALID_PARAMS, "uri must be a string")
        resolved = self._resolve(uri)
        if resolved is None:
            # https://modelcontextprotocol.io/specification/2026-07-28/server/resources#error-handling
            raise MCPError(INVALID_PARAMS, "Resource not found", data={"uri": uri})
        spec, arguments = resolved
        contents = await self._fetch(ctx, spec, arguments)
        resource = spec.resource
        ttl_ms = self._read_ttl_ms if resource.ttl is None else round(resource.ttl * 1000)
        return {
            "contents": [c._to_wire(uri) for c in contents],
            "ttlMs": ttl_ms,
            "cacheScope": resource.cache_scope or self._read_cache_scope,
        }

    async def _embed(self, ctx: MCPRequest, uri: str) -> list[EmbeddedResource]:
        """Implementation of :meth:`.MCPRequest.embed_resource`."""
        resolved = self._resolve(uri)
        if resolved is None:
            raise LookupError(f"No resource matches {uri!r}")
        spec, arguments = resolved
        resource = spec.resource
        embedded = []
        for c in await self._fetch(ctx, spec, arguments):
            if c.text is not None:
                block = EmbeddedResource(
                    c.uri or uri,
                    text=c.text,
                    mime_type=c.mime_type,
                    audience=resource.audience,
                    priority=resource.priority,
                    last_modified=resource.last_modified,
                )
            else:
                assert c.blob is not None  # ResourceContents holds one of the two.
                block = EmbeddedResource(
                    c.uri or uri,
                    blob=c.blob,
                    mime_type=c.mime_type,
                    audience=resource.audience,
                    priority=resource.priority,
                    last_modified=resource.last_modified,
                )
            embedded.append(block)
        return embedded
