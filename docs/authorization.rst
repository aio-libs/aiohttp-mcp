=============
Authorization
=============

MCP uses `OAuth 2.1
<https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>`_:
the MCP server acts as a *resource server*, accepting Bearer tokens issued by
an *authorization server*. :mod:`aiohttp_mcp` ships both halves:

- :class:`~aiohttp_mcp.Authorization` implements the resource-server side:
  it serves the Protected Resource Metadata document (:rfc:`9728`) that
  clients use to discover the authorization server, challenges requests with
  ``WWW-Authenticate`` headers (including scope guidance), and rejects
  malformed credentials. Token validation is delegated to a
  :data:`~aiohttp_mcp.TokenValidator` callable.
- :class:`~aiohttp_mcp.LocalAuthorization` extends it into a complete,
  self-contained setup: the same application also acts as the authorization
  server, hosting the login and consent pages and issuing the tokens. This is
  best used for small single-user deployments where an authorization server is
  not already available.

.. contents::
   :local:
   :depth: 1

Self-contained authorization
============================

.. literalinclude:: code/authorization.py
   :pyobject: self_contained
   :lines: 2-
   :dedent:

.. note::

   :attr:`Request.app <aiohttp.web.Request.app>` will be the
   :class:`.LocalAuthorization`'s sub-application. Use
   :attr:`Request.config_dict <aiohttp.web.Request.config_dict>` to access anything
   stored on the parent app.

Here we have used a callback to verify the username and password from an environment
variable. We have also loaded a signing key secret from another environment variable and
set the domain name that this will be available under.

This will result in a complete authorization flow with the user being directed to a
login page and then a page where they can approve the connection to their client.
While this implementation follows good security practices, for larger or more important
deployments it is highly recommended to use an established authorization server.

Using the app's existing login
++++++++++++++++++++++++++++++

If the MCP server is part of a website with its own accounts, users should
log in through the site's normal login page and only see the approval page.
Provide ``authenticate_request`` to identify the existing session, and
``login_url`` to send signed-out users to your login page.

.. note::

   The login page must redirect back to the approval page by inspecting the
   ``redirect`` query parameter (the parameter name can be changed with
   ``login_redirect_param``).

For an application already using :mod:`aiohttp_security`, simply use
:func:`~aiohttp_security.authorized_userid` for ``authenticate_request``:

.. literalinclude:: code/authorization.py
   :pyobject: app_login
   :lines: 2-
   :dedent:

Theme the consent page
++++++++++++++++++++++

The consent (approval) page is a minimal HTML form; replace it by passing
``render_consent=`. The page must POST back to the same URL with the fields from
:attr:`aiohttp_mcp.ConsentContext.fields`
(:attr:`~aiohttp_mcp.ConsentContext.hidden_fields_html` renders them
already escaped, including the anti-forgery token) plus an ``action`` of
``"approve"`` or ``"deny"``. Note that
:attr:`~aiohttp_mcp.ConsentContext.client_name` is supplied by the client, so
it must be HTML-escaped:

.. literalinclude:: code/authorization.py
   :pyobject: render_consent

Using an external authorization server
======================================

If your tokens are issued by an existing authorization server
(such as :brand:`Keycloak` or :brand:`Auth0`), we just need to setup authorization
to validate the tokens from that server. We provide 2 helpers to validate common
token schemes, otherwise a custom function will need to be used.

**JWT access tokens** (:rfc:`9068`) can be validated with the
:class:`aiohttp_mcp.jwt.JWTAuthorization` helper. This requires the ``jwt`` extra
to be installed (``pip install aiohttp-mcp[jwt]``):

.. literalinclude:: code/authorization.py
   :pyobject: jwt_authorization
   :lines: 2-
   :dedent:

**Opaque tokens** can be validated by asking the authorization server per
request via Token Introspection (:rfc:`7662`). For this case, the
:class:`~aiohttp_mcp.IntrospectionAuthorization` helper exists.

.. literalinclude:: code/authorization.py
   :pyobject: introspection_authorization
   :lines: 2-
   :dedent:

If neither of these can be used with your chosen server, supply a custom
:data:`~aiohttp_mcp.TokenValidator` function to :class:`~aiohttp_mcp.Authorization`. The
implementation must verify the token, check that it is still valid, and check its
audience is the canonical ``resource`` URI (:rfc:`8707`), returning ``None`` to reject:

.. literalinclude:: code/authorization.py
   :pyobject: external_authorization_server
   :lines: 2-
   :dedent:

Serving as a sub-application
============================

When using this library on a sub-application, we need to make a small change.
The well-known discovery documents (:rfc:`9728`/:rfc:`8414`) must be placed at
a fixed location at the origin root. Registering them normally on an application
mounted with :meth:`~aiohttp.web.Application.add_subapp` would serve them under
the sub-application's prefix.

First, include the sub-application's public mount prefix in ``resource``
(``"https://mcp.server.example/api"`` below). Second, use
``well_known_routes=False`` to skip adding the well-known endpoints to the
sub-application and call
:func:`~aiohttp_mcp.LocalAuthorization.register_well_known` with the root
application instead.

.. literalinclude:: code/authorization.py
   :pyobject: sub_application

Accessing the token from handlers
=================================

The validated :class:`~aiohttp_mcp.TokenInfo` is stored on the request under
:data:`~aiohttp_mcp.TOKEN_INFO_KEY`, where the rest of the application can
read the subject and scopes:

.. literalinclude:: code/authorization.py
   :pyobject: token_handlers
   :lines: 2-
   :dedent:
