.. _aiohttp-mcp-api-reference:

=============
API Reference
=============

.. module:: aiohttp_mcp

Setup
=====

.. autofunction:: aiohttp_mcp.setup

.. autoclass:: MCPServer()

.. autodata:: PROTOCOL_VERSIONS

Authorization
=============

.. autoclass:: aiohttp_mcp.Authorization

.. autoclass:: aiohttp_mcp.IntrospectionAuthorization

.. autoclass:: aiohttp_mcp.jwt.JWTAuthorization

.. autotype:: TokenValidator

   Validates a Bearer token, returning ``None`` if it must be rejected.

   The callable receives the token and the request. It is responsible for everything
   that requires knowledge of the token format or the authorization server:
   signature verification or introspection, expiry, and that the token's audience
   is this server's canonical ``resource`` URI (:rfc:`8707`). Tokens issued for any
   other resource MUST be rejected by returning ``None``.

.. autoclass:: aiohttp_mcp.TokenInfo()

.. autodata:: TOKEN_INFO_KEY
   :no-value:

Local Authorization
+++++++++++++++++++

A bundled authorization server for simple deployments.

.. autoclass:: aiohttp_mcp.LocalAuthorization

.. autotype:: ScopeMatcher

   Decides whether one required scope is satisfied by the scopes a token carries.

   This is how deployment-defined scope hierarchies are allowed. As there is no
   prescribed way to express hierarchies, it is the responsibility of a custom function
   to implement it for a given implementation. The default is exact scope
   matching (no hierarchy).

.. autotype:: CheckCredentials

   Verifies a username/password pair presented at the built-in login page.

.. autotype:: AuthenticateRequest

   Returns the authenticated subject for a request (e.g. the logged in user ID),
   or ``None`` when not logged in.

   When using :mod:`aiohttp_security` this can simply be
   :func:`~aiohttp_security.authorized_userid`.

.. autotype:: RenderConsent

   Renders the consent page; see :class:`ConsentContext` for the contract the
   returned page must satisfy.

.. autoclass:: aiohttp_mcp.ConsentContext()
