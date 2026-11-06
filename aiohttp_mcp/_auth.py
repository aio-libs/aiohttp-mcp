"""Authorization: OAuth 2.1 resource-server behaviour for the MCP endpoint."""

import re
import sys
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, NoReturn, NotRequired, TypedDict

if sys.version_info >= (3, 12):
    from typing import TypeAliasType
else:
    from typing_extensions import TypeAliasType

from aiohttp import hdrs, web
from yarl import URL

from ._protocol import INVALID_REQUEST, MCPError, error_envelope

# https://datatracker.ietf.org/doc/html/rfc9728#name-well-known-uri-registry
_WELL_KNOWN: Final = "/.well-known/oauth-protected-resource"

# b64token syntax of RFC 6750 section 2.1 (token68 of RFC 9110).
_B64TOKEN: Final = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
# scope-token syntax of RFC 6749 section 3.3.
_SCOPE_TOKEN: Final = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")

_LOOPBACK: Final = frozenset({"127.0.0.1", "::1", "localhost"})


def _secure_url(url: URL) -> bool:
    return url.scheme == "https" or (url.scheme == "http" and url.host in _LOOPBACK)



@dataclass(frozen=True, slots=True)
class TokenInfo:
    """Validated access-token information produced by a :data:`TokenValidator`."""

    #: The scopes granted to the token, from the ``scope`` claim.
    scopes: frozenset[str] = frozenset()
    #: Who the token was issued to (the ``sub`` claim), when known.
    subject: str | None = None
    #: All claims returned by the validator, for application-specific checks
    #: (e.g. ``email``); keys are validator-dependent.
    claims: Mapping[str, object] = field(default_factory=dict)


# TODO(PY312): use the `type` statement.
ScopeMatcher = TypeAliasType("ScopeMatcher", Callable[[str, frozenset[str]], bool])
TokenValidator = TypeAliasType(
    "TokenValidator", Callable[[str, web.Request], Awaitable["TokenInfo | None"]]
)

TOKEN_INFO_KEY: Final = web.RequestKey("TOKEN_INFO_KEY", TokenInfo)


# TODO(PY315): closed=True
class ProtectedResourceMetadata(TypedDict):
    """The RFC 9728 metadata subset emitted by this library."""

    resource: str
    authorization_servers: Sequence[str]
    bearer_methods_supported: Sequence[str]
    scopes_supported: NotRequired[Sequence[str]]
    resource_name: NotRequired[str]
    resource_documentation: NotRequired[str]


class _AuthError(Exception):
    """Carries the finished HTTP response for a failed authentication."""

    def __init__(self, response: web.Response) -> None:
        super().__init__(response.reason)
        self.response = response


