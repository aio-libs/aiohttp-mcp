import secrets
import time
from typing import Annotated, TypedDict

import aiohttp_mcp
from aiohttp import web


def tools_setup() -> None:
    from typing import Annotated, Literal, NotRequired, TypedDict

    from aiohttp import web
    from aiohttp_mcp import Doc, ToolRegistry
    from aiohttp_mcp import setup as mcp_setup

    class Forecast(TypedDict):
        temperature: float
        conditions: str
        humidity: NotRequired[int]

    async def get_weather(
        location: Annotated[str, Doc("City name or zip code")],
        units: Literal["C", "F"] = "C",
    ) -> Forecast:
        """Weather Information Provider.

        Get current weather information for a location.
        """
        return {"temperature": 22.5 if units == "C" else 72.5, "conditions": "cloudy"}

    app = web.Application()
    mcp_setup(app, tools=ToolRegistry(get_weather))
    web.run_app(app, handler_cancellation=True)


async def book_flight(date: str) -> str:
    """Book a flight on the given date."""
    if date < "2026-11-01":
        # Actionable feedback the model can self-correct with.
        raise aiohttp_mcp.ToolError("Invalid departure date: must be in the future.")
    return f"Booked a flight on {date}."


def tool_hints() -> None:
    async def delete_index(name: str) -> str:
        """Delete a search index."""
        return f"Deleted {name}."

    tool = aiohttp_mcp.Tool(
        delete_index,
        destructive=True,
        idempotent=True,
        scopes=("search:admin",),
    )

    app = web.Application()
    aiohttp_mcp.setup(app, tools=aiohttp_mcp.ToolRegistry(tool))
    web.run_app(app, handler_cancellation=True)


def stateful_tools() -> None:
    class BasketHandle(TypedDict):
        basket_id: str

    baskets = web.AppKey("baskets", dict[str, tuple[float, list[str]]])

    async def create_basket(request: aiohttp_mcp.MCPRequest) -> BasketHandle:
        """Create a shopping basket.

        Baskets are valid for 300 seconds after creation.
        """
        basket_id = secrets.token_urlsafe()
        expiry = time.monotonic() + 300
        request.http.app[baskets][basket_id] = (expiry, [])
        return {"basket_id": basket_id}

    async def add_item(request: aiohttp_mcp.MCPRequest, basket_id: str, sku: str) -> str:
        """Add an item to a basket previously created with create_basket."""
        expiry, items = request.http.app[baskets].get(basket_id, (0, []))
        if expiry <= time.monotonic():
            raise aiohttp_mcp.ToolError(
                f"Unknown or expired basket {basket_id!r}; create a new one"
                " with create_basket."
            )
        items.append(sku)
        return f"Basket {basket_id} now holds {len(items)} item(s)."

    app = web.Application()
    app[baskets] = {}
    aiohttp_mcp.setup(app, tools=aiohttp_mcp.ToolRegistry(create_basket, add_item))
    web.run_app(app, handler_cancellation=True)


def param_headers() -> None:
    from aiohttp_mcp import Doc, Header

    async def execute_sql(
        region: Annotated[str, Doc("The region to execute in"), Header("Region")],
        query: str,
    ) -> str:
        """Execute SQL in the given region."""
        return "..."
