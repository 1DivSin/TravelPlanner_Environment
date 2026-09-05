"""Focused, read-only TravelPlanner tools for the Dolphin evaluation agent.

The host runner writes the current case's official ``reference_information``
to ``TRAVELPLANNER_CASE_REFERENCE``.  These functions expose the same focused
queries as the comparison MCP server without flattening, truncating, or mixing
unrelated reference records.
"""

from __future__ import annotations

import ast
import json
import os
import re
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Any

from psi_agent.session.runtime_context import get_session_id


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _strip_parenthetical(value: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()


def _reference_path() -> Path:
    raw = os.environ.get("TRAVELPLANNER_CASE_REFERENCE", "").strip()
    if not raw:
        raise RuntimeError("TRAVELPLANNER_CASE_REFERENCE is not set for this isolated case")
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"TravelPlanner case reference does not exist: {path}")
    return path


@lru_cache(maxsize=512)
def _load_reference(path_text: str) -> tuple[dict[str, Any], ...]:
    text = Path(path_text).read_text(encoding="utf-8").strip()
    if not text:
        return ()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = ast.literal_eval(text)
    if not isinstance(value, list):
        raise ValueError("TravelPlanner case reference must be a list")
    return tuple(row for row in value if isinstance(row, dict))


def _records() -> tuple[dict[str, Any], ...]:
    return _load_reference(str(_reference_path().resolve()))


def _typed_mode() -> bool:
    return os.environ.get("TRAVELPLANNER_TYPED_TOOLS", "").strip() == "1"


def _require_workflow_step() -> None:
    """Keep benchmark research inside run_flow Agent Steps in the auto arm."""

    if (
        os.environ.get("TRAVELPLANNER_WORKFLOW_STEP_ONLY", "").strip() == "1"
        and get_session_id().strip()
    ):
        raise PermissionError(
            "TravelPlanner data tools are restricted to Agent Steps inside run_flow; "
            "the outer Dolphin session may only author/invoke the Workflow."
        )


@lru_cache(maxsize=512)
def _load_structured_reference(path_text: str) -> dict[str, Any]:
    value = json.loads(Path(path_text).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("structured TravelPlanner reference must be an object")
    return value


def _structured_reference() -> dict[str, Any]:
    raw = os.environ.get("TRAVELPLANNER_CASE_STRUCTURED_REFERENCE", "").strip()
    if not raw:
        raise RuntimeError("TRAVELPLANNER_CASE_STRUCTURED_REFERENCE is not set")
    return _load_structured_reference(str(Path(raw).resolve()))


def _structured_content(description: str) -> Any:
    reference = _structured_reference()
    if description not in reference:
        return None
    return reference[description]


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _content_for(description: str) -> str | None:
    target = _normalize(description)
    for row in _records():
        if _normalize(str(row.get("Description", ""))) == target:
            return str(row.get("Content", ""))
    return None


def _city_content(kind: str, city: str) -> str:
    city = _strip_parenthetical(city)
    description = f"{kind} in {city}"
    content = _content_for(description)
    if content is None:
        return f"There is no {kind.casefold()} data for {city} in this case's official reference."
    return content


def _typed_source(
    kind: str,
    description: str,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Return canonical rows and distinguish missing from valid no-results."""

    raw = _structured_content(description)
    if raw is None:
        return [], "missing", None
    if isinstance(raw, str):
        return [], "none", raw
    if not isinstance(raw, list):
        raise ValueError(f"typed {kind} reference must be an array or no-result string")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if kind == "flight":
            row = {
                "flight_number": item.get("Flight Number"),
                "price": item.get("Price"),
                "departure_time": item.get("DepTime"),
                "arrival_time": item.get("ArrTime"),
                "duration": item.get("ActualElapsedTime"),
                "date": item.get("FlightDate"),
                "origin": item.get("OriginCityName"),
                "destination": item.get("DestCityName"),
                "distance": item.get("Distance"),
            }
        elif kind == "accommodation":
            occupancy = item.get("maximum occupancy")
            row = {
                "name": item.get("NAME"),
                "city": item.get("city"),
                "price": item.get("price"),
                "room_type": item.get("room type"),
                "house_rules": item.get("house_rules"),
                "minimum_nights": item.get("minimum nights"),
                "maximum_occupancy": occupancy,
                "review_rate": item.get("review rate number"),
            }
        elif kind == "restaurant":
            row = {
                "name": item.get("Name"),
                "city": item.get("City"),
                "average_cost": item.get("Average Cost"),
                "cuisines": item.get("Cuisines"),
                "aggregate_rating": item.get("Aggregate Rating"),
            }
        elif kind == "attraction":
            row = {
                "name": item.get("Name"),
                "city": item.get("City"),
                "address": item.get("Address"),
                "latitude": item.get("Latitude"),
                "longitude": item.get("Longitude"),
            }
        else:
            raise ValueError(f"unknown typed candidate kind: {kind}")
        rows.append(row)
    return rows, "available", None


def _typed_rows(kind: str, description: str) -> list[dict[str, Any]]:
    """Return rows for internal index construction."""

    rows, _, _ = _typed_source(kind, description)
    return rows


def _typed_candidate_result(
    kind: str,
    description: str,
    rows: list[dict[str, Any]],
    *,
    availability: str = "available",
    message: str | None = None,
) -> str:
    return _compact_json(
        {
            "schema_version": 1,
            "kind": kind,
            "source": description,
            "candidate_count": len(rows),
            "candidates": rows,
            "availability": availability,
            **({"message": message} if message is not None else {}),
        }
    )


def _room_type_allowed(actual: str, required: str) -> bool:
    required = _normalize(required)
    actual = _normalize(actual)
    if not required:
        return True
    if required == "not shared room":
        return actual != "shared room"
    if required == "entire room":
        return actual == "entire home/apt"
    return actual == required


def _house_rule_allowed(actual: str, required: str) -> bool:
    required = _normalize(required)
    if not required:
        return True
    forbidden = {
        "smoking": "no smoking",
        "parties": "no parties",
        "children under 10": "no children under 10",
        "visitors": "no visitors",
        "pets": "no pets",
    }.get(required)
    return forbidden is None or forbidden not in _normalize(actual)


async def search_flights(origin: str, destination: str, departure_date: str) -> str:
    """Search official candidate flights for one route and date.

    Args:
        origin: Exact origin city name.
        destination: Exact destination city name.
        departure_date: Departure date in YYYY-MM-DD format.

    Returns:
        The complete unmodified official flight candidate table, or the
        official no-flight message for this route and date.
    """

    _require_workflow_step()
    origin = _strip_parenthetical(origin)
    destination = _strip_parenthetical(destination)
    description = f"Flight from {origin} to {destination} on {departure_date.strip()}"
    if _typed_mode():
        rows, availability, message = _typed_source("flight", description)
        return _typed_candidate_result(
            "flight",
            description,
            rows,
            availability=availability,
            message=message,
        )
    content = _content_for(description)
    if content is None:
        return f"There is no flight from {origin} to {destination} on {departure_date.strip()}."
    return content


async def search_accommodations(
    city: str,
    required_nights: int = 0,
    travelers: int = 0,
    required_room_type: str = "",
    required_house_rule: str = "",
) -> str:
    """Search all official accommodation candidates in one city.

    Args:
        city: Exact city name.
        required_nights: Consecutive lodging nights; candidates requiring more are removed.
        travelers: Number of travelers, used to compute ``rooms_required``.
        required_room_type: Official constraint such as ``entire room``,
            ``private room``, ``shared room``, or ``not shared room``.
        required_house_rule: Facility that must be allowed, such as ``pets``,
            ``smoking``, ``visitors``, ``parties``, or ``children under 10``.

    Returns:
        In Workflow mode, a typed and deterministically filtered candidate
        object. In no-workflow mode, the original official candidate table.
    """

    _require_workflow_step()
    if not _typed_mode():
        return _city_content("Accommodations", city)
    city = _strip_parenthetical(city)
    description = f"Accommodations in {city}"
    rows, availability, message = _typed_source("accommodation", description)
    kept: list[dict[str, Any]] = []
    rejected = {"minimum_nights": 0, "room_type": 0, "house_rule": 0, "invalid_occupancy": 0}
    for row in rows:
        minimum_nights = float(row.get("minimum_nights") or 0)
        occupancy = int(row.get("maximum_occupancy") or 0)
        if required_nights and minimum_nights > required_nights:
            rejected["minimum_nights"] += 1
            continue
        if not _room_type_allowed(str(row.get("room_type") or ""), required_room_type):
            rejected["room_type"] += 1
            continue
        if not _house_rule_allowed(str(row.get("house_rules") or ""), required_house_rule):
            rejected["house_rule"] += 1
            continue
        if occupancy < 1:
            rejected["invalid_occupancy"] += 1
            continue
        row = dict(row)
        row["rooms_required"] = ceil(max(1, travelers) / occupancy) if travelers else 1
        kept.append(row)
    result = json.loads(
        _typed_candidate_result(
            "accommodation",
            description,
            kept,
            availability=availability,
            message=message,
        )
    )
    result["filter"] = {
        "required_nights": required_nights,
        "travelers": travelers,
        "required_room_type": required_room_type or None,
        "required_house_rule": required_house_rule or None,
        "source_candidate_count": len(rows),
        "rejected_counts": rejected,
    }
    return _compact_json(result)


async def search_restaurants(city: str) -> str:
    """Search all official restaurant candidates in one city.

    Args:
        city: Exact city name.

    Returns:
        The complete unmodified official restaurant candidate table.
    """

    _require_workflow_step()
    if not _typed_mode():
        return _city_content("Restaurants", city)
    city = _strip_parenthetical(city)
    description = f"Restaurants in {city}"
    rows, availability, message = _typed_source("restaurant", description)
    return _typed_candidate_result(
        "restaurant",
        description,
        rows,
        availability=availability,
        message=message,
    )


async def search_attractions(city: str) -> str:
    """Search all official attraction candidates in one city.

    Args:
        city: Exact city name.

    Returns:
        The complete unmodified official attraction candidate table.
    """

    _require_workflow_step()
    if not _typed_mode():
        return _city_content("Attractions", city)
    city = _strip_parenthetical(city)
    description = f"Attractions in {city}"
    rows, availability, message = _typed_source("attraction", description)
    return _typed_candidate_result(
        "attraction",
        description,
        rows,
        availability=availability,
        message=message,
    )


async def compute_distance(origin: str, destination: str, mode: str = "self-driving") -> str:
    """Look up official inter-city self-driving or taxi details.

    Args:
        origin: Exact origin city name.
        destination: Exact destination city name.
        mode: Either ``self-driving`` (``driving`` is accepted as an alias) or
            ``taxi``.

    Returns:
        The exact official transportation string including route, duration,
        distance, and cost.
    """

    _require_workflow_step()
    origin = _strip_parenthetical(origin)
    destination = _strip_parenthetical(destination)
    normalized_mode = mode.strip().casefold()
    if normalized_mode in {"driving", "self driving", "self-driving"}:
        label = "Self-driving"
    elif normalized_mode == "taxi":
        label = "Taxi"
    else:
        return "[Error] mode must be self-driving or taxi"
    description = f"{label} from {origin} to {destination}"
    if _typed_mode():
        content = _structured_content(description)
        if content is None:
            candidates: list[dict[str, Any]] = []
            availability = "missing"
        elif isinstance(content, str):
            candidates = [{
                "mode": label.casefold(),
                "origin": origin,
                "destination": destination,
                "official_string": content,
            }]
            availability = "available"
        else:
            raise ValueError(f"typed ground transport reference {description!r} must be a string")
        return _typed_candidate_result(
            "ground_transport",
            description,
            candidates,
            availability=availability,
        )
    content = _content_for(description)
    if content is None:
        return f"{label.casefold()}, from {origin} to {destination}, no valid information."
    return content


def _background_file() -> Path | None:
    raw = os.environ.get("TRAVELPLANNER_DATABASE", "").strip()
    if not raw:
        return None
    root = Path(raw)
    candidates = (
        root / "database" / "background" / "citySet_with_states.txt",
        root / "background" / "citySet_with_states.txt",
    )
    return next((path for path in candidates if path.is_file()), None)


@lru_cache(maxsize=4)
def _city_state_rows(path_text: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for line in Path(path_text).read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        city, state = line.split("\t", 1)
        rows.append((city.strip(), state.strip()))
    return tuple(rows)


async def list_cities_in_state(state: str) -> str:
    """List official TravelPlanner cities for a US state.

    Args:
        state: Full US state name, for example ``Texas``.

    Returns:
        A JSON array of exact city names in that state.
    """

    _require_workflow_step()
    path = _background_file()
    if path is None:
        return "[Error] TravelPlanner background city/state database is unavailable"
    target = _normalize(state)
    cities = [city for city, row_state in _city_state_rows(str(path.resolve())) if _normalize(row_state) == target]
    if not cities:
        return json.dumps({"error": f"No official cities found for state {state}"}, ensure_ascii=False)
    return json.dumps(cities, ensure_ascii=False)


def _case_constraints() -> dict[str, Any]:
    raw = os.environ.get("TRAVELPLANNER_CASE_CONSTRAINTS", "").strip()
    if not raw:
        raise RuntimeError("TRAVELPLANNER_CASE_CONSTRAINTS is not set")
    value = json.loads(Path(raw).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("TravelPlanner case constraints must be an object")
    return value


def _candidate_indexes() -> dict[str, Any]:
    restaurants: dict[tuple[str, str], dict[str, Any]] = {}
    attractions: dict[tuple[str, str], dict[str, Any]] = {}
    accommodations: dict[tuple[str, str], dict[str, Any]] = {}
    flights: dict[str, dict[str, Any]] = {}
    ground: set[str] = set()
    for description, value in _structured_reference().items():
        if description.startswith("Restaurants in "):
            for row in _typed_rows("restaurant", description):
                restaurants[(_normalize(str(row["name"])), _normalize(str(row["city"])))] = row
        elif description.startswith("Attractions in "):
            for row in _typed_rows("attraction", description):
                attractions[(_normalize(str(row["name"])), _normalize(str(row["city"])))] = row
        elif description.startswith("Accommodations in "):
            for row in _typed_rows("accommodation", description):
                accommodations[(_normalize(str(row["name"])), _normalize(str(row["city"])))] = row
        elif description.startswith("Flight from "):
            for row in _typed_rows("flight", description):
                flights[_normalize(str(row["flight_number"]))] = row
        elif description.startswith(("Self-driving from ", "Taxi from ")) and isinstance(value, str):
            ground.add(_normalize(value))
    return {
        "restaurants": restaurants,
        "attractions": attractions,
        "accommodations": accommodations,
        "flights": flights,
        "ground": ground,
    }


def _name_city(value: str) -> tuple[str, str] | None:
    if not isinstance(value, str) or value in {"", "-"} or "," not in value:
        return None
    name, city = value.rsplit(",", 1)
    if not name.strip() or not city.strip():
        return None
    return _normalize(name), _normalize(city)


def _route(value: str) -> tuple[str, str] | None:
    match = re.search(r"\bfrom\s+(.+?)\s+to\s+([^,]+)", value, re.IGNORECASE)
    if match is None:
        return None
    return _strip_parenthetical(match.group(1)), _strip_parenthetical(match.group(2))


def _violation(
    violations: list[dict[str, Any]],
    constraint: str,
    message: str,
    *,
    day: int | None = None,
    field: str | None = None,
) -> None:
    item: dict[str, Any] = {"constraint": constraint, "message": message}
    if day is not None:
        item["day"] = day
    if field is not None:
        item["field"] = field
    violations.append(item)


async def validate_travel_plan(plan_json: str) -> str:
    """Deterministically validate an assembled plan against this case's candidates.

    This is an execution-time quality gate for Workflow Agent Steps. It checks
    candidate membership, schema/completeness, routes, duplicate resources,
    accommodation constraints, hard user constraints, and estimated budget.

    Args:
        plan_json: Complete ``idx``/``query``/``plan`` object encoded as JSON.

    Returns:
        A structured JSON validation report. ``valid`` is true only when no
        deterministic violation was found.
    """

    _require_workflow_step()
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as error:
        return _compact_json(
            {
                "schema_version": 1,
                "valid": False,
                "violation_count": 1,
                "violations": [
                    {
                        "constraint": "schema.json",
                        "message": f"plan_json is invalid JSON: {error.msg}",
                    }
                ],
                "computed": {},
            }
        )
    if not isinstance(plan, dict):
        return _compact_json(
            {
                "schema_version": 1,
                "valid": False,
                "violation_count": 1,
                "violations": [
                    {"constraint": "schema.root", "message": "plan_json must encode an object"}
                ],
                "computed": {},
            }
        )
    constraints = _case_constraints()
    indexes = _candidate_indexes()
    violations: list[dict[str, Any]] = []
    days = int(constraints.get("days") or 0)
    travelers = int(constraints.get("people_number") or 1)
    budget = float(constraints.get("budget") or 0)
    itinerary = plan.get("plan") if isinstance(plan, dict) else None
    if not isinstance(itinerary, list):
        _violation(violations, "schema.plan", "plan must be an array")
        itinerary = []
    if len(itinerary) != days:
        _violation(violations, "schema.day_count", f"expected {days} days, received {len(itinerary)}")

    required_keys = {
        "day", "current_city", "transportation", "breakfast", "attraction",
        "lunch", "dinner", "accommodation",
    }
    seen_restaurants: set[tuple[str, str]] = set()
    seen_attractions: set[tuple[str, str]] = set()
    accommodation_sequence: list[tuple[tuple[str, str], int, dict[str, Any]]] = []
    cuisines_seen: set[str] = set()
    transport_modes: set[str] = set()
    visited_cities: set[str] = set()
    total_cost = 0.0
    origin = str(constraints.get("org") or "")

    for position, raw_day in enumerate(itinerary, 1):
        if not isinstance(raw_day, dict):
            _violation(violations, "schema.day", "day entry must be an object", day=position)
            continue
        missing = sorted(required_keys - raw_day.keys())
        if missing:
            _violation(violations, "schema.required_fields", f"missing fields: {missing}", day=position)
        if raw_day.get("day") != position:
            _violation(violations, "schema.day_number", f"day must equal {position}", day=position, field="day")
        current_city = str(raw_day.get("current_city") or "")
        route = _route(current_city)
        allowed_cities: set[str]
        if route is not None:
            allowed_cities = {_normalize(route[0]), _normalize(route[1])}
            if position == 1 and _normalize(route[0]) != _normalize(origin):
                _violation(violations, "route.origin", f"first route must start in {origin}", day=position)
            if position == len(itinerary) and _normalize(route[1]) != _normalize(origin):
                _violation(violations, "route.closed_circle", f"final route must return to {origin}", day=position)
            for city in route:
                if _normalize(city) != _normalize(origin):
                    visited_cities.add(_normalize(city))
        else:
            allowed_cities = {_normalize(_strip_parenthetical(current_city))}
            if allowed_cities != {_normalize(origin)}:
                visited_cities.update(allowed_cities)

        transportation = str(raw_day.get("transportation") or "")
        if route is not None and transportation in {"", "-"}:
            _violation(violations, "transportation.required", "travel day requires transportation", day=position)
        if transportation not in {"", "-"}:
            lowered_transport = transportation.casefold()
            transport_route = _route(transportation)
            if transport_route is None or not {_normalize(x) for x in transport_route}.issubset(allowed_cities):
                _violation(violations, "transportation.city", "transportation route conflicts with current_city", day=position)
            if "flight number:" in lowered_transport:
                transport_modes.add("flight")
                match = re.search(r"Flight Number:\s*([^,]+)", transportation, re.IGNORECASE)
                flight = indexes["flights"].get(_normalize(match.group(1))) if match else None
                if flight is None:
                    _violation(violations, "membership.flight", "flight is not in the case candidates", day=position)
                else:
                    total_cost += float(flight.get("price") or 0) * travelers
                    if str(flight.get("departure_time")) not in transportation or str(flight.get("arrival_time")) not in transportation:
                        _violation(violations, "transportation.flight_format", "flight times do not match the candidate", day=position)
            elif "self-driving" in lowered_transport or "taxi" in lowered_transport:
                mode = "self-driving" if "self-driving" in lowered_transport else "taxi"
                transport_modes.add(mode)
                if _normalize(transportation) not in indexes["ground"]:
                    _violation(violations, "membership.ground_transport", "ground transport is not an exact candidate", day=position)
                cost_match = re.search(r"cost:\s*([0-9.]+)", transportation, re.IGNORECASE)
                if cost_match:
                    capacity = 5 if mode == "self-driving" else 4
                    total_cost += float(cost_match.group(1)) * ceil(travelers / capacity)
            else:
                _violation(violations, "transportation.format", "unsupported transportation format", day=position)

        for field in ("breakfast", "lunch", "dinner"):
            value = str(raw_day.get(field) or "")
            parsed = _name_city(value)
            if parsed is None:
                if value not in {"", "-"}:
                    _violation(violations, "format.restaurant", "expected '<Name>, <City>'", day=position, field=field)
                elif route is None:
                    _violation(violations, "completeness.meal", "non-travel day requires every meal", day=position, field=field)
                continue
            restaurant = indexes["restaurants"].get(parsed)
            if restaurant is None:
                _violation(violations, "membership.restaurant", f"{value!r} is not a candidate", day=position, field=field)
                continue
            if parsed in seen_restaurants:
                _violation(violations, "diversity.restaurant", f"{value!r} is repeated", day=position, field=field)
            seen_restaurants.add(parsed)
            if parsed[1] not in allowed_cities:
                _violation(violations, "city.restaurant", f"{value!r} is outside the current city", day=position, field=field)
            total_cost += float(restaurant.get("average_cost") or 0) * travelers
            cuisines_seen.update(
                _normalize(item) for item in str(restaurant.get("cuisines") or "").split(",") if item.strip()
            )

        attraction_value = str(raw_day.get("attraction") or "")
        if attraction_value not in {"", "-"}:
            if not attraction_value.endswith(";"):
                _violation(violations, "format.attraction", "attraction list must end with ';'", day=position)
            entries = [item.strip() for item in attraction_value.split(";") if item.strip()]
            for entry in entries:
                parsed = _name_city(entry)
                attraction = indexes["attractions"].get(parsed) if parsed else None
                if attraction is None:
                    _violation(violations, "membership.attraction", f"{entry!r} is not a candidate", day=position)
                    continue
                if parsed in seen_attractions:
                    _violation(violations, "diversity.attraction", f"{entry!r} is repeated", day=position)
                seen_attractions.add(parsed)
                if parsed[1] not in allowed_cities:
                    _violation(violations, "city.attraction", f"{entry!r} is outside the current city", day=position)
        elif route is None:
            _violation(violations, "completeness.attraction", "non-travel day requires an attraction", day=position)

        accommodation_value = str(raw_day.get("accommodation") or "")
        parsed_accommodation = _name_city(accommodation_value)
        if parsed_accommodation is None:
            if position < days:
                _violation(violations, "completeness.accommodation", "every lodging night requires accommodation", day=position)
        else:
            accommodation = indexes["accommodations"].get(parsed_accommodation)
            if accommodation is None:
                _violation(violations, "membership.accommodation", f"{accommodation_value!r} is not a candidate", day=position)
            else:
                if parsed_accommodation[1] not in allowed_cities:
                    _violation(violations, "city.accommodation", f"{accommodation_value!r} is outside the current city", day=position)
                accommodation_sequence.append((parsed_accommodation, position, accommodation))
                occupancy = int(accommodation.get("maximum_occupancy") or 0)
                if occupancy < 1:
                    _violation(violations, "accommodation.occupancy", "maximum occupancy must be positive", day=position)
                else:
                    total_cost += float(accommodation.get("price") or 0) * ceil(travelers / occupancy)

    expected_cities = constraints.get("visiting_city_number")
    if expected_cities is not None and len(visited_cities) != int(expected_cities):
        _violation(
            violations,
            "route.visiting_city_count",
            f"expected {expected_cities} visiting cities, found {len(visited_cities)}",
        )
    if {"flight", "self-driving"}.issubset(transport_modes) or {"taxi", "self-driving"}.issubset(transport_modes):
        _violation(violations, "transportation.conflict", f"conflicting modes: {sorted(transport_modes)}")

    # Minimum nights are checked over consecutive appearances, matching the
    # official evaluator's semantics rather than assuming days == nights.
    start = 0
    while start < len(accommodation_sequence):
        key, _, row = accommodation_sequence[start]
        end = start + 1
        while end < len(accommodation_sequence) and accommodation_sequence[end][0] == key and accommodation_sequence[end][1] == accommodation_sequence[end - 1][1] + 1:
            end += 1
        used_nights = end - start
        minimum_nights = int(float(row.get("minimum_nights") or 0))
        if used_nights < minimum_nights:
            _violation(
                violations,
                "accommodation.minimum_nights",
                f"{row.get('name')!r} requires {minimum_nights} nights but is used for {used_nights}",
                day=accommodation_sequence[start][1],
            )
        start = end

    local = constraints.get("local_constraint") or {}
    if isinstance(local, dict):
        room_type = str(local.get("room type") or "")
        house_rule = str(local.get("house rule") or "")
        for _, day, row in accommodation_sequence:
            if not _room_type_allowed(str(row.get("room_type") or ""), room_type):
                _violation(violations, "hard.room_type", f"room type does not satisfy {room_type!r}", day=day)
            if not _house_rule_allowed(str(row.get("house_rules") or ""), house_rule):
                _violation(violations, "hard.house_rule", f"house rules do not allow {house_rule!r}", day=day)
        required_transport = str(local.get("transportation") or "")
        if required_transport == "no flight" and "flight" in transport_modes:
            _violation(violations, "hard.transportation", "query forbids flights")
        if required_transport == "no self-driving" and "self-driving" in transport_modes:
            _violation(violations, "hard.transportation", "query forbids self-driving")
        required_cuisines = local.get("cuisine") or []
        for cuisine in required_cuisines if isinstance(required_cuisines, list) else []:
            if _normalize(str(cuisine)) not in cuisines_seen:
                _violation(violations, "hard.cuisine", f"required cuisine {cuisine!r} is missing")

    if budget and total_cost > budget:
        _violation(violations, "hard.budget", f"estimated cost {total_cost:.2f} exceeds budget {budget:.2f}")

    report = {
        "schema_version": 1,
        "valid": not violations,
        "violation_count": len(violations),
        "violations": violations,
        "computed": {
            "estimated_total_cost": round(total_cost, 2),
            "budget": budget,
            "transport_modes": sorted(transport_modes),
            "visiting_city_count": len(visited_cities),
        },
    }
    return _compact_json(report)
