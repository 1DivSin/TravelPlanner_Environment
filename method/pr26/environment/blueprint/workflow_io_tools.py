"""Workspace-confined read/write tools for the isolated Workflow experiment."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from psi_agent.session.runtime_context import get_workspace


_REFERENCE_READS: set[tuple[str, str]] = set()
_REFERENCE_READS_LOCK = Lock()


def _static_reference_key(path: Path) -> str | None:
    """Collapse the workflow/workflow-skill aliases into two logical files."""

    lowered = tuple(part.casefold() for part in path.parts)
    for skill_name in ("workflow", "workflow-skill"):
        suffix = ("skills", skill_name, "skill.md")
        if lowered[-len(suffix) :] == suffix:
            return "workflow-skill"
        grammar_suffix = ("skills", skill_name, "grammar", "fusionflow.g4")
        if lowered[-len(grammar_suffix) :] == grammar_suffix:
            return "workflow-grammar"
    return None


def _workspace_path(file_path: str) -> Path:
    workspace = Path(get_workspace()).resolve()
    raw = Path((file_path or "").strip())
    candidate = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
    if not candidate.is_relative_to(workspace):
        raise ValueError("file path must stay inside the current TravelPlanner workspace")
    return candidate


async def read(file_path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a UTF-8 file inside the current question workspace."""

    path = _workspace_path(file_path)
    if not path.is_file():
        return f"[Error] File not found: {path}"
    reference_key = _static_reference_key(path)
    read_once = (
        reference_key is not None
        and os.environ.get("TRAVELPLANNER_READ_ONCE_REFERENCES", "") == "1"
    )
    if read_once:
        workspace = str(Path(get_workspace()).resolve())
        read_key = (workspace, reference_key)
        with _REFERENCE_READS_LOCK:
            if read_key in _REFERENCE_READS:
                return (
                    f"[Already loaded] {reference_key} may be read only once in this "
                    "TravelPlanner attempt. Reuse the content already present in the conversation."
                )
            _REFERENCE_READS.add(read_key)
    content = path.read_text(encoding="utf-8", errors="replace")
    if read_once:
        # Static references are returned atomically so one bounded/partial read
        # cannot force a second model round merely to fetch the remainder.
        return content
    if offset == 0 and limit == 0:
        return content
    lines = content.splitlines(keepends=True)
    selected = lines[offset:] if limit == 0 else lines[offset : offset + limit]
    return "".join(selected)


async def write(file_path: str, content: str) -> str:
    """Create or overwrite a UTF-8 file inside the current question workspace."""

    path = _workspace_path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"[OK] Written {len(content.encode('utf-8'))} bytes to {path}"
