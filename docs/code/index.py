def minimal_setup() -> None:
    import aiohttp_mcp_server as mcp
    from aiohttp import web

    async def get_weather(location: str) -> str:
        """Get current weather information for a location."""
        return f"Sunny in {location}."

    app = web.Application()
    mcp.setup(app, tools=mcp.ToolRegistry(get_weather))
    web.run_app(app, handler_cancellation=True)


def remote_setup() -> None:
    import os
    import secrets

    import aiohttp_mcp_server as mcp
    from aiohttp import web

    async def check_credentials(username: str, password: str, request: web.Request) -> bool:
        return username == "admin" and secrets.compare_digest(
            password, os.environ["MCP_PASSWORD"]
        )

    app = web.Application()
    mcp.setup(
        app,
        allowed_origins=("mcp.server.example",),
        authorization=mcp.LocalAuthorization(
            check_credentials,
            resource="https://mcp.server.example",
            secret=bytes.fromhex(os.environ["MCP_TOKEN_KEY"]),
        ),
    )
    web.run_app(app, handler_cancellation=True)
