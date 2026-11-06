"""A self-contained OAuth 2.1 authorization server for a single MCP resource."""

import asyncio
import base64
import binascii
import hashlib
import heapq
import hmac
import html
import json
import logging
import secrets
import socket
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Final, NoReturn, TypedDict

if sys.version_info >= (3, 12):
    from typing import TypeAliasType
else:
    from typing_extensions import TypeAliasType

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from yarl import URL

from ._auth import (
    _LOOPBACK,
    _SCOPE_TOKEN,
    Authorization,
    ScopeMatcher,
    TokenInfo,
    _secure_url,
)

logger = logging.getLogger("aiohttp_mcp")

# TODO(PY312): use the `type` statement.
AuthenticateRequest = TypeAliasType(
    "AuthenticateRequest", Callable[[web.Request], Awaitable["str | None"]]
)
CheckCredentials = TypeAliasType(
    "CheckCredentials", Callable[[str, str, web.Request], Awaitable[bool]]
)

_CIMD_TIMEOUT: Final = ClientTimeout(total=10)
_CIMD_MAX_SIZE: Final = 64 * 1024
_CIMD_CACHE_TTL: Final = 300.0
_CIMD_CACHE_MAX: Final = 16
_SESSION_COOKIE: Final = "aiohttp_mcp_session"
# Our endpoints all live under this prefix; the session cookie is scoped to it.
_OAUTH_PATH: Final = "/oauth2"
_CONSENT_TTL: Final = 600.0
_REVOKED: Final = -1

_FORM_FIELDS: Final = (
    "response_type",
    "client_id",
    "redirect_uri",
    "state",
    "scope",
    "resource",
    "code_challenge",
    "code_challenge_method",
)

_CONSENT_TEMPLATE: Final = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Authorize {server}</title></head>
<body>
<h1>Authorize access to {server}</h1>
<p><b>{client}</b> is requesting access{scopes}.</p>
<p>Approving will send the authorization code to <b>{redirect_host}</b>.</p>
{warning}<p>Signed in as <b>{subject}</b>.</p>
{error}<form method="post">
{hidden}<button name="action" value="approve">Approve</button>
<button name="action" value="deny">Deny</button>
</form>
</body>
</html>"""

_LOGIN_TEMPLATE: Final = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Sign in to {server}</title></head>
<body>
<h1>Sign in to {server}</h1>
{error}<form method="post">
{hidden}<label>Username <input name="username" autocomplete="username"></label><br>
<label>Password <input type="password" name="password" autocomplete="current-password"></label><br>
<button>Sign in</button>
</form>
</body>
</html>"""


def _valid_redirect_uris(uris: Sequence[object]) -> bool:
    """Redirect URIs MUST be either localhost or use HTTPS."""
    try:
        return bool(uris) and all(
            isinstance(u, str) and _secure_url(URL(u)) for u in uris
        )
    except ValueError:  # unparseable URI
        return False


async def _cimd_url_allowed(url: URL) -> bool:
    """Fetch policy for Client ID Metadata Documents.

    These are server-side fetches of a *client-controlled* URL: https only,
    public hosts only - a private, loopback, or link-local target would turn
    the authorization server into an SSRF proxy for its own network.
    """
    host = url.host
    if url.scheme != "https" or host is None:
        return False
    try:
        addresses = [ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, url.port, type=socket.SOCK_STREAM
            )
        except OSError:
            return False
        try:
            addresses = [ip_address(info[4][0]) for info in infos]
        except ValueError:
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


@dataclass(frozen=True, slots=True)
class ConsentContext:
    """Everything a consent page needs to render and echo back.

    A custom page MUST render an HTML form that POSTs back to the same URL, carrying
    every entry of :attr:`fields` unmodified (hidden inputs, see
    :attr:`hidden_fields_html`) together with ``action`` (``"approve"`` to
    proceed; any other value denies).
    """

    # The MCP server's display name (``resource_name`` or the resource URI).
    server_name: str
    # The requesting client's self-reported display name.  Client-controlled:
    # MUST be HTML-escaped when rendered.
    client_name: str
    # The requesting client's identifier.  Client-controlled, as above.
    client_id: str
    # Scopes that approval will grant.
    scopes: tuple[str, ...]
    # The authenticated user approving the request.
    subject: str
    # Hostname of the redirect URI that will receive the authorization code.
    # Pages MUST display it clearly..
    redirect_host: str
    # True when the redirect goes to the local machine.
    # Pages SHOULD warn that any locally running program could be listening.
    redirect_is_loopback: bool
    # Parameters the form must echo back verbatim, including the anti-forgery consent token.
    fields: Mapping[str, str]
    # Set when re-prompting (e.g. after an expired consent token).
    error: str | None

    @property
    def hidden_fields_html(self) -> str:
        """:attr:`fields` as escaped ``<input type="hidden">`` elements."""
        return "".join(
            f'<input type="hidden" name="{html.escape(name, quote=True)}"'
            f' value="{html.escape(value, quote=True)}">\n'
            for name, value in self.fields.items()
        )


