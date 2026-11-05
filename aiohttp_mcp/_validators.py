"""Standard token validation: OAuth 2.0 Token Introspection (RFC 7662)."""

import heapq
import time
from collections.abc import AsyncIterator, Sequence

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    encode_basic_auth,
    hdrs,
    web,
)
from yarl import URL

from ._auth import Authorization, ScopeMatcher, TokenInfo, _secure_url


async def _discover_metadata(http: ClientSession, issuer: str) -> dict[str, object]:
    """Fetch and validate the authorization server's metadata for ``issuer``."""
    issuer_url = URL(issuer)
    path = issuer_url.path if issuer_url.path != "/" else ""
    candidates = [
        str(issuer_url.with_path(f"/.well-known/oauth-authorization-server{path}")),
        str(issuer_url.with_path(f"/.well-known/openid-configuration{path}")),
    ]
    if path:
        candidates.append(
            str(issuer_url.with_path(f"{path}/.well-known/openid-configuration"))
        )
    error: Exception | None = None
    for url in candidates:
        try:
            async with http.get(url) as resp:
                resp.raise_for_status()
                metadata = await resp.json()
        except ClientError as exc:
            error = exc
            continue
        # https://datatracker.ietf.org/doc/html/rfc8414#section-3.3
        if not isinstance(metadata, dict) or metadata.get("issuer") != issuer:
            raise ValueError(f"Metadata at {url} is not for issuer {issuer}")
        return metadata
    raise ValueError(
        f"Could not discover authorization server metadata for {issuer}"
    ) from error


def _aud_contains(aud: object, resource: str) -> bool:
    """Audience check (RFC 8707): ``aud`` may be a string or an array."""
    if isinstance(aud, str):
        return aud == resource
    if isinstance(aud, list):
        return resource in aud
    return False


class IntrospectionAuthorization(Authorization):
    """Authorization validating opaque tokens via :rfc:`7662` introspection.

    Exactly one of ``introspection_url`` and ``issuer`` must be provided.

    :param introspection_url: The authorization server's introspection
        endpoint (:rfc:`7662`), when known directly. Must be HTTPS (plain
        ``http`` only on ``localhost``, for local development). Requires
        ``auth_servers``.
    :param issuer: The authorization server's issuer identifier. The
        introspection endpoint is discovered from its :rfc:`8414` metadata
        at startup, and the issuer doubles as the default ``auth_servers``
        entry.
    :param client_id: This server's client identifier at the authorization
        server, used to authenticate introspection requests (HTTP Basic).
    :param client_secret: The client secret paired with ``client_id``.
    :param resource: Base URL this MCP server is reachable at, see
        :class:`Authorization`. Tokens whose audience (:rfc:`8707`) is not
        this server's resource identifier are rejected.
    :param auth_servers: See :class:`Authorization`; defaults to
        ``(issuer,)`` when ``issuer`` is given.
    :param cache_ttl: How long in seconds to cache a successful
        introspection result, bounding the extra latency and load of one
        introspection request per MCP call. ``0`` (the default) disables
        caching: revocation is seen immediately, at the cost of one
        round-trip per request.
    :param timeout: Total timeout in seconds for introspection and metadata
        requests.

    See :class:`Authorization` for other parameters.
    """

    def __init__(
        self,
        introspection_url: str | None = None,
        *,
        issuer: str | None = None,
        client_id: str,
        client_secret: str,
        resource: str,
        auth_servers: Sequence[str] | None = None,
        cache_ttl: float = 0.0,
        timeout: float = 10.0,
        default_scopes: Sequence[str] = (),
        scope_matcher: ScopeMatcher | None = None,
        resource_name: str | None = None,
        resource_documentation: str | None = None,
    ) -> None:
        if cache_ttl < 0:
            raise ValueError("cache_ttl must be >= 0")
        if (introspection_url is None) == (issuer is None):
            raise ValueError("provide exactly one of introspection_url or issuer")
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations#communication-security
        if introspection_url is not None and not _secure_url(URL(introspection_url)):
            raise ValueError(f"introspection_url must be an HTTPS URL: {introspection_url!r}")
        if issuer is not None and not _secure_url(URL(issuer)):
            raise ValueError(f"issuer must be an HTTPS URL: {issuer!r}")
        if auth_servers is None:
            if issuer is None:
                raise ValueError(
                    "auth_servers is required with an explicit introspection_url"
                )
            auth_servers = (issuer,)
        super().__init__(
            self._validate_token,
            resource=resource,
            auth_servers=auth_servers,
            default_scopes=default_scopes,
            scope_matcher=scope_matcher,
            resource_name=resource_name,
            resource_documentation=resource_documentation,
        )
        self._issuer = issuer
        self._introspection_url = introspection_url
        self._authorization = encode_basic_auth(client_id, client_secret)
        self._cache_ttl = cache_ttl
        self._timeout = ClientTimeout(total=timeout)
        self._cache: dict[str, tuple[float, TokenInfo]] = {}
        self._cache_expiries: list[tuple[float, str]] = []
        self._http: ClientSession | None = None

    def _register_routes(self, app: web.Application) -> None:
        super()._register_routes(app)
        app.cleanup_ctx.append(self._client_session)

    async def _client_session(self, app: web.Application) -> AsyncIterator[None]:
        self._http = session = ClientSession(timeout=self._timeout)
        yield
        await session.close()

    async def _introspect(self, token: str) -> dict[str, object]:
        assert self._http is not None
        if self._introspection_url is None:
            assert self._issuer is not None
            metadata = await _discover_metadata(self._http, self._issuer)
            endpoint = metadata.get("introspection_endpoint")
            if not isinstance(endpoint, str) or not _secure_url(URL(endpoint)):
                raise ValueError(
                    f"Metadata for {self._issuer} does not declare an HTTPS "
                    "introspection_endpoint"
                )
            self._introspection_url = endpoint

        # https://datatracker.ietf.org/doc/html/rfc7662#section-2.1
        async with self._http.post(
            self._introspection_url,
            data={"token": token, "token_type_hint": "access_token"},
            headers={hdrs.AUTHORIZATION: self._authorization},
        ) as resp:
            resp.raise_for_status()
            claims = await resp.json()
        if not isinstance(claims, dict):
            raise TypeError("Invalid introspection response")
        return claims

    async def _validate_token(self, token: str, request: web.Request) -> TokenInfo | None:
        now = time.time()
        cached = self._cache.get(token)
        if cached is not None and cached[0] > now:
            return cached[1]

        claims = await self._introspect(token)
        # https://datatracker.ietf.org/doc/html/rfc7662#section-2.2
        if claims.get("active") is not True:
            return None
        expiry = claims.get("exp")
        if not isinstance(expiry, (int, float)) or expiry <= now:
            return None
        # RFC 8707 audience binding: reject tokens issued for other resources.
        if not _aud_contains(claims.get("aud"), self._resource):
            return None

        scope = claims.get("scope")
        subject = claims.get("sub")
        info = TokenInfo(
            scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
            subject=subject if isinstance(subject, str) else None,
            claims=claims,
        )
        if self._cache_ttl > 0:
            while self._cache_expiries and self._cache_expiries[0][0] <= now:
                _, expired_token = heapq.heappop(self._cache_expiries)
                entry = self._cache.get(expired_token)
                if entry is not None and entry[0] <= now:
                    del self._cache[expired_token]
            cache_expiry = min(now + self._cache_ttl, expiry)
            self._cache[token] = (cache_expiry, info)
            heapq.heappush(self._cache_expiries, (cache_expiry, token))
        return info
