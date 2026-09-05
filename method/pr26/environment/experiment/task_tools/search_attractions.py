"""Read-only attraction query over the official case reference."""

from __future__ import annotations

import _task_reference as reference


async def search_attractions(city: str) -> str:
    """Search all official attraction candidates in one city.

    Args:
        city: Exact city name.

    Returns:
        The complete unmodified official attraction candidate table.
    """

    return reference.city_content("Attractions", city)
