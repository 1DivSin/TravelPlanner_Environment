"""Read-only TravelPlanner reference search tool injected into both arms."""

from __future__ import annotations

import json
import os
import re
import ast
from pathlib import Path
from typing import Any


def _database_file() -> Path | None:
    case_reference = os.environ.get("TRAVELPLANNER_CASE_REFERENCE", "").strip()
    if case_reference:
        candidate = Path(case_reference)
        if candidate.is_file():
            return candidate
    raw = os.environ.get("TRAVELPLANNER_DATABASE", "").strip()
    if not raw:
        return None
    root = Path(raw)
    if root.is_file():
        return root
    set_type = os.environ.get("TRAVELPLANNER_SET_TYPE", "validation").strip().lower()
    candidates = sorted(root.glob(f"{set_type}_ref_info.jsonl"))
    if not candidates:
        candidates = sorted(root.parent.glob(f"{set_type}_ref_info.jsonl"))
    if not candidates:
        candidates = sorted(root.glob("*_ref_info.jsonl"))
    if not candidates:
        candidates = sorted(root.parent.glob("*_ref_info.jsonl"))
    return candidates[0] if candidates else None


def _load_records(path: Path) -> list[Any]:
    """Load either a JSONL database row or a per-case CSV literal reference."""

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.name.endswith(".jsonl"):
        records: list[Any] = []
        for line in text.splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        value = ast.literal_eval(text)
    return value if isinstance(value, list) else [value]


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key}: {_flatten(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(_flatten(item) for item in value)
    return str(value)


async def travelplanner_search(kind: str, query: str, limit: int = 8) -> str:
    """Search the local TravelPlanner reference database without modifying it.

    Args:
        kind: One of flights, restaurants, accommodations, attractions, or all.
        query: City names, dates, and/or origin/destination terms to match.
        limit: Maximum number of matching reference records to return.
    """

    database_file = _database_file()
    if database_file is None:
        return "[Error] TRAVELPLANNER_DATABASE is unset or contains no *_ref_info.jsonl file."
    allowed = {"flights", "restaurants", "accommodations", "attractions", "all"}
    kind = kind.strip().lower()
    if kind not in allowed:
        return f"[Error] kind must be one of {sorted(allowed)}"
    try:
        limit = max(1, min(int(limit), 30))
    except (TypeError, ValueError):
        return "[Error] limit must be an integer"
    case_specific = bool(os.environ.get("TRAVELPLANNER_CASE_REFERENCE", "").strip())
    terms = [term.lower() for term in re.findall(r"[A-Za-z][A-Za-z0-9' -]{1,}", query) if len(term.strip()) > 2]
    matches: list[str] = []
    for row in _load_records(database_file):
        if len(matches) >= limit:
            break
        text = _flatten(row)
        lowered = text.lower()
        description = str(row.get("Description", "")).lower() if isinstance(row, dict) else ""
        if kind != "all":
            singular = kind.rstrip("s")
            if description and singular not in description and kind not in description:
                continue
            if not description and singular not in lowered:
                continue
        # Per-case references are already query-specific. Global database rows
        # still use conservative keyword filtering to avoid returning unrelated records.
        if not case_specific and terms and not all(term.strip() in lowered for term in terms[:3]):
            continue
        matches.append(text[:12000])
    if not matches:
        return "No matching official reference records found."
    return "\n\n".join(f"[{index}] {match}" for index, match in enumerate(matches, 1))
