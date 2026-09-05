"""Read-only access to the isolated TravelPlanner skill document."""

from __future__ import annotations

from pathlib import Path

from psi_agent.session.runtime_context import get_workspace


async def read(file_path: str, offset: int = 0, limit: int = 0) -> str:
    """Read the TravelPlanner SKILL.md inside the current question workspace."""

    workspace = Path(get_workspace()).resolve()
    requested = Path((file_path or "").strip())
    path = requested.resolve() if requested.is_absolute() else (workspace / requested).resolve()
    expected = (workspace / "skills" / "travelplanner" / "SKILL.md").resolve()
    if path != expected:
        raise ValueError("only skills/travelplanner/SKILL.md is readable in the skill arm")
    if not path.is_file():
        return f"[Error] File not found: {path}"
    content = path.read_text(encoding="utf-8", errors="replace")
    if offset == 0 and limit == 0:
        return content
    lines = content.splitlines(keepends=True)
    selected = lines[offset:] if limit == 0 else lines[offset : offset + limit]
    return "".join(selected)
