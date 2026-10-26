=====
Tools
=====

`Tools <https://modelcontextprotocol.io/specification/2026-07-28/server/tools>`_
are functions the language model can discover and invoke (e.g. querying a
database, performing a computation etc.). Passing a
:class:`~aiohttp_mcp.ToolRegistry` to :func:`aiohttp_mcp.setup` enables the
``tools`` capability:

.. literalinclude:: code/tools.py
   :pyobject: tools_setup
   :lines: 2-
   :dedent:

A tool is an ``async`` function. The name, title and description come from the function
name and docstring (with :class:`.Tool` allowing overrides). Tool arguments
are fully typed; the client will see a schema derived from the signature's type hints,
and arguments are validated against it before the function executes.

An optional first parameter annotated :class:`.MCPRequest` receives the request object,
and :doc:`elicitation <elicitation>` is also available in tools.

Results
=======

The return annotation defines the result type:

* :class:`str` - Unstructured text result.
* :data:`None` - Empty result.
* :class:`.Content` (or a sequence of) - Unstructured result of any standard content
  blocks.
* :data:`~typing.Any`, :class:`object` or no annotation - Dynamic result.
* :class:`.ToolResult` - A raw result that can be used to combine structured and
  unstructured results. For example, structured values could be returned to the model
  while an :class:`.ImageContent` could be returned with the ``user`` hint to display
  the values as a chart to the user. Use the type parameter (:code:`ToolResult[Data]`)
  for the structured data part and the format will be advertised to the client as the
  output schema.
* Any other JSON-like type - A structured result. The format will be advertised to the
  client as the output schema. A :class:`~typing.TypedDict` would be the most common
  option to define a structured JSON object.

For best results, handlers should prefer the explicit structured types (the last 2
options) where possible.

Tool errors
===========

Raise :class:`~aiohttp_mcp.ToolError` to signal an error to the model. The error
message should be actionable, so the model can correct itself (e.g. by fixing the input
or trying again at a later time).

.. literalinclude:: code/tools.py
   :pyobject: book_flight

Tools that need state
=====================

MCP is stateless, so tools that need state carried between calls will need to return
an explicit handle it can use to retrieve that state. For example, one tool can
create a new state and the model can then pass the handle returned from that call to
the next tool:

.. literalinclude:: code/tools.py
   :pyobject: stateful_tools
   :lines: 2-
   :dedent:

Returning the handle as structured content makes it easy for the model (and
the application) to carry it forward reliably. When designing handles:

* On an authorized server, a handle is a name, not a capability: bind the
  state to the caller (e.g. :attr:`.TokenInfo.subject` from
  :attr:`.MCPRequest.auth`) and check it on every call. Without
  authorization the handle *is* the bearer token, so generate it with
  sufficient entropy and give it a bounded lifetime, as above.
* Keep handles opaque, as identifiers that encode internal structure invite
  guessing.
* State the retention policy in the creation tool's description, as above,
  so the model can see it when deciding to create state.
* Report an expired or unknown handle as a :class:`~.ToolError`
  that says how to recover, so the model can create a new one.

Hints and authorization
=======================

:class:`.Tool` has hints (:attr:`~.Tool.read_only`, :attr:`~.Tool.destructive`,
:attr:`~.Tool.idempotent`, :attr:`~.Tool.open_world`) to help clients decide when to
ask the user for confirmation, and :attr:`~.Tool.scopes` to restrict a tool to
authorized callers:

.. literalinclude:: code/tools.py
   :pyobject: tool_hints
   :lines: 2-
   :dedent:

Parameter headers
=================

Annotating a :class:`str`, :class:`int`, or :class:`bool` parameter with :class:`.Header`
mirrors its value into an ``Mcp-Param-{name}`` HTTP header on each call, so
network intermediaries (proxies and gateways) can route on it without parsing the body:

.. literalinclude:: code/tools.py
   :pyobject: param_headers
   :lines: 2-
   :dedent:

.. warning::

   Do not use :class:`.Header` with any sensitive data such as PII, as values are
   visible to intermediaries.