# TODO(PY312): use the `type` statement.
RenderConsent = TypeAliasType(
    "RenderConsent", Callable[[web.Request, ConsentContext], Awaitable[web.Response]]
)


async def _render_consent_default(request: web.Request, context: ConsentContext) -> web.Response:
    scopes = " ".join(context.scopes)
    warning = (
        "<p><b>Warning:</b> the code will be sent to an application running "
        "on this computer; make sure you initiated this request locally.</p>\n"
        if context.redirect_is_loopback
        else ""
    )
    body = _CONSENT_TEMPLATE.format(
        server=html.escape(context.server_name),
        client=html.escape(context.client_name),
        subject=html.escape(context.subject),
        redirect_host=html.escape(context.redirect_host),
        warning=warning,
        scopes=f" with scopes: {html.escape(scopes)}" if scopes else "",
        error=f"<p><b>{html.escape(context.error)}</b></p>\n" if context.error else "",
        hidden=context.hidden_fields_html,
    )
    return web.Response(text=body, content_type="text/html")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _Signer:
    """HMAC-SHA256 signing of JSON payloads (``body.signature``, base64url)."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def sign(self, payload: Mapping[str, object]) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{_b64(hmac.digest(self._secret, body.encode(), 'sha256'))}"

    def verify(self, token: str) -> dict[str, object] | None:
        body, _, signature = token.partition(".")
        if not signature:
            return None
        expected = _b64(hmac.digest(self._secret, body.encode(), "sha256"))
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, binascii.Error):
            logger.error(f"Correctly signed token carries an unparseable payload: {body!r}")
            return None
        if not isinstance(payload, dict):
            logger.error(f"Correctly signed token carries a non-object payload: {payload!r}")
            return None
        return payload


@dataclass(frozen=True, slots=True)
class _Client:
    redirect_uris: Sequence[str]
    name: str


class _PageError(Exception):
    """Invalid client/redirect_uri: rendered as a 400 page, never redirected."""


class _RedirectError(Exception):
    """Reported to the client via redirect (RFC 6749 section 4.1.2.1)."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


