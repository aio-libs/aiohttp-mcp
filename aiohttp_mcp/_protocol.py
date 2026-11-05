"""Wire-level definitions for the MCP base protocol, revision 2026-07-28.

https://modelcontextprotocol.io/specification/2026-07-28
"""

import base64
import binascii
import sys
from collections.abc import Mapping, Sequence
from typing import (
    Final,
    Literal,
    NotRequired,
    Required,
    TypedDict,
    cast,
)

if sys.version_info >= (3, 13):
    from typing import ReadOnly
else:
    from typing_extensions import ReadOnly

#: Protocol revisions implemented, newest first.
PROTOCOL_VERSIONS: Final = ("2026-07-28",)

# https://modelcontextprotocol.io/specification/2026-07-28/basic#_meta
META_PROTOCOL_VERSION: Final = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO: Final = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES: Final = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO: Final = "io.modelcontextprotocol/serverInfo"
META_SUBSCRIPTION_ID: Final = "io.modelcontextprotocol/subscriptionId"

# https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#request-metadata
PROTOCOL_VERSION_HDR: Final = "MCP-Protocol-Version"
METHOD_HDR: Final = "Mcp-Method"
NAME_HDR: Final = "Mcp-Name"

# Methods requiring the ``Mcp-Name`` header, mapped to the params field it mirrors.
NAME_HDR_METHODS: Final[Mapping[str, str]] = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}

# https://modelcontextprotocol.io/specification/2026-07-28/basic#error-codes
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
HEADER_MISMATCH: Final = -32020
MISSING_CLIENT_CAPABILITY: Final = -32021
UNSUPPORTED_PROTOCOL_VERSION: Final = -32022

_HTTP_STATUS: Final[Mapping[int, int]] = {
    # These 2 not mandated in spec.
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#protocol-version-header
    METHOD_NOT_FOUND: 404,
    # https://modelcontextprotocol.io/specification/2026-07-28/basic#_meta
    INVALID_PARAMS: 400,
    # Not mandated in spec.
    INTERNAL_ERROR: 500,
    # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-behavior-for-custom-headers
    HEADER_MISMATCH: 400,
    # https://modelcontextprotocol.io/specification/2026-07-28/basic#_meta
    MISSING_CLIENT_CAPABILITY: 400,
    # https://modelcontextprotocol.io/specification/2026-07-28/schema#requestmetaobject-iomodelcontextprotocolprotocolversion
    UNSUPPORTED_PROTOCOL_VERSION: 400,
}

_B64_PREFIX: Final = "=?base64?"
_B64_SUFFIX: Final = "?="

# https://modelcontextprotocol.io/specification/2026-07-28/basic#requests
RequestId = str | int
# https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/progress#progress-flow
ProgressToken = str | int
# https://modelcontextprotocol.io/specification/2026-07-28/server/prompts#promptmessage
Role = Literal["user", "assistant"]


# TODO(PY315): closed=True
class _Elicitation(TypedDict, total=False):
    form: dict[str, object]
    url: dict[str, object]


# TODO(PY315): closed=True
class _ListChanged(TypedDict, total=False):
    listChanged: bool


# TODO(PY315): closed=True
class _Resources(TypedDict, total=False):
    listChanged: bool
    subscribe: bool


# TODO(PY315): closed=True
class ClientCapabilities(TypedDict, total=False):
    experimental: dict[str, dict[str, object]]
    elicitation: _Elicitation
    extensions: dict[str, object]


# TODO(PY315): closed=True
class ServerCapabilities(TypedDict, total=False):
    experimental: dict[str, dict[str, object]]
    completions: dict[str, object]
    prompts: _ListChanged
    resources: _Resources
    tools: _ListChanged
    extensions: dict[str, dict[str, object]]


# TODO(PY315): closed=True
class Icon(TypedDict):
    src: str
    mimeType: NotRequired[str]
    sizes: NotRequired[Sequence[str]]
    theme: NotRequired[Literal["light", "dark"]]


# TODO(PY315): closed=True
class Implementation(TypedDict):
    icons: NotRequired[Sequence[Icon]]
    name: str
    title: NotRequired[str]
    version: str
    description: NotRequired[str]
    websiteUrl: NotRequired[str]


