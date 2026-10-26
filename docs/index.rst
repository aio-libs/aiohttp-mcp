====================
:brand:`aiohttp-mcp`
====================

A framework to easily make production-grade
`MCP <https://modelcontextprotocol.io/>`_ servers for AI agents, built on
:mod:`aiohttp <aiohttp.web>`.

Installation
============

.. code-block:: bash

   pip install aiohttp-mcp

Usage
=====

.. literalinclude:: code/index.py
   :pyobject: minimal_setup
   :lines: 2-
   :dedent:

This serves the MCP endpoint at ``/mcp``. It can be added to any existing
aiohttp web application, or used as a standalone one as above.

Note that it is especially recommended to enable handler cancellation for MCP servers,
but you should read the documentation first to understand the impact it has on your
handlers: https://docs.aiohttp.org/en/stable/web_advanced.html#web-handler-cancellation

Remote servers
==============

A server exposed on the internet needs two additions: the domain added to
``allowed_origins``, and an authorization configuration. MCP uses :brand:`OAuth` 2.1, and
:mod:`aiohttp_mcp` ships everything needed for it out of the box (including an
authorization server) so a complete, working deployment is:

.. literalinclude:: code/index.py
   :pyobject: remote_setup
   :lines: 2-
   :dedent:

See :doc:`authorization` for how the flow works, integrating with your
application's existing login, changing the consent page, and using an
external authorization server.

Table of Contents
=================

.. toctree::
   :name: mastertoc
   :maxdepth: 2

   tools
   prompts
   resources
   authorization
   elicitation
   api
