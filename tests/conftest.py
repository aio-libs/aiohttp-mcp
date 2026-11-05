from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_aiohttp import AiohttpClient

import aiohttp_mcp
from _helpers import MakeClient


@pytest.fixture
def mcp_servers() -> list[aiohttp_mcp.MCPServer]:
    """The servers created by ``make_client``, in creation order."""
    return []


@pytest.fixture
def make_client(
    aiohttp_client: AiohttpClient, mcp_servers: list[aiohttp_mcp.MCPServer]
) -> MakeClient:
    """Create a ``TestClient`` for an app with one MCP endpoint on it."""

    async def maker(**kwargs: Any) -> TestClient[web.Request, web.Application]:
        kwargs.setdefault("name", "test-server")
        kwargs.setdefault("version", "1.2.3")
        app = web.Application()
        mcp_servers.append(aiohttp_mcp.setup(app, **kwargs))
        return await aiohttp_client(app)

    return maker
