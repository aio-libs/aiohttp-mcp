=========
Resources
=========

`Resources <https://modelcontextprotocol.io/specification/2026-07-28/server/resources>`_
expose data that a client can use (e.g. files, database records, application state etc.)
each identified by a URI. Passing a :class:`~aiohttp_mcp.ResourceRegistry` to
:func:`aiohttp_mcp.setup` enables the ``resources`` capability:

.. literalinclude:: code/resources.py
   :pyobject: resources_setup
   :lines: 2-
   :dedent:

A resource is a plain ``async`` function paired with a URI. As with
:doc:`prompts <prompts>`, the advertised metadata is derived from the function.

When the URI contains :rfc:`6570` template expressions, the resource is a
*template*: its variables become the function's parameters, filled from the
URI a client reads. Only the two most common expansions are supported;
``{var}`` matches a single path segment and is percent-decoded, while ``{+var}``
matches across ``/`` and is passed through as-is. An optional first
parameter annotated :class:`~aiohttp_mcp.MCPRequest` receives the in-flight
request.

Handlers can return a :class:`str` (text contents), :class:`bytes` (binary contents),
a :class:`.ResourceContents` for explicit URIs and MIME types, or
a sequence of :class:`.ResourceContents` to return several at once, such as the
files of a directory resource.

.. note::

   The ``https://`` scheme should only be used for resources which are publicly
   available on the web, where the client may choose to fetch them directly without
   the MCP server.

   The ``file://`` scheme should be used when representing resources that behave
   similarly to a filesystem. Consider using the ``inode/*``
   `MIME types <https://specifications.freedesktop.org/shared-mime-info/0.14/ar01s02.html#id-1.3.14>`__.

   The ``git://`` scheme can be used for :brand:`Git` version control operation.

   Custom schemes must adhere to :rfc:`3986#section-3.1`.

Update notifications
====================

Clients can watch specific resources for updates. When the application changes
the data behind a resource, calling :meth:`.MCPServer.resource_updated` sends
a notification to every client watching that URI:

.. literalinclude:: code/resources.py
   :pyobject: resource_updates
   :lines: 2-
   :dedent:
