"""JWT access-token validation (RFC 9068) for an external authorization server.

Requires the ``jwt`` extra (``pip install aiohttp-mcp[jwt]``).
"""

import logging
from collections.abc import AsyncIterator, Collection, Sequence
from typing import Final

import jwt
from aiohttp import ClientSession, ClientTimeout, web
from yarl import URL

from ._auth import Authorization, ScopeMatcher, TokenInfo, _secure_url
from ._validators import _discover_metadata

logger = logging.getLogger("aiohttp_mcp")

# Asymmetric signature algorithms only: a resource server must never accept
# an HMAC algorithm, where the public key would also sign forgeries.
DEFAULT_ALGORITHMS: Final = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)

# RFC 9068 section 2.1 media types for the JOSE ``typ`` header.
_AT_JWT_TYPES: Final = frozenset({"at+jwt", "application/at+jwt"})


class JWTAuthorization(Authorization):
    """Authorization validating self-contained JWT access tokens (:rfc:`9068`).

    :param issuer: The authorization server's issuer identifier; also the default
        for ``auth_servers``.  The token's ``iss`` claim must match it exactly.
    :param algorithms: Accepted signature algorithms; asymmetric only by default.
    :param typ: Accepted values of the JOSE ``typ`` header, per :rfc:`9068`.
        Pass :data:`None` to skip the check for authorization servers that set the
        ``typ`` of their access tokens to plain JWT - at the cost of also accepting
        look-alike tokens such as OpenID Connect ID tokens.
    :param leeway: Clock-skew allowance in seconds for time-based claims.
    :param timeout: Total timeout in seconds for metadata and JWKS requests.
    """

    def __init__(
        self,
        *,
        issuer: str,
        resource: str,
        auth_servers: Sequence[str] | None = None,
        algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
        typ: Collection[str] | None = _AT_JWT_TYPES,
        leeway: float = 0.0,
        timeout: float = 10.0,
        default_scopes: Sequence[str] = (),
        scope_matcher: ScopeMatcher | None = None,
        resource_name: str | None = None,
        resource_documentation: str | None = None,
    ) -> None:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations#communication-security
        if not _secure_url(URL(issuer)):
            raise ValueError(f"issuer must be an HTTPS URL: {issuer!r}")
        super().__init__(
            self._validate_token,
            resource=resource,
            auth_servers=auth_servers if auth_servers is not None else (issuer,),
            default_scopes=default_scopes,
            scope_matcher=scope_matcher,
            resource_name=resource_name,
            resource_documentation=resource_documentation,
        )
        self._issuer: Final = issuer.rstrip("/")
        self._algorithms = tuple(algorithms)
        self._typ = frozenset(typ) if typ is not None else None
        self._leeway = leeway
        self._timeout = ClientTimeout(total=timeout)
        self._http: ClientSession | None = None
        self._jwks: jwt.PyJWKSet | None = None

    def _register_routes(self, app: web.Application) -> None:
        super()._register_routes(app)
        app.cleanup_ctx.append(self._client_session)

    async def _client_session(self, app: web.Application) -> AsyncIterator[None]:
        self._http = session = ClientSession(timeout=self._timeout)
        yield
        await session.close()

    async def _fetch_jwks(self, url: str) -> jwt.PyJWKSet:
        assert self._http is not None
        async with self._http.get(url) as resp:
            resp.raise_for_status()
            if resp.content_type not in {"application/json", "application/jwk-set+json"}:
                raise ValueError(f"Unexpected JWKS content type: {resp.content_type!r}")
            jwks = await resp.json(content_type=None)
        if not isinstance(jwks, dict):
            raise TypeError("Invalid JWKS document")
        return jwt.PyJWKSet.from_dict(jwks)

    async def _discover_jwks_uri(self) -> str:
        """Locate the signing keys via authorization server metadata."""
        assert self._http is not None
        metadata = await _discover_metadata(self._http, self._issuer)
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not _secure_url(URL(jwks_uri)):
            raise ValueError(f"Metadata for {self._issuer} does not declare an HTTPS jwks_uri")
        return jwks_uri

    async def _signing_key(self, token: str) -> jwt.PyJWK | None:
        kid = jwt.get_unverified_header(token).get("kid")
        for refetch in (False, True):
            if self._jwks is None or refetch:  # unknown kid: keys may have rotated
                self._jwks = await self._fetch_jwks(await self._discover_jwks_uri())
            for key in self._jwks.keys:
                if key.key_id == kid:
                    return key
        return None

    async def _validate_token(self, token: str, request: web.Request) -> TokenInfo | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None
        # https://datatracker.ietf.org/doc/html/rfc9068#section-4
        if self._typ is not None and header.get("typ") not in self._typ:
            return None
        try:
            key = await self._signing_key(token)
        except jwt.InvalidTokenError:
            return None
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._resource,  # RFC 8707 audience binding
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            logger.debug("Rejected JWT access token: %s", exc)
            return None

        scope = claims.get("scope")
        subject = claims.get("sub")
        return TokenInfo(
            scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
            subject=subject if isinstance(subject, str) else None,
            claims=claims,
        )