class Authorization:
    """Authorization configuration for an MCP endpoint.

    :param validator: Asynchronous callable validating a presented bearer token. The
        callback will receive the token and the request and must return a
        :class:`TokenInfo` object or :data:`None` if the token is not valid.
    :param resource: Base URL this MCP server is reachable at: scheme and
        host (and optional port), plus the mount prefix when served from a
        sub-application — e.g. ``"https://mcp.mydomain.example"`` or
        ``"https://mcp.mydomain.example/api"``. Must be absolute ``http(s)``,
        canonical, without query or fragment. The canonical resource
        identifier that clients request tokens for is completed by
        :func:`~aiohttp_mcp.setup`, which appends the MCP endpoint path — so
        the identifier always matches the endpoint it is served on.
    :param auth_servers: Issuer identifiers of at least one authorization
        server from which clients can obtain access tokens.
    :param default_scopes: The initial set of scopes advertised to a client.
        Generally a client will start by requesting an access token with all
        of these scopes. It is recommended to advertise only enough scopes for
        basic functionality. Clients will re-authenticate to acquire new scopes
        when needed for additional functionality.
    :param scope_matcher: A callback which receives the requested scope and the
        user's current scopes and returns True if the requested scope is
        allowed, False otherwise. This can be used to implement scope hierarchy.
        Defaults to an exact match (:code:`requested in user_scopes`).
    :param resource_name: Optional human-readable name for display purposes.
    :param resource_documentation: Optional URL of developer documentation.
    """

    _resource: str
    _metadata_path: str
    _metadata_url: str
    _metadata: ProtectedResourceMetadata

    def __init__(
        self,
        validator: TokenValidator,
        *,
        resource: str,
        auth_servers: Sequence[str],
        default_scopes: Iterable[str] = (),
        scope_matcher: ScopeMatcher | None = None,
        resource_name: str | None = None,
        resource_documentation: str | None = None,
    ) -> None:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#canonical-server-uri
        resource = resource.rstrip("/")
        resource_url = URL(resource)
        if (
            not resource_url.is_absolute()
            or resource_url.scheme not in {"http", "https"}
            or resource_url.host is None
            or resource_url.query_string
            or resource_url.fragment
        ):
            raise ValueError(
                "resource must be an absolute http(s) URL without query or fragment"
            )
        if str(resource_url) != resource:
            raise ValueError(f"resource must be in canonical form: {str(resource_url)!r}")
        if "%" in resource:
            raise ValueError("percent-encoded resource paths are not supported")
        self._origin: Final = str(resource_url.origin())
        if not resource.startswith(self._origin):
            # e.g. userinfo, which origin() strips.
            raise ValueError("resource must not contain userinfo")
        if not auth_servers:
            raise ValueError("at least one authorization server is required")
        for issuer in auth_servers:
            issuer_url = URL(issuer)
            if not issuer_url.is_absolute() or not _secure_url(issuer_url):
                # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations#communication-security
                raise ValueError(f"authorization server must be an HTTPS URL: {issuer!r}")

        self._default_scopes = tuple(default_scopes)
        for scope in self._default_scopes:
            if not _SCOPE_TOKEN.fullmatch(scope):
                raise ValueError(f"invalid scope token: {scope!r}")

        self._validator = validator
        self._base: Final = resource
        self._scope_matcher: ScopeMatcher = (
            scope_matcher if scope_matcher is not None else lambda s, scopes: s in scopes
        )
        self._auth_servers = tuple(auth_servers)
        self._resource_name = resource_name
        self._resource_documentation = resource_documentation

    def _bind(self, path: str) -> None:
        """Complete the resource identifier with the MCP endpoint ``path``."""
        resource = self._base + (path.rstrip("/") if path != "/" else "")
        if hasattr(self, "_resource"):
            if self._resource != resource:
                raise RuntimeError(
                    f"authorization is already bound to {self._resource!r}; "
                    "use a separate Authorization instance per endpoint"
                )
            return
        if str(URL(resource)) != resource or "%" in resource:
            raise ValueError(f"endpoint path is not canonical: {path!r}")
        self._resource = resource

        # RFC 9728 path-insertion relative to the resource identifier:
        # https://mcp.example.com/mcp -> /.well-known/oauth-protected-resource/mcp
        self._metadata_path = _WELL_KNOWN + resource[len(self._origin):]
        self._metadata_url = self._origin + self._metadata_path

        self._metadata = {
            "resource": self._resource,
            "authorization_servers": self._auth_servers,
            # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#token-requirements
            "bearer_methods_supported": ("header",),
        }
        if self._default_scopes:
            self._metadata["scopes_supported"] = self._default_scopes
        if self._resource_name is not None:
            self._metadata["resource_name"] = self._resource_name
        if self._resource_documentation is not None:
            self._metadata["resource_documentation"] = self._resource_documentation

    def _register_routes(self, app: web.Application) -> None:
        """Register any routes and lifecycle hooks on app."""

    def register_well_known(self, app: web.Application) -> None:
        """Register the well-known discovery documents.

        These must be served from the origin root (:rfc:`9728`/:rfc:`8414`
        path-inserted well-known URIs): call on the root application.
        """
        if not hasattr(self, "_resource"):
            raise RuntimeError("setup() must be called before register_well_known()")
        app.router.add_get(self._metadata_path, self._handle_metadata)

    async def _handle_metadata(self, request: web.Request) -> web.Response:
        """GET handler for the RFC 9728 well-known metadata document."""
        return web.json_response(self._metadata)

    def _challenge(
        self,
        error: str | None,
        description: str | None,
        scopes: Collection[str] | None = None,
    ) -> str:
        # https://datatracker.ietf.org/doc/html/rfc6750#section-3
        # https://datatracker.ietf.org/doc/html/rfc9728#name-www-authenticate-response
        params = [f'resource_metadata="{self._metadata_url}"']
        if error is not None:
            params.append(f'error="{error}"')
        if description is not None:
            params.append(f'error_description="{description}"')
        if effective := (scopes or self._default_scopes):
            params.append(f'scope="{" ".join(effective)}"')
        return "Bearer " + ", ".join(params)

    def _fail(
        self,
        status: int,
        error: str | None = None,
        description: str | None = None,
        scopes: Collection[str] | None = None,
    ) -> NoReturn:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#error-handling
        body = error_envelope(MCPError(INVALID_REQUEST, description or "Unauthorized"))
        response = web.json_response(
            body,
            status=status,
            headers={hdrs.WWW_AUTHENTICATE: self._challenge(error, description, scopes)},
        )
        raise _AuthError(response)

    async def _authenticate(
        self,
        request: web.Request,
        scopes: Collection[str] | None = None,
    ) -> TokenInfo:
        """Validate the request's bearer token and the operation's scopes.

        Raises ``_AuthError`` with the appropriate challenge when the token is
        missing/invalid (401), malformed (400), or lacks one of ``scopes``
        (403).  ``scopes`` is the current operation's scope requirements when
        known.
        """
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#token-requirements
        if "access_token" in request.query:
            self._fail(
                400,
                "invalid_request",
                "Access tokens must not be sent in the URI query string",
                scopes=scopes,
            )

        # TODO(aiohttp4): Update this for HeadersDictProxy
        headers = request.headers.getall(hdrs.AUTHORIZATION, ())
        if not headers:
            self._fail(401, scopes=scopes)
        if len(headers) > 1:
            self._fail(
                400, "invalid_request", "Multiple Authorization headers", scopes=scopes
            )
        scheme, _, token = headers[0].partition(" ")
        token = token.strip(" ")
        if scheme.lower() != "bearer" or not _B64TOKEN.fullmatch(token):
            self._fail(
                400, "invalid_request", "Malformed Authorization header", scopes=scopes
            )

        info = await self._validator(token, request)
        if info is None:
            self._fail(
                401, "invalid_token", "Invalid or expired access token", scopes=scopes
            )
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#runtime-insufficient-scope-errors
        if scopes and not all(self._scope_matcher(s, info.scopes) for s in scopes):
            self._fail(403, "insufficient_scope", "Insufficient scope", scopes=scopes)
        return info