# https://modelcontextprotocol.io/specification/2026-07-28/basic#_meta
RequestMeta = TypedDict(
    "RequestMeta",
    {
        "progressToken": NotRequired[ProgressToken],
        "io.modelcontextprotocol/protocolVersion": str,
        "io.modelcontextprotocol/clientCapabilities": ClientCapabilities,
        "io.modelcontextprotocol/clientInfo": NotRequired[Implementation],
    },
)

ResultMeta = TypedDict("ResultMeta", {
    "io.modelcontextprotocol/serverInfo": NotRequired[Implementation]
})


# TODO(PY315): closed=True
class Error(TypedDict):
    code: int
    message: str
    data: NotRequired[object]


class InputResult(TypedDict, total=False):
    """A result as produced by a method handler.

    The protocol-mandated fields are read-only so that per-method result
    types may redeclare them as required/mutable; the transport fills them
    in when building the wire result.
    """

    _meta: ReadOnly[ResultMeta]
    resultType: ReadOnly[str]


class Result(InputResult):
    """A complete result as sent on the wire."""

    _meta: NotRequired[ResultMeta]
    resultType: Required[str]


# TODO(PY315): closed=True
class JSONRPCErrorResponse(TypedDict):
    jsonrpc: Literal["2.0"]
    id: NotRequired[RequestId]
    error: Error


# TODO(PY315): closed=True
class JSONRPCNotification(TypedDict):
    jsonrpc: Literal["2.0"]
    method: str
    params: NotRequired[dict[str, object]]


# TODO(PY315): closed=True
class JSONRPCRequest(TypedDict):
    jsonrpc: Literal["2.0"]
    id: RequestId
    method: str
    params: NotRequired[dict[str, object]]


# TODO(PY315): closed=True
class JSONRPCResultResponse(TypedDict):
    jsonrpc: Literal["2.0"]
    id: RequestId
    result: Result


class DiscoverResult(InputResult):
    supportedVersions: Sequence[str]
    capabilities: ServerCapabilities
    instructions: NotRequired[str]
    ttlMs: int
    cacheScope: Literal["public", "private"]


# TODO(PY315): closed=True
class SubscriptionFilter(TypedDict, total=False):
    toolsListChanged: bool
    promptsListChanged: bool
    resourcesListChanged: bool
    resourceSubscriptions: Sequence[str]


SubscriptionsListenResultMetaObject = TypedDict(
    "SubscriptionsListenResultMetaObject",
    {
        "io.modelcontextprotocol/serverInfo": NotRequired[Implementation],
        "io.modelcontextprotocol/subscriptionId": RequestId
    },
)


class SubscriptionsListenResult(InputResult):
    _meta: Required[ReadOnly[SubscriptionsListenResultMetaObject]]


# TODO(PY315): closed=True
class Annotations(TypedDict, total=False):
    audience: Sequence[Role]
    priority: float
    lastModified: str


# TODO(PY315): closed=True
class TextContent(TypedDict):
    type: Literal["text"]
    text: str
    annotations: NotRequired[Annotations]


# TODO(PY315): closed=True
class ImageContent(TypedDict):
    type: Literal["image"]
    data: str
    mimeType: str
    annotations: NotRequired[Annotations]


# TODO(PY315): closed=True
class AudioContent(TypedDict):
    type: Literal["audio"]
    data: str
    mimeType: str
    annotations: NotRequired[Annotations]


# TODO(PY315): closed=True
class ResourceLink(TypedDict):
    type: Literal["resource_link"]
    name: str
    uri: str
    title: NotRequired[str]
    description: NotRequired[str]
    mimeType: NotRequired[str]
    icons: NotRequired[Sequence[Icon]]
    size: NotRequired[int]
    annotations: NotRequired[Annotations]


# TODO(PY315): closed=True
class TextResourceContents(TypedDict):
    uri: str
    text: str
    mimeType: NotRequired[str]


# TODO(PY315): closed=True
class BlobResourceContents(TypedDict):
    uri: str
    blob: str
    mimeType: NotRequired[str]


