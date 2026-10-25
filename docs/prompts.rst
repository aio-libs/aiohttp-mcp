=======
Prompts
=======

`Prompts <https://modelcontextprotocol.io/specification/2026-07-28/server/prompts>`_
are user-controlled (typically invoked by a slash command) templates a server offers for
interacting with a language model. Passing a :class:`~aiohttp_mcp.PromptRegistry` to
:func:`aiohttp_mcp.setup` enables the ``prompts`` capability:

.. literalinclude:: code/prompts.py
   :pyobject: prompts_setup
   :lines: 2-
   :dedent:

A prompt is a plain ``async`` function; its advertised definition is derived from
the signature:

* The function name is the prompt's identifier.
* The docstring's summary line is its title, and the rest of the docstring
  its description (:pep:`257`).
* Parameters become the prompt's arguments, which must be :class:`str` values.
* Parameters without a default are required.
* :data:`~typing.Annotated` metadata marked with :class:`~aiohttp_mcp.Doc`
  becomes the argument description.

Use the :class:`~aiohttp_mcp.Prompt` descriptor to override the
derived metadata (``name``, ``title``, ``description``, ``icons``).

A prompt may also declare a first parameter annotated
:class:`~aiohttp_mcp.MCPRequest` to receive the in-flight request. This gives
access to the underlying :class:`~aiohttp.web.Request`, the client's
identity, and the validated token of an authorized caller. It is not
advertised as a prompt argument.

Results
=======

Returning a :class:`str` produces a single user message with text content.
For multi-modal content, return a sequence of the content abstractions:

.. literalinclude:: code/prompts.py
   :pyobject: multi_modal
   :lines: 2-
   :dedent:

See :ref:`message content reference <aiohttp-mcp-content-types>` for all supported
types and their available arguments.

Every content block also accepts the specification's annotation fields as
keyword arguments, hinting to the client who the content is for and how
important it is:

.. literalinclude:: code/prompts.py
   :pyobject: style_guide

For multi-message templates (e.g. worked examples with assistant turns),
wrap content in :class:`~aiohttp_mcp.Message` to set the role:

.. literalinclude:: code/prompts.py
   :pyobject: plan_feature

Authorization
=============

When :doc:`authorization <authorization>` is configured, a prompt
can be restricted to callers holding specific scopes. Restricted prompts are
omitted from prompt listing for clients lacking the scopes:

.. literalinclude:: code/prompts.py
   :pyobject: scoped_prompt
   :lines: 2-
   :dedent:
