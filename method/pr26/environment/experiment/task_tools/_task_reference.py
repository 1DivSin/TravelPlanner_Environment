"""Read-only access to one isolated case's official reference data."""

from __future__ import annotations

import ast
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def strip_parenthetical(value: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()


def _required_file(variable: str) -> Path:
    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise RuntimeError(f"{variable} is not set for this isolated case")
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"isolated task data does not exist: {path}")
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
        raise ValueError("case reference must be a list")
    return tuple(row for row in value if isinstance(row, dict))


def records() -> tuple[dict[str, Any], ...]:
    path = _required_file("CLEAN_CASE_REFERENCE")
    return _load_reference(str(path.resolve()))


def content_for(description: str) -> str | None:
    target = normalize(description)
    for row in records():
        if normalize(str(row.get("Description", ""))) == target:
            return str(row.get("Content", ""))
    return None


def city_content(kind: str, city: str) -> str:
    city = strip_parenthetical(city)
    content = content_for(f"{kind} in {city}")
    if content is None:
        return f"There is no {kind.casefold()} data for {city} in this case's official reference."
    return content


@lru_cache(maxsize=4)
def city_state_rows(path_text: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for line in Path(path_text).read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        city, state = line.split("\t", 1)
        rows.append((city.strip(), state.strip()))
    return tuple(rows)


def cities_in_state(state: str) -> list[str]:
    path = _required_file("CLEAN_CITY_STATE_INDEX")
    target = normalize(state)
    return [
        city
        for city, row_state in city_state_rows(str(path.resolve()))
        if normalize(row_state) == target
    ]