# TODO(PY315): closed=True
class EmbeddedResource(TypedDict):
    type: Literal["resource"]
    resource: TextResourceContents | BlobResourceContents
    annotations: NotRequired[Annotations]


ContentBlock = TextContent | ImageContent | AudioContent | ResourceLink | EmbeddedResource


# TODO(PY315): closed=True
class PromptMessage(TypedDict):
    role: Role
    content: ContentBlock


# TODO(PY315): closed=True
class PromptArgument(TypedDict):
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    required: NotRequired[bool]


# Named ``Prompt`` in the specification schema; renamed to avoid colliding
# with the public aiohttp_mcp.Prompt descriptor.
# TODO(PY315): closed=True
class PromptInfo(TypedDict):
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    arguments: NotRequired[Sequence[PromptArgument]]
    icons: NotRequired[Sequence[Icon]]


class ListPromptsResult(InputResult):
    prompts: Sequence[PromptInfo]
    nextCursor: NotRequired[str]
    ttlMs: int
    cacheScope: Literal["public", "private"]


class GetPromptResult(InputResult):
    description: NotRequired[str]
    messages: Sequence[PromptMessage]


# Named ``Resource`` in the specification schema.
# TODO(PY315): closed=True
class ResourceInfo(TypedDict):
    uri: str
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    mimeType: NotRequired[str]
    size: NotRequired[int]
    icons: NotRequired[Sequence[Icon]]
    annotations: NotRequired[Annotations]


# Named ``ResourceTemplate`` in the specification schema.
# TODO(PY315): closed=True
class ResourceTemplateInfo(TypedDict):
    uriTemplate: str
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    mimeType: NotRequired[str]
    icons: NotRequired[Sequence[Icon]]
    annotations: NotRequired[Annotations]


class ListResourcesResult(InputResult):
    resources: Sequence[ResourceInfo]
    nextCursor: NotRequired[str]
    ttlMs: int
    cacheScope: Literal["public", "private"]


class ListResourceTemplatesResult(InputResult):
    resourceTemplates: Sequence[ResourceTemplateInfo]
    nextCursor: NotRequired[str]
    ttlMs: int
    cacheScope: Literal["public", "private"]


class ReadResourceResult(InputResult):
    contents: Sequence[TextResourceContents | BlobResourceContents]
    ttlMs: int
    cacheScope: Literal["public", "private"]


_InputSchema = TypedDict("_InputSchema", {
    "$schema": NotRequired[str], "type": Literal["object"]
})
_OutputSchema = TypedDict("_OutputSchema", {"$schema": NotRequired[str]})


# TODO(PY315): closed=True
class ToolAnnotations(TypedDict, total=False):
    title: str
    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


# Named ``Tool`` in the specification schema.
# TODO(PY315): closed=True
class ToolInfo(TypedDict):
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    inputSchema: _InputSchema
    outputSchema: NotRequired[_OutputSchema]
    annotations: NotRequired[ToolAnnotations]
    icons: NotRequired[Sequence[Icon]]


class ListToolsResult(InputResult):
    tools: Sequence[ToolInfo]
    nextCursor: NotRequired[str]
    ttlMs: int
    cacheScope: Literal["public", "private"]


class CallToolResult(InputResult):
    content: Sequence[ContentBlock]
    structuredContent: NotRequired[object]
    isError: NotRequired[bool]


# TODO(PY315): closed=True
class _SchemaCommon(TypedDict, total=False):
    title: str
    description: str


class StringSchema(_SchemaCommon, total=False):
    type: Required[Literal["string"]]
    minLength: int
    maxLength: int
    format: Literal["uri", "email", "date", "date-time"]
    default: str


class NumberSchema(_SchemaCommon, total=False):
    type: Required[Literal["number", "integer"]]
    minimum: float
    maximum: float
    default: float


class BooleanSchema(_SchemaCommon, total=False):
    type: Required[Literal["boolean"]]
    default: bool


class UntitledSingleSelectEnumSchema(_SchemaCommon, total=False):
    type: Required[Literal["string"]]
    enum: Required[Sequence[str]]
    default: str


