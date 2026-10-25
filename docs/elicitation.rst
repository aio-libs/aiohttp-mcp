===========
Elicitation
===========

`Elicitation <https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation>`_
lets a handler ask the user for additional input. It can be used in
:doc:`prompts <prompts>`, :doc:`resources <resources>` and tasks.
Elicitation is performed with the :meth:`.MCPRequest.elicit` method, which will
return the user's input. There are 2 forms that can be used.

Form elicitation
================

Using :class:`.FormElicitation` it is possible to send a simple form
that the user can fill in through their client.

.. warning::

   Do not use form mode for requesting sensitive details such as credentials.
   See :ref:`aiohttp-mcp-url-elicitation`.

.. literalinclude:: code/elicitation.py
   :pyobject: form_elicitation

See the :ref:`field schemas <aiohttp-mcp-field-schemas>` for the available
field types that can be requested from a user.

Note that while the code can be read as if :meth:`.MCPRequest.elicit` is pausing
to retrieve the user input, it is important to note that handlers must be idempotent.
In reality, the method actually raises the first time a user reaches it and requests
the input; then the client resubmits the same request along with the user input and
on the second time the same method will return the validated input. This can also
execute over multiple attempts, accumulating the required input across handler calls.

.. _aiohttp-mcp-url-elicitation:

URL Elicitation
===============

Using :class:`.URLElicitation` it is possible to request sensitive data without it
passing through the client, or to request input that cannot fit within the form schema
types.

.. warning::

   There are a few important security details you must adhere to when using this:

    - Do not carry user information in the URL.
    - Do not grant access via the URL itself without any user interaction.
    - The page the URL points to must verify the user's identity independently.
    - The page the URL points to must verify that identity is the same one that
      the request was originally created for.

   These are important to protect against 2 situations:

    - The URL being made available to third parties.
    - A malicious third party creating an elicitation request and then tricking
      another user to navigate to the provided URL to authorize access, potentially
      resulting in an account takeover.

.. literalinclude:: code/elicitation.py
   :pyobject: url_elicitation

``accept`` only means the user agreed to *open* the URL, while the interaction
itself happens in the browser, so the handler keeps raising
:exc:`.InputRequired` (state-only, with no new requests) until the signature
shows up. The page behind the URL is ordinary web-handler code; the examples
below show how these might be implemented.

Hosted secure forms
++++++++++++++++++++

For simple cases, the library can serve the URL itself, with no custom endpoint code
at all. :class:`.SecureFormElicitation` takes the same
:ref:`field schemas <aiohttp-mcp-field-schemas>` as form mode, but creates a page
served by this server which can be accessed by a single-use link:

.. literalinclude:: code/elicitation.py
   :pyobject: secure_form_elicitation

The library creates the link, verifies that the browser user opening it is the
one who made the MCP request, renders the fields, validates the submission,
and hands the values to the handler as an accepted result. The handler uses a similar
retry process in the above example while waiting for the user input.

When using :class:`.LocalAuthorization`, the login session is used automatically to
identify the browser user. For other cases, pass ``authenticate_request`` to
:func:`~aiohttp_mcp.setup` (with :mod:`aiohttp_security`, use
:func:`~aiohttp_security.authorized_userid`). Pending submissions are held in memory,
so a multi-worker deployment needs sticky routing for the form pages.

Same-site endpoint
++++++++++++++++++

If the MCP server is part of an existing website using :mod:`aiohttp_security`,
the implementation could look something like this:

.. literalinclude:: code/elicitation.py
   :pyobject: same_site_endpoint
   :lines: 2-
   :dedent:

The approval is recorded against the MCP caller's subject, and the endpoint
only accepts it from a browser session logged in as that same user; anyone
else opening the URL is refused.
On the retry the handler finds the approval and proceeds. Note that consent
in the client (``accept``) only means the user agreed to *open* the URL, so
the handler keeps raising :exc:`.InputRequired` until the approval actually
arrives.

This comparison requires the MCP authorization subject and the application's
user IDs to come from the same identifier space. The simplest arrangement is
:class:`.LocalAuthorization` reusing the application's login via
``authenticate_request=authorized_userid``, as shown in :doc:`authorization`.

Browser login via an external authorization server
++++++++++++++++++++++++++++++++++++++++++++++++++

When MCP tokens are issued by an external authorization server, the application has no
login of its own to lean on, yet we must still verify the user's identity.
Therefore we must authenticate the browser against the same server.

.. literalinclude:: code/elicitation.py
   :pyobject: external_as_login
   :lines: 2-
   :dedent:

The login endpoint runs a standard :brand:`OpenID Connect` authorization-code flow,
and the callback exchanges the code server-side and asks the ``userinfo`` endpoint who
logged in, and the resulting ``sub`` is kept in an encrypted session
cookie with :mod:`aiohttp_session`.

The ``delete_project`` and ``approve`` handlers are the same from the previous example;
only ``browser_subject`` added in ``approve`` changed, and the comparison against the
MCP token's subject is identical. A production version would also validate the ID
token's signature and claims rather than relying on the ``userinfo`` call
alone, and discover the endpoints from the server's metadata.

Integrating a third-party :brand:`OAuth` flow
+++++++++++++++++++++++++++++++++++++++++++++

URL elicitation is also how a server obtains credentials for *other*
services: it acts as an :brand:`OAuth` client of the third party, and sends
the user to authorize access. This is separate from MCP
authorization - the client's token for this server is unaffected, the
third-party tokens must never be sent to the MCP client, and the client's
MCP token must never be forwarded to the third party.

.. literalinclude:: code/elicitation.py
   :pyobject: oauth_elicitation
   :lines: 2-
   :dedent:

The elicitation URL is the server's own ``/connect/calendar`` page, not the
third party's authorization endpoint: the flow must not start until the
server knows who is making the browser request. That page binds the freshly
created :brand:`OAuth` ``state`` to the logged-in user, and the callback
stores the obtained tokens for that user only - after checking both the
``state`` binding and the session again. The elicitation's role ends there;
each retry simply checks whether tokens have arrived for the MCP caller's
subject.

For brevity the example omits the parts any real :brand:`OAuth` client
needs - PKCE, token refresh, and persistent storage for the token and state
tables (in-memory dictionaries do not survive restarts or span workers).
