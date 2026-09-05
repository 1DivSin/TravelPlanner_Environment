"""Read-only ground transportation query over the official case reference."""

from __future__ import annotations

import _task_reference as reference


async def compute_distance(
    origin: str,
    destination: str,
    mode: str = "self-driving",
) -> str:
    """Look up official inter-city self-driving or taxi details.

    Args:
        origin: Exact origin city name.
        destination: Exact destination city name.
        mode: ``self-driving`` (``driving`` alias) or ``taxi``.

    Returns:
        The exact official transportation string with duration, distance, and cost.
    """

    origin = reference.strip_parenthetical(origin)
    destination = reference.strip_parenthetical(destination)
    normalized_mode = mode.strip().casefold()
    if normalized_mode in {"driving", "self driving", "self-driving"}:
        label = "Self-driving"
    elif normalized_mode == "taxi":
        label = "Taxi"
    else:
        return "[Error] mode must be self-driving or taxi"
    content = reference.content_for(f"{label} from {origin} to {destination}")
    if content is None:
        return f"{label.casefold()}, from {origin} to {destination}, no valid information."
    return content
