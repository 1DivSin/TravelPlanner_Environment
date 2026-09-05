"""Read-only city query over the frozen official city/state index."""

from __future__ import annotations

import json

import _task_reference as reference


async def list_cities_in_state(state: str) -> str:
    """List official task cities for one US state.

    Args:
        state: Full US state name, for example ``Texas``.

    Returns:
        A JSON array of exact city names in that state.
    """

    cities = reference.cities_in_state(state)
    if not cities:
        return json.dumps({"error": f"No official cities found for state {state}"})
    return json.dumps(cities, ensure_ascii=False)
