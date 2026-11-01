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

    async def get_weather(location: str) -> str:
        """Get current weather information for a location."""
        return f"Sunny in {location}."

    app = web.Application()
    aiohttp_mcp.setup(app, tools=aiohttp_mcp.ToolRegistry(get_weather))
    web.run_app(app, handler_cancellation=True)

This serves the MCP endpoint at ``/mcp``. This can be added to any existing
aiohttp web application, or used as a standalone one as above.

Note that it is especially recommended to enable handler cancellation for MCP servers,
but you should read the documentation first to understand the impact it has on your
handlers: https://docs.aiohttp.org/en/stable/web_advanced.html#web-handler-cancellation

Remote servers and auth
-----------------------

A server exposed on the internet needs two additions: the domain added to
``allowed_origins``, and an authorization configuration. MCP uses OAuth 2.1, and
aiohttp-mcp ships everything needed for it out of the box (including an
authorization server) so a complete, working deployment is:

.. code-block:: python

    import os
    import secrets

    import aiohttp_mcp
    from aiohttp import web

    async def check_credentials(username: str, password: str, request: web.Request) -> bool:
        return username == "admin" and secrets.compare_digest(
            password, os.environ["MCP_PASSWORD"]
        )

    app = web.Application()
    aiohttp_mcp.setup(
        app,
        allowed_origins=("mcp.server.example",),
        authorization=aiohttp_mcp.LocalAuthorization(
            check_credentials,
            resource="https://mcp.server.example",
            secret=bytes.fromhex(os.environ["MCP_TOKEN_KEY"]),
        ),
    )
    web.run_app(app, handler_cancellation=True)

``LocalAuthorization`` makes the application its own OAuth 2.1 authorization
server, with the credential check as the only deployment-specific part. Larger
deployments with multiple users should use ``Authorization`` to connect to an
established OAuth server instead.