class _AuthServerMetadata(TypedDict):
    """The RFC 8414 metadata subset emitted by this server."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    response_types_supported: Sequence[str]
    grant_types_supported: Sequence[str]
    code_challenge_methods_supported: Sequence[str]
    token_endpoint_auth_methods_supported: Sequence[str]
    authorization_response_iss_parameter_supported: bool
    client_id_metadata_document_supported: bool
    scopes_supported: Sequence[str]


def _redirect_allowed(registered: Sequence[str], uri: str) -> bool:
    """Exact match, with the OAuth 2.1 variable-port exception for loopback."""
    if uri in registered:
        return True
    requested = URL(uri)
    if requested.host not in _LOOPBACK:
        return False
    return any(
        (reg := URL(r)).host == requested.host
        and reg.scheme == requested.scheme
        and reg.path == requested.path
        and reg.query_string == requested.query_string
        for r in registered
    )


def _token_error(code: str, description: str) -> web.Response:
    # https://datatracker.ietf.org/doc/html/rfc6749#section-5.2
    return web.json_response(
        {"error": code, "error_description": description},
        status=400,
        headers={"Cache-Control": "no-store"},
    )


class LocalAuthorization(Authorization):
    """Self-contained authorization: this app is its own authorization server.

    :param check_credentials: Asynchronous callable verifying a username/password
        pair presented at the hosted login page.
    :param resource: Base URL this MCP server is reachable at, as for
        :class:`Authorization`. It is also the authorization
        server's issuer identity, and the :brand:`OAuth` endpoints live under
        ``/oauth2/`` beneath it: they are registered on this application by
        :func:`~aiohttp_mcp.setup`; register the well-known documents via
        :meth:`register_well_known` on the root application.
    :param secret: Key signing tokens and authorization codes. Random when
        omitted, which invalidates tokens on restart. Can be the same secret
        passed as ``state_secret`` to :func:`~aiohttp_mcp.setup`.
    :param clients: Statically registered clients, mapping ``client_id`` to
        its exact ``redirect_uris``. Clients using Client ID Metadata
        Documents (an ``https`` ``client_id`` URL) need no registration.
    :param default_scopes: As for :class:`Authorization`, the minimal scope
        set for basic functionality, advertised to clients as the starting
        point and granted when a client requests no particular scopes.
    :param grantable_scopes: Every scope this authorization server is willing
        to grant (advertised as the :rfc:`8414` ``scopes_supported``). Requested
        scopes outside it are rejected with ``invalid_scope``. Defaults to
        ``default_scopes`` - set it when step-up scopes beyond the starting
        set exist.
    :param token_ttl: Access token lifetime in seconds.
    :param code_ttl: Authorization code lifetime in seconds.
    :param refresh_ttl: Absolute lifetime in seconds of a refresh-token grant
        (the whole rotation chain, not each token), after which the user must
        re-authorize. :data:`None` disables refresh tokens entirely.
    :param render_consent: Replaces the hosted consent page. Receives the
        request and a :class:`ConsentContext`. The page it returns must
        satisfy the form contract documented there.
    :param authenticate_request: Returns the authenticated subject for a
        request using the application's existing session mechanism (with
        :mod:`aiohttp_security` use :func:`~aiohttp_security.authorized_userid`), or
        :data:`None`. Consulted before the built-in session cookie.
    :param login_url: The application's own login page, to which
        unauthenticated users are sent (with the authorize URL appended as a
        query parameter) instead of the built-in ``/oauth2/login``. After
        logging the user in, the page must redirect back to that URL.
        Requires ``authenticate_request``, which is how the authorization
        endpoint sees the resulting session.
    :param login_redirect_param: Name of the query parameter carrying the
        post-login return URL, for both the built-in and ``login_url`` pages.
    :param login_ttl: Lifetime in seconds of the built-in login session cookie.
    """

    def __init__(
        self,
        check_credentials: CheckCredentials | None = None,
        *,
        resource: str,
        secret: bytes | None = None,
        clients: Mapping[str, Sequence[str]] | None = None,
        default_scopes: Sequence[str] = (),
        grantable_scopes: Sequence[str] = (),
        scope_matcher: ScopeMatcher | None = None,
        token_ttl: float = 3600.0,
        code_ttl: float = 120.0,
        refresh_ttl: float | None = 30 * 86400.0,
        render_consent: RenderConsent | None = None,
        authenticate_request: AuthenticateRequest | None = None,
        login_url: str | None = None,
        login_redirect_param: str = "redirect",
        login_ttl: float = 86400.0,
        resource_name: str | None = None,
        resource_documentation: str | None = None,
    ) -> None:
        if token_ttl <= 0 or code_ttl <= 0:
            raise ValueError("token_ttl and code_ttl must be > 0")
        if refresh_ttl is not None and refresh_ttl <= 0:
            raise ValueError("refresh_ttl must be > 0 or None")
        if login_ttl <= 0:
            raise ValueError("login_ttl must be > 0")
        if check_credentials is None and login_url is None:
            raise ValueError(
                "a way to log users in is required: provide check_credentials "
                "(built-in login page) and/or login_url (the app's own page)"
            )
        if login_url is not None and authenticate_request is None:
            raise ValueError(
                "login_url requires authenticate_request: without it the authorization "
                "endpoint cannot see the session the app's login page establishes"
            )
        if not login_redirect_param:
            raise ValueError("login_redirect_param must be a non-empty string")
        super().__init__(
            self._validate_token,
            resource=resource,
            auth_servers=(resource.rstrip("/"),),
            default_scopes=default_scopes,
            scope_matcher=scope_matcher,
            resource_name=resource_name,
            resource_documentation=resource_documentation,
        )
        self._issuer: Final = self._base
        self._prefix = URL(self._base).path.rstrip("/")
        self._oauth_path = f"{self._prefix}{_OAUTH_PATH}"
        self._oauth_app: web.Application | None = None
        self._check_credentials = check_credentials
        self._render_consent = (
            _render_consent_default if render_consent is None else render_consent
        )
        self._authenticate_request = authenticate_request
        self._login_url = login_url
        self._login_redirect_param = login_redirect_param
        self._login_ttl = login_ttl
        # Relative paths the login page may redirect back to.
        self._return_prefixes: tuple[str, ...] = ()
        self._extra_return_prefixes: list[Callable[[], str]] = []
        self._signer = _Signer(secret if secret is not None else secrets.token_bytes(32))
        self._grantable_scopes = (
            tuple(grantable_scopes) if grantable_scopes else tuple(default_scopes)
        )
        for scope in self._grantable_scopes:
            if not _SCOPE_TOKEN.fullmatch(scope):
                raise ValueError(f"invalid scope token: {scope!r}")
        if not set(default_scopes) <= set(self._grantable_scopes):
            raise ValueError("default_scopes must all be grantable")
        self._token_ttl = token_ttl
        self._code_ttl = code_ttl
        self._refresh_ttl = refresh_ttl
        for client_id, uris in (clients or {}).items():
            if not _valid_redirect_uris(tuple(uris)):
                raise ValueError(f"redirect URIs must be localhost or HTTPS: {client_id!r}")
        self._static_clients = {
            client_id: _Client(tuple(uris), client_id)
            for client_id, uris in (clients or {}).items()
        }
        self._cimd_cache: dict[str, tuple[float, _Client]] = {}
        self._used_codes: dict[str, float] = {}
        # Latest issued generation (or _REVOKED) and expiry per refresh family.
        self._refresh_families: dict[str, tuple[int, float]] = {}
        self._family_heap: list[tuple[float, str]] = []
        self._http: ClientSession | None = None
        self._as_metadata: _AuthServerMetadata = {
            # https://datatracker.ietf.org/doc/html/rfc8414#section-2
            "issuer": self._issuer,
            "authorization_endpoint": f"{self._issuer}{_OAUTH_PATH}/authorize",
            "token_endpoint": f"{self._issuer}{_OAUTH_PATH}/token",
            "response_types_supported": ("code",),
            "grant_types_supported": ("authorization_code",)
                if refresh_ttl is None else ("authorization_code", "refresh_token"),
            "code_challenge_methods_supported": ("S256",),
            "token_endpoint_auth_methods_supported": ("none",),
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": True,
            "scopes_supported": self._grantable_scopes,
        }

    def _register_routes(self, app: web.Application) -> None:
        """Mount the authorization server as a sub-application at ``/oauth2``."""
        super()._register_routes(app)
        oauth = web.Application()
        oauth.router.add_get("/authorize", self._handle_authorize_page, name="authorize")
        oauth.router.add_post("/authorize", self._handle_authorize_submit)
        oauth.router.add_post("/token", self._handle_token, name="token")
        if self._check_credentials is not None:
            oauth.router.add_get("/login", self._handle_login_page, name="login")
            oauth.router.add_post("/login", self._handle_login_submit)
        oauth.cleanup_ctx.append(self._client_session)
        oauth.on_startup.append(self._finalise_return_prefixes)
        app.add_subapp(_OAUTH_PATH, oauth)
        self._oauth_app = oauth

    async def _finalise_return_prefixes(self, app: web.Application) -> None:
        self._return_prefixes = (
            self._endpoint_path("authorize"), *(r() for r in self._extra_return_prefixes)
        )

    def _endpoint_path(self, name: str) -> str:
        """The real mounted path of one of our endpoints, from the router."""
        assert self._oauth_app is not None  # _register_routes() ran
        return str(self._oauth_app.router[name].url_for())

    def register_well_known(self, app: web.Application) -> None:
        super().register_well_known(app)
        # https://datatracker.ietf.org/doc/html/rfc8414#section-3: path
        # insertion for a path-bearing issuer, at the origin root.
        as_metadata_path = URL(self._issuer).path
        app.router.add_get(
            "/.well-known/oauth-authorization-server"
            + (as_metadata_path if as_metadata_path != "/" else ""),
            self._handle_as_metadata,
        )

    async def _client_session(self, app: web.Application) -> AsyncIterator[None]:
        async with ClientSession(timeout=_CIMD_TIMEOUT) as sess:
            self._http = sess
            yield

    # - Token validation (the resource-server side) -------------------------

    async def _validate_token(self, token: str, request: web.Request) -> TokenInfo | None:
        payload = self._signer.verify(token)
        if payload is None or payload.get("t") != "token":
            return None
        expiry = payload.get("exp")
        if not isinstance(expiry, (int, float)) or expiry <= time.time():
            return None
        if payload.get("aud") != self._resource:  # RFC 8707 audience binding
            return None
        scope = payload.get("scope")
        subject = payload.get("sub")
        return TokenInfo(
            scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
            subject=subject if isinstance(subject, str) else None,
            claims=payload,
        )

    # - Client resolution ---------------------------------------------------

    async def _resolve_client(self, client_id: str) -> _Client:
        if (static := self._static_clients.get(client_id)) is not None:
            return static
        # https://datatracker.ietf.org/doc/html/draft-ietf-oauth-client-id-metadata-document-00
        url = URL(client_id)
        if not await _cimd_url_allowed(url):
            raise _PageError("Unknown client_id")
        cached = self._cimd_cache.get(client_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        assert self._http is not None
        try:
            async with self._http.get(client_id, allow_redirects=False) as resp:
                if resp.status != 200:
                    raise _PageError("Failed to fetch client metadata")
                raw = await resp.content.read(_CIMD_MAX_SIZE + 1)
        except ClientError:
            raise _PageError("Failed to fetch client metadata") from None
        if len(raw) > _CIMD_MAX_SIZE:
            raise _PageError("Client metadata document too large")
        try:
            doc = json.loads(raw)
        except ValueError:
            raise _PageError("Invalid client metadata document") from None
        redirect_uris = doc.get("redirect_uris") if isinstance(doc, dict) else None
        if (
            not isinstance(doc, dict)
            or doc.get("client_id") != client_id
            or not isinstance(redirect_uris, list)
            or not _valid_redirect_uris(redirect_uris)
        ):
            raise _PageError("Invalid client metadata document")
        name = doc.get("client_name")
        client = _Client(tuple(redirect_uris), name if isinstance(name, str) else client_id)
        now = time.monotonic()
        for stale in [c for c, (expiry, _) in self._cimd_cache.items() if expiry <= now]:
            del self._cimd_cache[stale]
        while len(self._cimd_cache) >= _CIMD_CACHE_MAX:
            del self._cimd_cache[min(self._cimd_cache, key=lambda c: self._cimd_cache[c][0])]
        self._cimd_cache[client_id] = (now + _CIMD_CACHE_TTL, client)
        return client

    # - Authorization endpoint ----------------------------------------------

    async def _check_authorize(self, args: Mapping[str, str]) -> tuple[_Client, dict[str, str]]:
        client_id = args.get("client_id")
        if not client_id:
            raise _PageError("Missing client_id")
        client = await self._resolve_client(client_id)
        redirect_uri = args.get("redirect_uri")
        if not redirect_uri or not _redirect_allowed(client.redirect_uris, redirect_uri):
            raise _PageError("Invalid redirect_uri")

        if args.get("response_type") != "code":
            raise _RedirectError("unsupported_response_type", "Must be response_type=code")
        # PKCE is mandatory (OAuth 2.1); only S256 is offered.
        if not args.get("code_challenge"):
            raise _RedirectError("invalid_request", "code_challenge is required")
        if args.get("code_challenge_method") != "S256":
            raise _RedirectError("invalid_request", "code_challenge_method must be S256")
        resource = args.get("resource")
        if resource and resource != self._resource:
            # RFC 8707: tokens are only issued for this server's resource.
            raise _RedirectError("invalid_target", "Unknown resource")
        requested = args.get("scope", "").split()
        if not set(requested) <= set(self._grantable_scopes):
            raise _RedirectError("invalid_scope", "Unknown scope requested")
        params = {field: args.get(field, "") for field in _FORM_FIELDS}
        # Least privilege: an unspecific request grants the default starting
        # set, not everything this server could grant.
        params["scope"] = " ".join(requested if requested else self._default_scopes)
        return client, params

    # - Sessions --------------------------------------------------------------

    async def _subject(self, request: web.Request) -> str | None:
        """The authenticated user, from the app's session or our own cookie."""
        if self._authenticate_request is not None:
            subject = await self._authenticate_request(request)
            if subject is not None:
                return subject
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie is not None:
            payload = self._signer.verify(cookie)
            if payload is not None and payload.get("t") == "session":
                expiry = payload.get("exp")
                cookie_sub = payload.get("sub")
                if (
                    isinstance(expiry, (int, float))
                    and expiry > time.time()
                    and isinstance(cookie_sub, str)
                ):
                    return cookie_sub
        return None

    def _login_redirect(self, authorize_url: URL) -> NoReturn:
        """Send an unauthenticated user to the login page, and back after."""
        target = URL(self._login_url or self._endpoint_path("login"))
        location = target.update_query({self._login_redirect_param: str(authorize_url)})
        raise web.HTTPSeeOther(location=location)

    def _return_url(self, args: Mapping[str, str]) -> URL:
        """The URL a login page should send the user back to."""
        return URL(self._endpoint_path("authorize")).with_query(
            {field: value for field in _FORM_FIELDS if (value := args.get(field))}
        )

    def _consent_fields(self, subject: str, params: Mapping[str, str]) -> dict[str, str]:
        """The echoed form fields, plus the subject-bound anti-forgery token."""
        fields = {f: params[f] for f in _FORM_FIELDS if params[f]}
        fields["consent"] = self._signer.sign(
            {"t": "consent", "sub": subject, "exp": time.time() + _CONSENT_TTL}
        )
        return fields

    def _consent_valid(self, token: str, subject: str) -> bool:
        payload = self._signer.verify(token)
        if payload is None or payload.get("t") != "consent":
            return False
        expiry = payload.get("exp")
        return (
            isinstance(expiry, (int, float))
            and expiry > time.time()
            and payload.get("sub") == subject
        )

    async def _render_consent_page(
        self,
        request: web.Request,
        client: _Client,
        params: Mapping[str, str],
        subject: str,
        error: str | None = None,
    ) -> web.Response:
        redirect_url = URL(params["redirect_uri"])
        context = ConsentContext(
            server_name=self._metadata.get("resource_name", self._resource),
            client_name=client.name,
            client_id=params["client_id"],
            scopes=tuple(params["scope"].split()),
            subject=subject,
            redirect_host=redirect_url.host or params["redirect_uri"],
            redirect_is_loopback=redirect_url.host in _LOOPBACK,
            fields=self._consent_fields(subject, params),
            error=error,
        )
        return await self._render_consent(request, context)

    def _redirect(self, redirect_uri: str, state: str, **params: str) -> NoReturn:
        location = URL(redirect_uri).update_query(
            # https://datatracker.ietf.org/doc/html/rfc9207#section-2
            {**params, "iss": self._issuer, **({"state": state} if state else {})}
        )
        raise web.HTTPSeeOther(location=location)

    async def _error_redirect(
        self, request: web.Request, args: Mapping[str, str], exc: _RedirectError
    ) -> NoReturn:
        """Report a validation error to the client via its redirect URI.

        The user MUST be authenticated first (OAuth 2.1 section 7.12.2):
        otherwise a crafted link with a deliberately invalid request would
        bounce an unauthenticated victim straight to any registered (e.g.
        Client ID Metadata Document) site, using our origin as a trusted
        one-click redirect.
        """
        if await self._subject(request) is None:
            self._login_redirect(self._return_url(args))
        self._redirect(
            args["redirect_uri"],
            args.get("state", ""),
            error=exc.error,
            error_description=exc.description,
        )

    async def _handle_authorize_page(self, request: web.Request) -> web.StreamResponse:
        try:
            client, params = await self._check_authorize(request.query)
        except _PageError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        except _RedirectError as exc:
            await self._error_redirect(request, request.query, exc)
        subject = await self._subject(request)
        if subject is None:
            self._login_redirect(self._return_url(params))
        return await self._render_consent_page(request, client, params, subject)

    async def _handle_authorize_submit(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        args = {k: v for k, v in form.items() if isinstance(v, str)}
        try:
            client, params = await self._check_authorize(args)
        except _PageError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        except _RedirectError as exc:
            await self._error_redirect(request, args, exc)

        origin = request.headers.get("Origin")
        if origin is not None and origin != self._issuer:
            raise web.HTTPForbidden(text="Cross-origin form submission")
        subject = await self._subject(request)
        if subject is None:
            self._login_redirect(self._return_url(params))

        state = params["state"]
        if args.get("action") != "approve":
            self._redirect(
                params["redirect_uri"],
                state,
                error="access_denied",
                error_description="The user denied the request",
            )
        if not self._consent_valid(args.get("consent", ""), subject):
            return await self._render_consent_page(
                request, client, params, subject, error="Please confirm again"
            )

        code = self._signer.sign(
            {
                "t": "code",
                "jti": secrets.token_urlsafe(16),
                "sub": subject,
                "cid": params["client_id"],
                "ruri": params["redirect_uri"],
                "aud": self._resource,
                "scope": params["scope"],
                "cc": params["code_challenge"],
                "exp": time.time() + self._code_ttl,
            }
        )
        self._redirect(params["redirect_uri"], state, code=code)

    # - Built-in login page ---------------------------------------------------

    def _safe_return_url(self, value: str | None) -> str:
        if value is not None:
            url = URL(value)
            if not url.is_absolute() and url.path.startswith(self._return_prefixes):
                return str(url)
        return "/"

    def _render_login_page(self, redirect: str, error: str | None) -> web.Response:
        hidden = (
            f'<input type="hidden" name="{html.escape(self._login_redirect_param, quote=True)}"'
            f' value="{html.escape(redirect, quote=True)}">\n'
        )
        body = _LOGIN_TEMPLATE.format(
            server=html.escape(self._metadata.get("resource_name", self._resource)),
            error=f"<p><b>{html.escape(error)}</b></p>\n" if error else "",
            hidden=hidden,
        )
        return web.Response(text=body, content_type="text/html")

    async def _handle_login_page(self, request: web.Request) -> web.StreamResponse:
        redirect = self._safe_return_url(request.query.get(self._login_redirect_param))
        if await self._subject(request) is not None:
            # Already signed in: straight back to the authorization flow.
            raise web.HTTPSeeOther(location=redirect)
        return self._render_login_page(redirect, error=None)

    async def _handle_login_submit(self, request: web.Request) -> web.StreamResponse:
        assert self._check_credentials is not None  # route only added with it
        form = await request.post()
        args = {k: v for k, v in form.items() if isinstance(v, str)}
        redirect = self._safe_return_url(args.get(self._login_redirect_param))
        username = args.get("username", "")
        if not username or not await self._check_credentials(
            username, args.get("password", ""), request
        ):
            return self._render_login_page(redirect, error="Invalid credentials")

        session = self._signer.sign(
            {"t": "session", "sub": username, "exp": time.time() + self._login_ttl}
        )
        response = web.HTTPSeeOther(location=redirect)
        response.set_cookie(
            _SESSION_COOKIE,
            session,
            max_age=int(self._login_ttl),
            path=self._endpoint_path("login").removesuffix("/login"),
            httponly=True,
            samesite="Lax",
            secure=request.secure,
        )
        raise response

    # - Token endpoint -------------------------------------------------------

    def _consume_code(self, jti: str, expiry: float) -> bool:
        """Single-use enforcement. Returns False on replay."""
        now = time.time()
        for used_jti, used_expiry in tuple(self._used_codes.items()):
            if used_expiry <= now:
                del self._used_codes[used_jti]
        if jti in self._used_codes:
            return False
        self._used_codes[jti] = expiry
        return True

    async def _handle_token(self, request: web.Request) -> web.Response:
        form = await request.post()
        args = {k: v for k, v in form.items() if isinstance(v, str)}
        grant_type = args.get("grant_type")
        if grant_type == "authorization_code":
            return self._grant_authorization_code(args)
        if grant_type == "refresh_token" and self._refresh_ttl is not None:
            return self._grant_refresh_token(args)
        return _token_error("unsupported_grant_type", "Unsupported grant_type")

    def _grant_authorization_code(self, args: Mapping[str, str]) -> web.Response:
        payload = self._signer.verify(args.get("code", ""))
        if payload is None or payload.get("t") != "code":
            return _token_error("invalid_grant", "Invalid authorization code")
        expiry = payload.get("exp")
        jti = payload.get("jti")
        if (
            not isinstance(expiry, (int, float))
            or expiry <= time.time()
            or not isinstance(jti, str)
            or not self._consume_code(jti, expiry)
        ):
            return _token_error("invalid_grant", "Expired or already used authorization code")
        if args.get("client_id") != payload.get("cid"):
            return _token_error("invalid_grant", "Code was issued to a different client")
        if args.get("redirect_uri") != payload.get("ruri"):
            return _token_error("invalid_grant", "redirect_uri does not match")
        verifier = args.get("code_verifier", "")
        # https://datatracker.ietf.org/doc/html/rfc7636#section-4.6
        if not verifier or _b64(hashlib.sha256(verifier.encode()).digest()) != payload.get("cc"):
            return _token_error("invalid_grant", "PKCE verification failed")
        resource = args.get("resource")
        if resource and resource != self._resource:
            return _token_error("invalid_target", "Unknown resource")

        subject = payload.get("sub")
        scope = payload.get("scope")
        return self._issue_grant(
            subject=subject if isinstance(subject, str) else None,
            grant_scope=scope if isinstance(scope, str) else "",
            access_scope=scope if isinstance(scope, str) else "",
            family=secrets.token_urlsafe(16),
            generation=0,
            family_exp=time.time() + (self._refresh_ttl or 0),
        )

    def _grant_refresh_token(self, args: Mapping[str, str]) -> web.Response:
        # Refresh token rotation for public clients
        # (https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13#section-4.3.1).
        payload = self._signer.verify(args.get("refresh_token", ""))
        if payload is None or payload.get("t") != "refresh":
            return _token_error("invalid_grant", "Invalid refresh token")
        expiry = payload.get("exp")
        family = payload.get("fam")
        generation = payload.get("gen")
        grant_scope = payload.get("scope")
        if (
            not isinstance(expiry, (int, float))
            or expiry <= time.time()
            or not isinstance(family, str)
            or not isinstance(generation, int)
            or not isinstance(grant_scope, str)
            or payload.get("aud") != self._resource
        ):
            return _token_error("invalid_grant", "Expired refresh token")
        resource = args.get("resource")
        if resource and resource != self._resource:
            return _token_error("invalid_target", "Unknown resource")

        self._prune_families()
        current = self._refresh_families.get(family)
        # An unknown family (process restart) is accepted: the signature and
        # expiry still gate it. A known family only accepts its latest token.
        if current is not None and current[0] != generation:
            # A rotated-out token was presented: assume the grant is
            # compromised and revoke the whole family.
            self._refresh_families[family] = (_REVOKED, current[1])
            return _token_error("invalid_grant", "Refresh token reuse; grant revoked")

        requested = args.get("scope", "").split()
        if not set(requested) <= set(grant_scope.split()):
            # https://datatracker.ietf.org/doc/html/rfc6749#section-6
            return _token_error("invalid_scope", "Scope exceeds the original grant")
        subject = payload.get("sub")
        return self._issue_grant(
            subject=subject if isinstance(subject, str) else None,
            grant_scope=grant_scope,
            access_scope=" ".join(requested) if requested else grant_scope,
            family=family,
            generation=generation + 1,
            family_exp=expiry,
        )

    def _prune_families(self) -> None:
        now = time.time()
        while self._family_heap and self._family_heap[0][0] <= now:
            _, family = heapq.heappop(self._family_heap)
            del self._refresh_families[family]

    def _issue_grant(
        self,
        *,
        subject: str | None,
        grant_scope: str,
        access_scope: str,
        family: str,
        generation: int,
        family_exp: float,
    ) -> web.Response:
        now = time.time()
        token = self._signer.sign(
            {
                "t": "token",
                "sub": subject,
                "aud": self._resource,
                "scope": access_scope,
                "iat": now,
                "exp": now + self._token_ttl,
            }
        )
        # https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13#section-4.1.3
        grant: dict[str, object] = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": int(self._token_ttl),
            "scope": access_scope,
        }
        if self._refresh_ttl is not None:
            grant["refresh_token"] = self._signer.sign(
                {
                    "t": "refresh",
                    "fam": family,
                    "gen": generation,
                    "sub": subject,
                    "aud": self._resource,
                    "scope": grant_scope,
                    "exp": family_exp,
                }
            )
            if family not in self._refresh_families:
                heapq.heappush(self._family_heap, (family_exp, family))
            self._refresh_families[family] = (generation, family_exp)
        return web.json_response(grant, headers={"Cache-Control": "no-store"})

    async def _handle_as_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(self._as_metadata)
