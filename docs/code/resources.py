import aiohttp_mcp
from aiohttp import web


def resources_setup() -> None:
    import aiohttp_mcp
    from aiohttp import web

    async def readme() -> str:
        """Project README.

        The entry point to the documentation.
        """
        return "# My project\n..."

    async def report(month: str) -> aiohttp_mcp.ResourceContents:
        """Monthly report.

        The activity report for one month, e.g. ``reports://2026-10``.
        """
        return aiohttp_mcp.ResourceContents(text=f"Report for {month}: ...")

    app = web.Application()
    aiohttp_mcp.setup(
        app,
        resources=aiohttp_mcp.ResourceRegistry(
            aiohttp_mcp.Resource(readme, "docs://readme", mime_type="text/markdown"),
            aiohttp_mcp.Resource(report, "reports://{month}", mime_type="text/plain"),
        ),
    )
    web.run_app(app, handler_cancellation=True)


def resource_updates() -> None:
    motd_key = web.AppKey("motd", str)
    mcp_key = web.AppKey("mcp", aiohttp_mcp.MCPServer)

    async def set_motd(request: web.Request) -> web.Response:
        request.app[motd_key] = await request.text()
        # Notify subscribed clients.
        request.app[mcp_key].resource_updated("app://motd")
        return web.Response()

    async def motd(request: aiohttp_mcp.MCPRequest) -> str:
        """Message of the day."""
        return request.http.app[motd_key]

    app = web.Application()
    app[motd_key] = "Hello!"
    app[mcp_key] = aiohttp_mcp.setup(
        app,
        resources=aiohttp_mcp.ResourceRegistry(
            aiohttp_mcp.Resource(motd, "app://motd", mime_type="text/plain")
        ),
    )
    app.router.add_put("/motd", set_motd)
    web.run_app(app, handler_cancellation=True)
