=============
API Reference
=============

.. module:: aiohttp_mcp

Setup
=====

.. autofunction:: aiohttp_mcp.setup

.. autoclass:: MCPServer()

.. autodata:: PROTOCOL_VERSIONS


Tools
=====

.. autoclass:: aiohttp_mcp.ToolRegistry

.. autoclass:: aiohttp_mcp.Tool

.. autoclass:: aiohttp_mcp.ToolResult

.. autoclass:: aiohttp_mcp.RateLimiter

.. autoclass:: aiohttp_mcp.ToolError

.. autoclass:: aiohttp_mcp.Header

.. autotype:: ToolHandler

   A tool function. Arguments arrive as keyword arguments, validated against
   the schema derived from the signature's type hints, with an optional
   first parameter annotated :class:`MCPRequest` receiving the current request.

Prompts
=======

.. autoclass:: aiohttp_mcp.PromptRegistry

.. autoclass:: aiohttp_mcp.Prompt

.. autoclass:: aiohttp_mcp.Message

.. autoclass:: aiohttp_mcp.Doc

.. autotype:: PromptHandler

   A prompt template function. Prompt arguments arrive as keyword arguments;
   an optional first parameter annotated :class:`MCPRequest` receives the
   in-flight request.

.. autotype:: PromptResult

   What a prompt function may return: a plain string (one user text message),
   or a sequence of :ref:`content blocks <aiohttp-mcp-content-types>` and
   :class:`Message` objects. Bare content blocks become ``user`` messages.

.. _aiohttp-mcp-content-types:

Content
=======

The content blocks that make up unstructured results: returned from tools and
prompts. Simple handlers can return a single :class:`str`, which is converted to a
:class:`~aiohttp_mcp.TextContent`, others must return a sequence of the classes
below, like::

    return [ImageContent.from_file(Path("foo/bar.jpeg")), TextContent("Check the image")]

In prompt messages, bare content blocks become ``user`` messages; wrap a
block in a :class:`Message` for other roles.

.. autoclass:: aiohttp_mcp.Content()

.. autoclass:: aiohttp_mcp.TextContent

.. autoclass:: aiohttp_mcp.ImageContent

.. autoclass:: aiohttp_mcp.AudioContent

.. autoclass:: aiohttp_mcp.ResourceLink

.. autoclass:: aiohttp_mcp.EmbeddedResource()


Resources
=========

.. autoclass:: aiohttp_mcp.ResourceRegistry

.. autoclass:: aiohttp_mcp.Resource

.. autoclass:: aiohttp_mcp.ResourceContents

.. autotype:: ResourceHandler

   A resource function. URI template variables arrive as keyword arguments;
   an optional first parameter annotated :class:`MCPRequest` receives the
   in-flight request.

.. autotype:: ResourceResult


Elicitation
===========

.. autoclass:: aiohttp_mcp.Elicitation()

.. autoclass:: aiohttp_mcp.FormElicitation

.. autoclass:: aiohttp_mcp.URLElicitation

.. autoclass:: aiohttp_mcp.SecureFormElicitation

.. autotype:: RenderForm

   Replaces the hosted secure-form page; see :func:`~aiohttp_mcp.setup`'s
   ``render_form``. The returned page must satisfy the contract documented
   on :class:`FormContext`.

.. autoclass:: aiohttp_mcp.FormContext()

.. autoclass:: aiohttp_mcp.ElicitResult()

.. autoclass:: aiohttp_mcp.InputRequired

.. _aiohttp-mcp-field-schemas:

Form fields
+++++++++++

.. autoclass:: aiohttp_mcp.FieldSchema()

.. autoclass:: aiohttp_mcp.StringSchema

.. autoclass:: aiohttp_mcp.IntegerSchema

.. autoclass:: aiohttp_mcp.NumberSchema

.. autoclass:: aiohttp_mcp.BooleanSchema

.. autoclass:: aiohttp_mcp.EnumSchema

.. autoclass:: aiohttp_mcp.MultiSelectEnumSchema

Common types
============

.. autoclass:: aiohttp_mcp.Icon

.. autoclass:: aiohttp_mcp.MCPRequest()

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