class _Enum(TypedDict):
    const: str
    title: str


class TitledSingleSelectEnumSchema(_SchemaCommon, total=False):
    type: Required[Literal["string"]]
    oneOf: Required[Sequence[_Enum]]
    default: str


class _Item(TypedDict):
    type: Literal["string"]
    enum: Sequence[str]


class UntitledMultiSelectEnumSchema(_SchemaCommon, total=False):
    type: Required[Literal["array"]]
    minItems: int
    maxItems: int
    items: Required[_Item]
    default: Sequence[str]


class _AnyOfEnums(TypedDict):
    anyOf: Sequence[_Enum]


class TitledMultiSelectEnumSchema(_SchemaCommon, total=False):
    type: Required[Literal["array"]]
    minItems: int
    maxItems: int
    items: Required[_AnyOfEnums]
    default: Sequence[str]


SingleSelectEnumSchema = UntitledSingleSelectEnumSchema | TitledSingleSelectEnumSchema
MultiSelectEnumSchema = UntitledMultiSelectEnumSchema | TitledMultiSelectEnumSchema
EnumSchema = SingleSelectEnumSchema | MultiSelectEnumSchema
PrimitiveSchemaDefinition = StringSchema | NumberSchema | BooleanSchema | EnumSchema


_RequestedSchema = TypedDict("_RequestedSchema", {
    "$schema": NotRequired[str],
    "type": Literal["object"],
    "properties": Mapping[str, PrimitiveSchemaDefinition],
    "required": NotRequired[Sequence[str]]
})


# TODO(PY315): closed=True
class ElicitRequestFormParams(TypedDict):
    mode: Literal["form"]
    message: str
    requestedSchema: _RequestedSchema


# TODO(PY315): closed=True
class ElicitRequestURLParams(TypedDict):
    mode: Literal["url"]
    message: str
    url: str


# TODO(PY315): closed=True
class ElicitRequest(TypedDict):
    method: Literal["elicitation/create"]
    params: ElicitRequestFormParams | ElicitRequestURLParams


class InputRequiredResult(InputResult):
    inputRequests: NotRequired[Mapping[str, ElicitRequest]]
    requestState: NotRequired[str]


class MCPError(Exception):
    """A JSON-RPC error response to be sent to the client."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: object = None,
        request_id: "RequestId | None" = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id

    @property
    def http_status(self) -> int:
        """HTTP status used when this error forms the whole response body."""
        return _HTTP_STATUS.get(self.code, 200)

    def to_dict(self) -> Error:
        error: Error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def validate_request_meta(meta: object) -> RequestMeta:
    if not isinstance(meta, dict):
        raise MCPError(INVALID_PARAMS, "params._meta is required")
    if not isinstance(meta.get(META_PROTOCOL_VERSION), str):
        raise MCPError(INVALID_PARAMS, f"_meta[{META_PROTOCOL_VERSION!r}] is required")
    if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
        raise MCPError(
            INVALID_PARAMS, f"_meta[{META_CLIENT_CAPABILITIES!r}] is required"
        )
    client_info = meta.get(META_CLIENT_INFO)
    if client_info is not None and not isinstance(client_info, dict):
        raise MCPError(INVALID_PARAMS, f"_meta[{META_CLIENT_INFO!r}] must be an object")
    return cast(RequestMeta, meta)


def decode_header_value(value: str) -> str:
    """Decode the Base64 sentinel format used by ``Mcp-Name``/``Mcp-Param-*``."""
    if not (value.startswith(_B64_PREFIX) and value.endswith(_B64_SUFFIX)):
        return value
    encoded = value[len(_B64_PREFIX) : -len(_B64_SUFFIX)]
    try:
        return base64.b64decode(encoded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise MCPError(HEADER_MISMATCH, "Malformed Base64-encoded header value") from None


def error_envelope(error: MCPError, request_id: RequestId | None = None) -> JSONRPCErrorResponse:
    envelope: JSONRPCErrorResponse = {"jsonrpc": "2.0", "error": error.to_dict()}
    if request_id is not None:
        envelope["id"] = request_id
    return envelope
