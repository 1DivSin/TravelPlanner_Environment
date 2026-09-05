"""Read-only flight query over the current case's official reference."""

from __future__ import annotations

import _task_reference as reference


async def search_flights(origin: str, destination: str, departure_date: str) -> str:
    """Search official candidate flights for one route and date.

    Args:
        origin: Exact origin city name.
        destination: Exact destination city name.
        departure_date: Departure date in YYYY-MM-DD format.

    Returns:
        The complete official candidate table or official no-flight message.
    """

    origin = reference.strip_parenthetical(origin)
    destination = reference.strip_parenthetical(destination)
    date = departure_date.strip()
    content = reference.content_for(f"Flight from {origin} to {destination} on {date}")
    if content is None:
        return f"There is no flight from {origin} to {destination} on {date}."
    return content
