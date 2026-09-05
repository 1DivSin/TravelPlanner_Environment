"""Read-only restaurant query over the official case reference."""

from __future__ import annotations

import _task_reference as reference


async def search_restaurants(city: str) -> str:
    """Search all official restaurant candidates in one city.

    Args:
        city: Exact city name.

    Returns:
        The complete unmodified official restaurant candidate table.
    """

    return reference.city_content("Restaurants", city)
