"""MCP server exposing OSU TravelPlanner environment tools to Claude Code.

Run with:
    uv run python examples/travelplanner-workspace/tools/mcp_server.py

Environment:
    TRAVELPLANNER_ROOT - path to the checked-out OSU TravelPlanner repo
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import anyio
from loguru import logger
from mcp.server.fastmcp import FastMCP

_travelplanner_root = os.environ.get("TRAVELPLANNER_ROOT")
if not _travelplanner_root:
    raise RuntimeError("TRAVELPLANNER_ROOT must be set")
_TRAVELPLANNER_ROOT = Path(_travelplanner_root).resolve()

if str(_TRAVELPLANNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRAVELPLANNER_ROOT))

try:
    from tools.accommodations.apis import Accommodations
    from tools.attractions.apis import Attractions
    from tools.cities.apis import Cities
    from tools.flights.apis import Flights
    from tools.googleDistanceMatrix.apis import GoogleDistanceMatrix
    from tools.notebook.apis import Notebook
    from tools.restaurants.apis import Restaurants
except ImportError as exc:
    logger.error(f"Failed to import TravelPlanner tools from {_TRAVELPLANNER_ROOT}: {exc}")
    raise

mcp = FastMCP("travelplanner", instructions="TravelPlanner environment tool server.")

# Shared notebook instance so the agent can persist information across tool calls.
_notebook = Notebook()

# Tool class instances are created lazily so missing database files surface as
# clean tool-call errors instead of crashing the server at startup.
_tool_cache: dict[str, Any] = {}

# Generic cap on the number of rows returned by search tools.  Zero disables
# the cap and preserves the original TravelPlanner behavior.  This is read once
# at startup so every query is subject to the same limit.
_SEARCH_TOP_K = int(os.environ.get("TRAVELPLANNER_SEARCH_TOP_K", "0"))


def _db_path(rel: str) -> str:
    return str(_TRAVELPLANNER_ROOT / "database" / rel)


def _get(cls_name: str):
    if cls_name not in _tool_cache:
        match cls_name:
            case "flights":
                _tool_cache[cls_name] = Flights(_db_path("flights/clean_Flights_2022.csv"))
            case "accommodations":
                _tool_cache[cls_name] = Accommodations(_db_path("accommodations/clean_accommodations_2022.csv"))
            case "attractions":
                _tool_cache[cls_name] = Attractions(_db_path("attractions/attractions.csv"))
            case "restaurants":
                _tool_cache[cls_name] = Restaurants(_db_path("restaurants/clean_restaurant_2022.csv"))
            case "distance":
                # GoogleDistanceMatrix ignores its constructor arg and loads a
                # hardcoded relative path, so build it against the real CSV and
                # rebind .data to the absolute path under TRAVELPLANNER_ROOT.
                import pandas as pd

                inst = GoogleDistanceMatrix.__new__(GoogleDistanceMatrix)
                inst.gplaces_api_key = ""
                inst.data = pd.read_csv(_db_path("googleDistanceMatrix/distance.csv"))
                _tool_cache[cls_name] = inst
            case "cities":
                _tool_cache[cls_name] = Cities(_db_path("background/citySet_with_states.txt"))
            case _:
                raise ValueError(f"Unknown tool class: {cls_name}")
    return _tool_cache[cls_name]


def _to_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        if _SEARCH_TOP_K > 0:
            result = result.head(_SEARCH_TOP_K)
        return result.to_string(index=False)
    except AttributeError:
        return str(result)


@mcp.tool()
async def search_flights(origin: str, destination: str, departure_date: str) -> str:
    """Search for flights by origin city, destination city, and departure date (YYYY-MM-DD)."""
    flights = _get("flights")
    result = await anyio.to_thread.run_sync(flights.run, origin, destination, departure_date)
    return _to_text(result)


@mcp.tool()
async def search_accommodations(city: str) -> str:
    """Search for accommodations in a given city."""
    acc = _get("accommodations")
    result = await anyio.to_thread.run_sync(acc.run, city)
    return _to_text(result)


@mcp.tool()
async def search_attractions(city: str) -> str:
    """Search for attractions in a given city."""
    att = _get("attractions")
    result = await anyio.to_thread.run_sync(att.run, city)
    return _to_text(result)


@mcp.tool()
async def search_restaurants(city: str) -> str:
    """Search for restaurants in a given city."""
    res = _get("restaurants")
    result = await anyio.to_thread.run_sync(res.run, city)
    return _to_text(result)


@mcp.tool()
async def compute_distance(origin: str, destination: str, mode: str = "driving") -> str:
    """Compute driving/taxi distance, duration, and cost between two cities.

    mode: "driving" or "taxi".
    """
    dist = _get("distance")
    result = await anyio.to_thread.run_sync(dist.run, origin, destination, mode)
    return _to_text(result)


@mcp.tool()
async def list_cities_in_state(state: str) -> str:
    """List all cities in a given US state."""
    cities = _get("cities")
    result = await anyio.to_thread.run_sync(cities.run, state)
    return _to_text(result)


@mcp.tool()
def notebook_write(content: str, description: str) -> str:
    """Record a short text note (e.g. candidate flight/hotel/attraction) in the notebook."""
    return _notebook.write(content, description)


@mcp.tool()
def notebook_list() -> str:
    """List all recorded notebook entries."""
    return str(_notebook.list())


@mcp.tool()
def notebook_read(index: int) -> str:
    """Read a notebook entry by index."""
    return str(_notebook.read(index))


@mcp.tool()
def notebook_reset() -> str:
    """Clear the notebook."""
    _notebook.reset()
    return "Notebook cleared."


if __name__ == "__main__":
    mcp.run(transport="stdio")
