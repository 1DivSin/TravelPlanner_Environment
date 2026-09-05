"""Read-only accommodation query over the official case reference."""

from __future__ import annotations

import _task_reference as reference


async def search_accommodations(city: str) -> str:
    """Search all official accommodation candidates in one city.

    Args:
        city: Exact city name.

    Returns:
        The complete unmodified official accommodation candidate table.
    """

    return reference.city_content("Accommodations", city)
