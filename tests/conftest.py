from typing import Any

import pytest
from _helpers import MakeClient
from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_aiohttp import AiohttpClient

import aiohttp_mcp


@pytest.fixture
def make_client(aiohttp_client: AiohttpClient) -> MakeClient:
    """Create a ``TestClient`` for an app with one MCP endpoint on it."""

    async def maker(**kwargs: Any) -> TestClient[web.Request, web.Application]:
        kwargs.setdefault("name", "test-server")
        kwargs.setdefault("version", "1.2.3")
        app = web.Application()
        aiohttp_mcp.setup(app, **kwargs)
        return await aiohttp_client(app)

    return maker
