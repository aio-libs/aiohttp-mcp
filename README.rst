aiohttp-mcp
===========

A framework to easily make production-grade `MCP <https://modelcontextprotocol.io/>`_
servers for AI agents.

Installation
------------

.. code-block:: bash

    pip install aiohttp-mcp

Usage
-----

.. code-block:: python

    import aiohttp_mcp
    from aiohttp import web

    app = web.Application()
    aiohttp_mcp.setup(app)
    web.run_app(app, handler_cancellation=True)

This serves the MCP endpoint at ``/mcp``. This can be added to any existing
aiohttp web application, or used as a standalone one as above.

Note that it is especially recommended to enable handler cancellation for MCP servers,
but you should read the documentation first to understand the impact it has on your
handlers: https://docs.aiohttp.org/en/stable/web_advanced.html#web-handler-cancellation
