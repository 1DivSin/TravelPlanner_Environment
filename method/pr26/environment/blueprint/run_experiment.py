#!/usr/bin/env python3
"""Run isolated Dolphin/TravelPlanner experiment arms.

The runner deliberately keeps benchmark-specific scoring outside the agent.  It
normalizes the input questions, launches a fresh psi-agent Session for each
question by default, captures the complete response, and optionally invokes the
official TravelPlanner evaluator supplied by the user.
"""

from __future__ import annotations

import argparse
import ast
import csv
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_AGENT = REPO_ROOT / "psi-agent" / "examples" / "haitun-workspace"
DEFAULT_PSI = REPO_ROOT / "psi-agent" / ".venv" / "bin" / "psi-agent"
DEFAULT_SECRETS = REPO_ROOT / ".secrets" / "psi-li-bingqian.env"
RUN_ROOT = ROOT / ".runs"
WORKFLOW_ARM = "auto-workflow"
SKILL_ARM = "travelplanner-skill"
NO_WORKFLOW_ARM = "no-workflow"
EXPERIMENT_ARMS = (WORKFLOW_ARM, SKILL_ARM, NO_WORKFLOW_ARM)
TRAVELPLANNER_SKILL_SOURCE = ROOT / "skills" / "travelplanner"

_ID_KEYS = ("id", "query_id", "task_id", "index")
_QUESTION_KEYS = ("query", "question", "instruction", "prompt", "user_query")
# Match the comparison Claude runner exactly here.  Treating every bare closing
# fence as a possible opening fence can pair two unrelated FusionFlow/Markdown
# blocks and swallow the final JSON response.
_JSON_BLOCK = re.compile(r"```json[ \t]*\n(.*?)\s*```", re.S | re.I)
_SESSION_INFRASTRUCTURE_EXIT_CODE = 75
_SESSION_INFRASTRUCTURE_RESPONSE_MARKER = "[runner session infrastructure failure]"
_SESSION_INFRASTRUCTURE_LOG_MARKERS = (
    "Session request incomplete:",
    "Session request completed without a terminal result",
)

WORKFLOW_EXPLICIT_ACTIVATION_NOTE = """## Explicit user activation takes precedence

If the current user explicitly asks to use the Workflow skill, activate Authoring
Mode even when the underlying task could otherwise be answered in one shot. Author
and run a concrete workflow before returning the final result; do not bypass the
requested Workflow merely because the task appears simple.
"""


@dataclass(frozen=True)
class Case:
    case_id: str
    question: str
    source_sha256: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RunRecord:
    arm: str
    case_id: str
    status: str
    started_at: str
    elapsed_seconds: float
    raw_response_path: str
    answer_path: str
    workflow_path: str | None
    stdout_sha256: str
    error: str | None = None
    attempt_count: int = 1
    validation_errors: list[str] | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_case_component(case_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", case_id).strip("._")
    return value[:96] or "case"


def _session_infrastructure_failure(log_text: str) -> str | None:
    """Return an explicit Session infrastructure terminal state, if present."""

    for line in reversed(log_text.splitlines()):
        if any(marker in line for marker in _SESSION_INFRASTRUCTURE_LOG_MARKERS):
            return line.split(" - ", 1)[-1].strip()
    return None


def _process_failure_reason(response: str, returncode: int) -> str:
    marker = f"{_SESSION_INFRASTRUCTURE_RESPONSE_MARKER}\n"
    if marker in response:
        reason = response.rsplit(marker, 1)[-1].splitlines()[0].strip()
        if reason:
            return reason
    return f"channel exit code {returncode}"


def _load_env_file(path: Path, _visited: set[Path] | None = None) -> dict[str, str]:
    """Load simple KEY=VALUE lines and explicit ``source FILE`` includes."""

    values: dict[str, str] = {}
    visited = _visited or set()
    path = path.resolve()
    if path in visited:
        return values
    visited.add(path)
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("source ") or line.startswith(". "):
            include = line.split(None, 1)[1].strip()
            include_path = Path(include)
            if not include_path.is_absolute():
                include_path = path.parent / include_path
            values.update(_load_env_file(include_path, visited))
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _json_records(path: Path) -> Iterable[dict[str, Any]]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: each JSONL row must be an object")
            yield value
        return

    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        for key in ("data", "queries", "tasks", "test", "validation", "examples"):
            if isinstance(value.get(key), list):
                rows = value[key]
                break
        else:
            # Some benchmark exports are an object keyed by task id.
            rows = [dict(row, id=str(key)) for key, row in value.items() if isinstance(row, dict)]
    else:
        raise ValueError(f"{path}: expected a JSON list/object")
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{number}: each row must be an object")
        yield row


def _csv_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def _find_data_file(data: Path) -> Path:
    if data.is_file():
        return data
    candidates = sorted(
        p
        for p in data.rglob("*")
        if p.is_file() and p.suffix.lower() in {".json", ".jsonl", ".ndjson", ".csv"}
        and any(token in p.name.lower() for token in ("test", "validation", "val", "query"))
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f"No benchmark JSON/JSONL/CSV file found under {data}")
    names = "\n".join(f"  - {p}" for p in candidates[:20])
    raise ValueError(f"Multiple candidate data files found; pass the exact file with --data:\n{names}")


def load_cases(data: Path) -> list[Case]:
    source = _find_data_file(data)
    records = _csv_records(source) if source.suffix.lower() == ".csv" else _json_records(source)
    cases: list[Case] = []
    seen: set[str] = set()
    for ordinal, row in enumerate(records, 1):
        question = next((row.get(key) for key in _QUESTION_KEYS if isinstance(row.get(key), str)), None)
        if not question or not question.strip():
            raise ValueError(f"{source}:{ordinal}: no question field; supported keys: {_QUESTION_KEYS}")
        raw_id = next((row.get(key) for key in _ID_KEYS if row.get(key) is not None), ordinal)
        case_id = str(raw_id).strip() or str(ordinal)
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cases.append(Case(case_id, question.strip(), _sha256_text(canonical), row))
    if not cases:
        raise ValueError(f"No cases found in {source}")
    return cases


def write_manifest(cases: list[Case], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(asdict(case), ensure_ascii=False, sort_keys=True) + "\n")


def read_manifest(path: Path) -> list[Case]:
    cases: list[Case] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cases.append(Case(str(row["case_id"]), str(row["question"]), str(row["source_sha256"]), row.get("metadata", {})))
    if not cases:
        raise ValueError(f"Manifest is empty: {path}")
    return cases


def align_cases_to_prompt_queries(cases: list[Case], path: Path) -> list[Case]:
    """Overlay only ``idx`` and ``query`` from the Claude comparison source.

    The comparison package's JSONL also contains example/result plans.  Those
    fields are deliberately ignored and never copied into ``Case.metadata`` or
    exposed to the model.  Official constraints, reference information, and
    evaluator row alignment remain owned by the original manifest.
    """

    rows = list(_json_records(path))
    if len(rows) != len(cases):
        raise ValueError(f"{path}: expected {len(cases)} prompt rows, found {len(rows)}")
    aligned: list[Case] = []
    for ordinal, (case, row) in enumerate(zip(cases, rows, strict=True), 1):
        raw_idx = row.get("idx")
        query = row.get("query")
        if str(raw_idx) != case.case_id:
            raise ValueError(
                f"{path}:{ordinal}: prompt idx {raw_idx!r} does not match manifest case {case.case_id!r}"
            )
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"{path}:{ordinal}: prompt query must be a non-empty string")
        metadata = dict(case.metadata)
        metadata["prompt_alignment"] = {
            "source": str(path.resolve()),
            "query_sha256": _sha256_text(query),
            "official_query_differs": query != case.question,
        }
        aligned.append(Case(case.case_id, query, case.source_sha256, metadata))
    return aligned


def select_case_range(cases: list[Case], value: str) -> list[Case]:
    """Select an inclusive, 1-based manifest range for an isolated shard."""

    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)", value.strip())
    if match is None:
        raise ValueError("--case-range must use inclusive START:END syntax, for example 65:103")
    start, end = (int(part) for part in match.groups())
    if start > end:
        raise ValueError("--case-range START must not be greater than END")
    if end > len(cases):
        raise ValueError(f"--case-range END {end} exceeds manifest size {len(cases)}")
    return cases[start - 1 : end]


def _write_experiment_context(workspace: Path, arm: str, agent: Path | None = None) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "USER.md").write_text(
        "# TravelPlanner experiment\n\n"
        "This is an isolated benchmark workspace.\n\n"
        "The host runner, not the model, owns the final timing and score records.\n",
        encoding="utf-8",
    )
    (workspace / "AGENTS.md").write_text(
        "# Experiment contract\n\n"
        "Do not access credentials, unrelated workspaces, or external benchmark answers. "
        "Treat the question in the current user message as the only task input. "
        "Use only the focused TravelPlanner tools exposed by this isolated agent.\n",
        encoding="utf-8",
    )
    if arm == WORKFLOW_ARM:
        if agent is None:
            raise ValueError("auto-workflow workspace preparation requires the isolated agent path")
        source_skill = agent / "skills" / "workflow"
        target_skill = workspace / "skills" / "workflow"
        target_skill.mkdir(parents=True, exist_ok=True)
        for name in ("SKILL.md", "README.md"):
            source = source_skill / name
            if source.is_file():
                shutil.copy2(source, target_skill / name)
        source_grammar = source_skill / "grammar"
        if source_grammar.is_dir():
            shutil.copytree(source_grammar, target_skill / "grammar", dirs_exist_ok=True)
        # Natural-language references to "the workflow skill" are sometimes
        # resolved by models as the slug ``workflow-skill``.  Keep an identical
        # read-only alias so skill discovery cannot become the treatment.
        alias_skill = workspace / "skills" / "workflow-skill"
        shutil.copytree(target_skill, alias_skill, dirs_exist_ok=True)
    elif arm == SKILL_ARM:
        if agent is None:
            raise ValueError("travelplanner-skill workspace preparation requires the isolated agent path")
        source_skill = agent / "skills" / "travelplanner"
        target_skill = workspace / "skills" / "travelplanner"
        target_skill.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_skill / "SKILL.md", target_skill / "SKILL.md")


def _write_agent_bootstrap(agent: Path, arm: str) -> None:
    """Replace the general-purpose Haitun bootstrap with the experiment surface.

    The source agent documents describe a large production tool catalogue.  Keeping
    those documents after removing the corresponding tools wastes context and can
    encourage invalid tool calls, so each new run gets a small arm-specific copy.
    """

    (agent / "AGENTS.md").write_text(
        "# Isolated TravelPlanner evaluation agent\n\n"
        "The current user message is the only benchmark task input. Do not access "
        "credentials, the web, external benchmark answers, unrelated workspaces, or "
        "tools that are not listed in TOOLS.md. Follow the current user request and "
        "return the requested result directly.\n",
        encoding="utf-8",
    )


def _write_agent_system(agent: Path, arm: str) -> None:
    """Install a minimal benchmark system prompt without production side effects."""

    requested_skill_path = (
        "skills/travelplanner/SKILL.md" if arm == SKILL_ARM else "skills/workflow/SKILL.md"
    )
    prompt = (
        "You are Haitun (Dolphin), running in an isolated TravelPlanner benchmark profile.\n\n"
        "The current user message is the only task input. Do not access credentials, the web, "
        "external benchmark answers, unrelated workspaces, or any tool not explicitly exposed "
        "by this session. Do not invoke subagents, supervisors, memory services, schedules, "
        "messages, or background tasks.\n\n"
        "When the user asks you to use a skill, read and follow its instructions from "
        "skills/<skill-name>/SKILL.md using the tools available in the session before solving "
        f"the task. The requested skill's exact path is {requested_skill_path}.\n\n"
        "Follow the current user request, use only the tools exposed by this session, and "
        "return the requested result directly."
    )
    source = (
        '"""Minimal system entry for the isolated Haitun TravelPlanner benchmark."""\n\n'
        f"_SYSTEM_PROMPT = {prompt!r}\n\n"
        "async def system_prompt_builder() -> str:\n"
        "    return _SYSTEM_PROMPT\n"
    )
    systems = agent / "systems"
    systems.mkdir(parents=True, exist_ok=True)
    (systems / "system.py").write_text(source, encoding="utf-8")
    constrained_workflow = (
        arm == WORKFLOW_ARM
        and os.environ.get("TRAVELPLANNER_PROMPT_VARIANT", "v1").casefold()
        in {"v4", "v6-token-efficient"}
    )
    if constrained_workflow:
        tools_text = (
            "# Available tools\n\n"
            "Outer Dolphin tools:\n"
            "- read/write: author the Workflow; SKILL.md and FusionFlow.g4 are each read-once references\n"
            "- run_flow: execute the authored Workflow\n\n"
            "The following typed TravelPlanner tools are callable only by Agent Steps inside run_flow:\n"
            "- search_flights(origin, destination, departure_date)\n"
            "- search_accommodations(city, required_nights, travelers, required_room_type, required_house_rule)\n"
            "- search_restaurants(city)\n"
            "- search_attractions(city)\n"
            "- compute_distance(origin, destination, mode)\n"
            "- list_cities_in_state(state)\n"
            "- validate_travel_plan(plan_json)\n\n"
            "The outer session must not call TravelPlanner data tools directly. No browser, shell, "
            "messaging, or external-search tool is available.\n"
        )
    elif arm == SKILL_ARM:
        tools_text = (
            "# Available tools\n\n"
            "Skill reference tool:\n"
            "- read: read skills/travelplanner/SKILL.md\n\n"
            "TravelPlanner data tools:\n"
            "- search_flights(origin, destination, departure_date)\n"
            "- search_accommodations(city)\n"
            "- search_restaurants(city)\n"
            "- search_attractions(city)\n"
            "- compute_distance(origin, destination, mode)\n"
            "- list_cities_in_state(state)\n"
            "\nNo Workflow, run_flow, browser, shell, messaging, or external-search tool is available.\n"
        )
    else:
        tools_text = (
            "# Available tools\n\n"
            "TravelPlanner data tools:\n"
            "- search_flights(origin, destination, departure_date)\n"
            "- search_accommodations(city)\n"
            "- search_restaurants(city)\n"
            "- search_attractions(city)\n"
            "- compute_distance(origin, destination, mode)\n"
            "- list_cities_in_state(state)\n"
            "\nNo browser, shell, messaging, or external-search tool is available.\n"
        )
    (agent / "TOOLS.md").write_text(tools_text, encoding="utf-8")


def _prepare_agent(source: Path, destination: Path, arm: str) -> Path:
    if destination.exists():
        return destination
    ignored_names = ["__pycache__", ".mcp_cache"]
    if arm in (NO_WORKFLOW_ARM, SKILL_ARM):
        ignored_names.extend(["workflow", "run_flow.py", "flow_run.py", "flow_manage.py", "fusion-flow-legacy"])
    ignored = shutil.ignore_patterns(*ignored_names)
    shutil.copytree(source, destination, ignore=ignored)
    tools_dir = destination / "tools"
    allowed_public_tools = {"travelplanner_tools.py"}
    if arm == WORKFLOW_ARM:
        allowed_public_tools.update({"run_flow.py", "workflow_io_tools.py"})
    elif arm == SKILL_ARM:
        allowed_public_tools.add("travelplanner_skill_io_tools.py")
    for path in tools_dir.glob("*.py"):
        if not path.name.startswith("_") and path.name not in allowed_public_tools:
            path.unlink()

    skills_dir = destination / "skills"
    if skills_dir.is_dir():
        for path in skills_dir.iterdir():
            if arm == WORKFLOW_ARM and path.name == "workflow":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    overlay = ROOT / "travelplanner_tools.py"
    shutil.copy2(overlay, destination / "tools" / overlay.name)
    if arm == WORKFLOW_ARM:
        workflow_io_overlay = ROOT / "workflow_io_tools.py"
        shutil.copy2(workflow_io_overlay, destination / "tools" / workflow_io_overlay.name)
        skill_document = destination / "skills" / "workflow" / "SKILL.md"
        skill_document.parent.mkdir(parents=True, exist_ok=True)
        original_skill = skill_document.read_text(encoding="utf-8") if skill_document.is_file() else ""
        if WORKFLOW_EXPLICIT_ACTIVATION_NOTE not in original_skill:
            insertion = 0
            if original_skill.startswith("---\n"):
                frontmatter_end = original_skill.find("\n---\n", 4)
                if frontmatter_end != -1:
                    insertion = frontmatter_end + len("\n---\n")
            aligned_skill = (
                original_skill[:insertion]
                + "\n"
                + WORKFLOW_EXPLICIT_ACTIVATION_NOTE
                + "\n"
                + original_skill[insertion:]
            )
            skill_document.write_text(aligned_skill, encoding="utf-8")
    elif arm == SKILL_ARM:
        skill_io_overlay = ROOT / "travelplanner_skill_io_tools.py"
        shutil.copy2(skill_io_overlay, destination / "tools" / skill_io_overlay.name)
        target_skill = destination / "skills" / "travelplanner"
        shutil.copytree(TRAVELPLANNER_SKILL_SOURCE, target_skill)
    _write_agent_bootstrap(destination, arm)
    _write_agent_system(destination, arm)
    return destination


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_ai_config(path: Path, socket_path: Path) -> None:
    path.write_text(
        "- type: ai\n"
        f"  session_socket: {_yaml_string(str(socket_path))}\n"
        "  provider: \"\"\n  model: \"\"\n  api_key: \"\"\n  base_url: \"\"\n",
        encoding="utf-8",
    )


def _write_session_config(path: Path, socket_path: Path, ai_socket: Path, workspace: Path, agent: Path, appdata: Path, session_id: str) -> None:
    path.write_text(
        "- type: session\n"
        f"  ai_socket: {_yaml_string(str(ai_socket))}\n"
        f"  channel_socket: {_yaml_string(str(socket_path))}\n"
        f"  workspace: {_yaml_string(str(workspace))}\n"
        f"  agent: {_yaml_string(str(agent))}\n"
        f"  appdata: {_yaml_string(str(appdata))}\n"
        f"  session_id: {_yaml_string(session_id)}\n"
        "  max_tool_rounds: 256\n",
        encoding="utf-8",
    )


def _literal_metadata(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return value


def _case_constraint_payload(case: Case) -> dict[str, Any]:
    """Materialize the official query fields used by deterministic checks."""

    metadata = case.metadata
    try:
        idx: int | str = int(case.case_id)
    except ValueError:
        idx = case.case_id
    payload: dict[str, Any] = {"idx": idx, "query": case.question}
    for key in (
        "org",
        "dest",
        "days",
        "date",
        "people_number",
        "budget",
        "level",
        "local_constraint",
        "visiting_city_number",
    ):
        if key in metadata:
            payload[key] = _literal_metadata(metadata[key])
    for key in ("days", "people_number", "visiting_city_number"):
        if key in payload:
            payload[key] = int(payload[key])
    if "budget" in payload:
        payload["budget"] = float(payload["budget"])
    return payload


def _structured_reference_row(database: Path, set_type: str, case_id: str) -> dict[str, Any] | None:
    """Load the matching official typed reference row without exposing other cases."""

    try:
        ordinal = int(case_id)
    except ValueError:
        return None
    candidates = (
        database / f"{set_type}_ref_info.jsonl",
        database.parent / f"{set_type}_ref_info.jsonl",
        database / "database" / f"{set_type}_ref_info.jsonl",
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        return None
    for index, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if index != ordinal:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{source}:{index}: structured reference row must be an object")
        return value
    raise ValueError(f"{source}: no structured reference row for case {case_id}")


class DolphinService:
    def __init__(self, *, psi: Path, env: dict[str, str], arm_root: Path, agent_source: Path, arm: str) -> None:
        self.psi = psi
        self.env = env
        self.arm_root = arm_root
        self.agent_source = agent_source
        self.arm = arm
        self.socket_root = Path(tempfile.mkdtemp(prefix="travelplanner-psi-"))
        self.ai_socket = self.socket_root / "ai.sock"
        self._ai_process: subprocess.Popen[str] | None = None
        self._session_process: subprocess.Popen[str] | None = None
        self._session_socket: Path | None = None
        self._session_log_path: Path | None = None

    def _wait_for_socket(self, socket: Path, process: subprocess.Popen[str], timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if socket.exists():
                return
            if process.poll() is not None:
                raise RuntimeError(f"psi-agent exited during startup (code={process.returncode}); see log")
            time.sleep(0.2)
        raise TimeoutError(f"timed out waiting for socket: {socket}")

    def start_ai(self, *, attempt: int = 1) -> None:
        self.arm_root.mkdir(parents=True, exist_ok=True)
        if self.ai_socket.exists():
            self.ai_socket.unlink()
        config = self.arm_root / "ai.yml"
        log = (self.arm_root / "ai.log").open("a", encoding="utf-8")
        log.write(f"\n[runner] AI process start attempt {attempt}\n")
        log.flush()
        _write_ai_config(config, self.ai_socket)
        self._ai_process = subprocess.Popen(
            [str(self.psi), "run", str(config)], cwd=REPO_ROOT, env=self.env,
            stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        self._wait_for_socket(self.ai_socket, self._ai_process)

    def run_case(
        self,
        case: Case,
        prompt: str,
        *,
        attempt: int = 1,
        reuse_session: bool = False,
    ) -> tuple[str, float, int]:
        case_root = self.arm_root / "cases" / _safe_case_component(case.case_id)
        agent = self.agent_source
        attempt_suffix = f"attempt-{attempt}"
        workspace = (
            self.arm_root
            / "workspace"
            / _safe_case_component(case.case_id)
            / attempt_suffix
        )
        _write_experiment_context(workspace, self.arm, self.agent_source)
        appdata = case_root / f"appdata-{attempt_suffix}"
        socket_path = self.socket_root / f"s-{_safe_case_component(case.case_id)[:48]}-{attempt_suffix}.sock"
        config = case_root / f"session.{attempt_suffix}.yml"
        case_root.mkdir(parents=True, exist_ok=True)
        reference = case.metadata.get("reference_information")
        if isinstance(reference, str) and reference.strip():
            reference_path = case_root / "reference_information.json"
            reference_path.write_text(reference, encoding="utf-8")
            self.env["TRAVELPLANNER_CASE_REFERENCE"] = str(reference_path)
        else:
            self.env.pop("TRAVELPLANNER_CASE_REFERENCE", None)
        if self.arm in (WORKFLOW_ARM, SKILL_ARM):
            database = Path(self.env["TRAVELPLANNER_DATABASE"])
            set_type = self.env.get("TRAVELPLANNER_SET_TYPE", "validation")
            structured = _structured_reference_row(database, set_type, case.case_id)
            if structured is not None:
                structured_path = case_root / "reference_information.structured.json"
                structured_path.write_text(
                    json.dumps(structured, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                self.env["TRAVELPLANNER_CASE_STRUCTURED_REFERENCE"] = str(structured_path)
            else:
                self.env.pop("TRAVELPLANNER_CASE_STRUCTURED_REFERENCE", None)
            constraints_path = case_root / "case_constraints.json"
            constraints_path.write_text(
                json.dumps(_case_constraint_payload(case), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.env["TRAVELPLANNER_CASE_CONSTRAINTS"] = str(constraints_path)
        session_id = f"travelplanner-{self.arm}-{case.case_id}-{attempt_suffix}"
        if not reuse_session or self._session_process is None:
            self.stop_session()
            _write_session_config(config, socket_path, self.ai_socket, workspace, agent, appdata, session_id)
            self._session_log_path = case_root / f"session.{attempt_suffix}.log"
            log = self._session_log_path.open("w", encoding="utf-8")
            self._session_process = subprocess.Popen(
                [str(self.psi), "run", str(config)], cwd=REPO_ROOT, env=self.env,
                stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            self._wait_for_socket(socket_path, self._session_process)
            self._session_socket = socket_path
        elif self._session_socket is not None:
            socket_path = self._session_socket
        session_log_path = self._session_log_path
        session_log_offset = (
            session_log_path.stat().st_size
            if session_log_path is not None and session_log_path.exists()
            else 0
        )
        started = time.monotonic()
        completed = subprocess.run(
            [str(self.psi), "channel", "cli", "--session-socket", str(socket_path), "--message", "-"],
            input=prompt, cwd=REPO_ROOT, env=self.env, capture_output=True, text=True, timeout=1800,
            check=False,
        )
        elapsed = time.monotonic() - started
        response = completed.stdout
        returncode = completed.returncode
        session_failure: str | None = None
        if session_log_path is not None and session_log_path.exists():
            with session_log_path.open("rb") as log_file:
                log_file.seek(session_log_offset)
                session_failure = _session_infrastructure_failure(
                    log_file.read().decode("utf-8", errors="replace")
                )
        if returncode == 0 and session_failure is not None:
            returncode = _SESSION_INFRASTRUCTURE_EXIT_CODE
            response += f"\n{_SESSION_INFRASTRUCTURE_RESPONSE_MARKER}\n{session_failure}\n"
        if completed.returncode != 0:
            response += f"\n[runner stderr]\n{completed.stderr}"
        return response, elapsed, returncode

    def stop_session(self) -> None:
        if self._session_process is None:
            return
        _terminate(self._session_process)
        self._session_process = None
        self._session_socket = None
        self._session_log_path = None
        for socket in self.socket_root.glob("s-*.sock"):
            socket.unlink(missing_ok=True)

    def close(self) -> None:
        self.stop_session()
        self.stop_ai()
        shutil.rmtree(self.socket_root, ignore_errors=True)

    def stop_ai(self) -> None:
        if self._ai_process is not None:
            _terminate(self._ai_process)
            self._ai_process = None
        self.ai_socket.unlink(missing_ok=True)


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


CLAUDE_CC_PROMPT_TEMPLATE = """You are a travel planning assistant. Use the available TravelPlanner tools to build a complete trip plan for the user query below.

User query:
{query}

Requirements:
1. Search flights, accommodations, restaurants, and attractions as needed.
2. Respect the budget, number of travelers, dates, and any local constraints in the query.
3. Return ONLY a JSON object inside a markdown code block. The JSON must have this exact shape:

```json
{{
  "idx": {idx},
  "query": {query_json},
  "plan": [
    {{
      "day": 1,
      "current_city": "from Origin to Destination",
      "transportation": "Flight Number: F1234567, from Origin to Destination, Departure Time: 09:00, Arrival Time: 11:00",
      "breakfast": "-",
      "attraction": "Attraction Name, City;Another Attraction, City;",
      "lunch": "Restaurant Name, City",
      "dinner": "Restaurant Name, City",
      "accommodation": "Accommodation Name, City"
    }},
    ...
  ]
}}
```

Field rules:
- `day`: 1-indexed integer.
- `current_city`: on the first day use "from <origin> to <destination>"; on the last day use "from <current city> to <origin/home>"; otherwise the city name.
- `transportation`: use the exact flight/self-driving/taxi format returned by the tools, or "-" if no travel that day.
- `breakfast`, `lunch`, `dinner`: "<Name>, <City>" or "-".
- `attraction`: semicolon-separated "<Name>, <City>;" entries, or "-".
- `accommodation`: "<Name>, <City>" or "-".

Do not include any explanation outside the JSON code block.
"""


# User-provided Claude Code Dynamic Workflow prompt.  The task, limits, output
# schema, and field rules are unchanged; only the workflow entry point and the
# local name for a workflow subagent are adapted to Haitun/FusionFlow.
CC_DYNAMIC_WORKFLOW_PROMPT_TEMPLATE = """Please complete the task using workflow skill. Author and run one Workflow through run_flow to plan a complete TravelPlanner itinerary for the user query below.

User query:
{query}

Workflow design constraints (CRITICAL - follow exactly):
1. Keep the workflow SMALL and FAST. Use at most 3 phases and at most 5 Agent Steps (subagents) total.
2. Prefer SEQUENTIAL phases over deep nesting or excessive parallelism.
3. Each Agent Step (subagent) should make at most 5 tool calls. If a tool returns no results, try ONE alternative and then move on - do not loop or retry repeatedly.
4. The entire workflow must complete within 10 minutes. Bias toward producing a good-enough plan quickly rather than an optimal plan slowly.
5. Do not create Agent Steps (subagents) for tasks that can be done inline. Only parallelize independent searches (e.g. different cities).

Itinerary requirements:
1. Use the available TravelPlanner tools (flights, accommodations, restaurants, attractions, distance) as needed.
2. Respect the budget, number of travelers, dates, and any local constraints in the query.
3. Return ONLY a JSON object inside a markdown code block. The JSON must have this exact shape:

```json
{{
  "idx": {idx},
  "query": {query_json},
  "plan": [
    {{
      "day": 1,
      "current_city": "from Origin to Destination",
      "transportation": "Flight Number: F1234567, from Origin to Destination, Departure Time: 09:00, Arrival Time: 11:00",
      "breakfast": "-",
      "attraction": "Attraction Name, City;Another Attraction, City;",
      "lunch": "Restaurant Name, City",
      "dinner": "Restaurant Name, City",
      "accommodation": "Accommodation Name, City"
    }},
    ...
  ]
}}
```

Field rules:
- `day`: 1-indexed integer.
- `current_city`: on the first day use "from <origin> to <destination>"; on the last day use "from <current city> to <origin/home>"; otherwise the city name.
- `transportation`: use the exact flight/self-driving/taxi format returned by the tools, or "-" if no travel that day.
- `breakfast`, `lunch`, `dinner`: "<Name>, <City>" or "-".
- `attraction`: semicolon-separated "<Name>, <City>;" entries, or "-".
- `accommodation`: "<Name>, <City>" or "-".

Do not include any explanation outside the JSON code block.
"""

CC_DYNAMIC_PROMPT_VARIANT = "cc_dynamic_v1"
TOKEN_EFFICIENT_PROMPT_VARIANT = "v6-token-efficient"

# Direct-skill counterpart of the current CC Dynamic Workflow prompt.  Keep the
# itinerary and output contract byte-identical; only phrases that require the
# Workflow runtime are adapted to direct execution in the parent session.
CC_SKILL_CARRIER_ADAPTERS = (
    (
        "Please complete the task using workflow skill. Author and run one Workflow through run_flow to plan a complete TravelPlanner itinerary for the user query below.",
        "Please complete the task using travelplanner skill. Use it directly to plan a complete TravelPlanner itinerary for the user query below.",
    ),
    ("Workflow design constraints (CRITICAL - follow exactly):", "Skill execution constraints (CRITICAL - follow exactly):"),
    (
        "1. Keep the workflow SMALL and FAST. Use at most 3 phases and at most 5 Agent Steps (subagents) total.",
        "1. Keep the work SMALL and FAST. Use at most 3 phases and at most 5 bounded planning stages total.",
    ),
    (
        "2. Prefer SEQUENTIAL phases over deep nesting or excessive parallelism.",
        "2. Prefer SEQUENTIAL phases over unnecessary branching or excessive parallelism.",
    ),
    (
        "3. Each Agent Step (subagent) should make at most 5 tool calls.",
        "3. Each bounded planning stage should make at most 5 tool calls.",
    ),
    (
        "4. The entire workflow must complete within 10 minutes.",
        "4. The entire task must complete within 10 minutes.",
    ),
    (
        "5. Do not create Agent Steps (subagents) for tasks that can be done inline. Only parallelize independent searches (e.g. different cities).",
        "5. Do not create subagents or a Workflow. Handle stages inline and group only independent searches (e.g. different cities).",
    ),
)


def _apply_prompt_adapters(template: str, adapters: tuple[tuple[str, str], ...]) -> str:
    adapted = template
    for source, target in adapters:
        if source not in adapted:
            raise RuntimeError(f"prompt carrier adapter source is missing: {source!r}")
        adapted = adapted.replace(source, target, 1)
    return adapted


CC_SKILL_PROMPT_TEMPLATE = _apply_prompt_adapters(
    CC_DYNAMIC_WORKFLOW_PROMPT_TEMPLATE,
    CC_SKILL_CARRIER_ADAPTERS,
)


# This is an opt-in smoke-test treatment.  The original prompt remains the
# default so that historical runs stay exactly reproducible.  v2 borrows only
# the narrowest useful parts of the Claude Code workflow prompts: explicit
# orchestration, structured Step returns, dependency-aware parallelism, and an
# outer response contract.  It intentionally does not add a validator Step;
# this trial is meant to isolate prompt/return-contract effects.
WORKFLOW_TREATMENT_V2 = """Please complete the task using workflow skill.

Workflow execution contract for this trial:
1. Author and run one concrete Workflow through run_flow before returning the answer.
2. Use the Workflow to decompose the TravelPlanner work into bounded steps. Do not add a separate validator/audit Step in this trial.
3. Run independent TravelPlanner searches in parallel only when they have no dependency; keep dependent selection and assembly steps sequential.
4. Each Agent Step must return exactly its declared structured Artifact via the structured step-result tool. Do not put an intermediate result in prose.
5. The final assembly Step must produce the complete `{idx, query, plan}` result and preserve every required field. After run_flow returns, emit ONLY that JSON object inside the requested markdown JSON code block; do not expose Workflow narration, tool traces, or acknowledgements.
6. If the Workflow cannot be authored or run, do not fabricate a plan. Return the required empty-plan object so the failure remains observable.

"""

# Validator treatment.  This is intentionally a separate opt-in variant so the
# earlier v1/v2 runs remain byte-for-byte reproducible.  The validator is an
# explicit Workflow step (rather than this runner silently repairing output),
# which lets us measure whether a model-authored validation/repair loop helps.
WORKFLOW_TREATMENT_V3 = """Please complete the task using workflow skill.

Workflow execution contract for this validator trial:
1. Author and run one concrete Workflow through run_flow before returning the answer.
2. Decompose the TravelPlanner work into bounded steps. Run independent searches in parallel only when they have no dependency; keep selection, assembly, validation, and repair sequential.
3. Each search/selection/assembly Agent Step must return exactly its declared structured Artifact through the structured step-result tool. Preserve raw candidate fields needed for filtering: flight route/date/time/price, accommodation name/city/price/minimum nights/maximum occupancy/house rules/room type, restaurant name/city/average cost/cuisine, and attraction name/city.
4. The Workflow MUST contain an explicit validator step after the first assembly step. The validator consumes the candidate artifacts and the assembled plan and returns a structured validation_report, not prose. It must check: exact day count and required keys; closed-circle outbound/return transportation; transportation format; every selected resource belongs to the corresponding candidate artifact; accommodation minimum nights and occupancy; accommodation only on lodging nights; required meals/attractions; restaurant non-repetition where enough candidates exist; city consistency; and total budget.
5. If validation_report contains any failure, run a sequential repair/assembly step that consumes the report plus the original artifacts, changes only invalid selections/fields, and emits a new final plan. Validate the repaired plan again before workflow completion. Do not silently accept a failed validation report.
6. The final assembly/repair Artifact must be exactly the complete `{idx, query, plan}` result. After run_flow returns, emit ONLY that JSON object inside the requested markdown JSON code block; do not expose Workflow narration, tool traces, validation reports, or acknowledgements.
7. If the Workflow cannot be authored, run, or validated, do not fabricate a plan. Return the required empty-plan object so the failure remains observable.

"""

# Constrained dynamic-workflow treatment.  This deliberately keeps per-case
# Workflow authoring (the fixed-graph treatment is a separate experiment), but
# removes four avoidable failure modes: repeated reference reads, outer-agent
# TravelPlanner research, free-text candidate artifacts, and prose-only review.
WORKFLOW_TREATMENT_V4 = """Please complete the task using workflow skill.

Workflow execution contract for this constrained dynamic-workflow trial:
1. Read `skills/workflow/SKILL.md` exactly once and
   `skills/workflow/grammar/FusionFlow.g4` at most once. Retain their contents in
   the current context; never read either file again, including through the
   `workflow-skill` alias.
2. The outer Dolphin session may parse the request, author the Workflow, call
   `run_flow`, and return its final Artifact. It MUST NOT call any TravelPlanner
   data tool itself. Only Agent Steps inside the Workflow may search flights,
   accommodations, restaurants, attractions, cities, or distances.
3. Author and run one concrete Workflow through `run_flow`. This remains a
   per-question dynamically authored Workflow; do not substitute a pre-authored
   fixed TravelPlanner graph.
4. Search Steps must consume the typed JSON returned by the TravelPlanner tools
   and return structured candidate arrays, never raw/verbatim tables or prose.
   Preserve the canonical fields required downstream. Accommodation searches
   MUST pass required_nights, travelers, required_room_type, and
   required_house_rule so deterministic filtering happens before selection.
5. Keep collection parallel where independent. Keep candidate selection,
   assembly, validation, and repair sequential. The first assembly Step must
   select only members of the typed candidate Artifacts.
6. After assembly, a validator Agent Step MUST JSON-encode the complete
   `{idx, query, plan}` object and pass it as `plan_json` to
   `validate_travel_plan`. Its `validation_report` Artifact
   must be exactly the JSON returned by that tool; an LLM prose review does not
   count as validation.
7. If `validation_report.valid` is false, run one repair Step using only the
   original typed candidates and reported violations, then call
   `validate_travel_plan` again. A failed second validation is not a successful
   Workflow output and must produce the required empty-plan object.
8. The final successful Artifact is the validated complete `{idx, query, plan}`
   object. After `run_flow` returns, emit that object unchanged inside the
   requested markdown JSON code block. Do not reselect candidates, rewrite
   fields, or add narration in the outer session.

"""

WORKFLOW_TREATMENT_V6_TOKEN_EFFICIENT = """Please complete the task using workflow skill.

Workflow execution contract for this token-efficient dynamic-workflow trial:
1. Read `skills/workflow/SKILL.md` exactly once and
   `skills/workflow/grammar/FusionFlow.g4` at most once. Retain their contents in
   the current context; never read either file again, including through the
   `workflow-skill` alias.
2. The outer Dolphin session may parse the request, author the Workflow, call
   `run_flow`, and return its final Artifact. It MUST NOT call any TravelPlanner
   data tool itself. Only Agent Steps inside the Workflow may search flights,
   accommodations, restaurants, attractions, cities, or distances.
3. Author and run one concrete Workflow through `run_flow`. This remains a
   per-question dynamically authored Workflow; do not substitute a pre-authored
   fixed TravelPlanner graph. Author valid FusionFlow on the first attempt:
   declare every Step and Agent as separate constants; make the Workflow
   constant/owner exactly match the identifier after `workflow`; bind each Step
   with `step_executor(step) == agent`; and grant each tool with one scalar
   statement such as `allowed_tool(agent, "search_flights");`. Never combine
   Step and Agent types, attach `allowed_tool` to a Step, use equality/list
   syntax for it, or pass a list as its tool argument. Do NOT use `agent_config`
   in this trial; every Agent inherits the configured runtime model.
   (`agent_config` has four arguments, so a one-argument model shortcut is
   invalid.)
4. Search Steps must consume the typed JSON returned by the TravelPlanner tools
   and return structured candidate arrays, never raw/verbatim tables or prose.
   Preserve canonical fields required for membership, route, lodging, diversity,
   and budget checks. Accommodation searches MUST pass required_nights,
   travelers, required_room_type, and required_house_rule so deterministic
   filtering happens before selection.
5. For a state or multi-city request, no collector may lock in the final cities
   or route before cross-category viability is known. Discover the official
   cities once, then collect lodging, dining, attraction, and transport
   availability over a common city pool. Stage collectors when useful so
   transport candidates cover only cities with enough candidates in every
   required resource category, but return multiple feasible routes when
   possible; the single final planning Agent alone selects the cities and route.
   Never select a city with empty required lodging, restaurant, or attraction
   candidates, and never use `-` to conceal missing city support. Transport
   collectors MUST preserve each ground candidate's canonical `official_string`,
   and the final planning Agent MUST copy it verbatim into `transportation`;
   never summarize or reconstruct a ground-transport string. Every feasible
   route MUST contain a real candidate for every travel leg: origin to the first
   city, all inter-city legs, and the last city back to origin. For each leg,
   search the requested or appropriate primary mode and, when it is unavailable
   or alternatives are allowed, call `compute_distance` for BOTH self-driving
   and taxi; this requirement includes both origin endpoint legs. Never put a
   partial route in the feasible-routes Artifact, and never fabricate a missing
   endpoint leg during planning or repair.
6. Keep independent collection parallel. After collection, use exactly one
   final planning Agent Step. It consumes the original typed candidate Artifacts,
   selects candidates, assembles the complete `{idx, query, plan}` object, and
   calls `validate_travel_plan` with that exact object encoded as `plan_json`.
7. Treat the `query` string in the required output example below as an immutable
   canonical value. Copy it byte-for-byte into every Workflow input, Step
   instruction, validation payload, fallback object, and final output. NEVER
   append or prepend inferred metadata such as the case index or traveler count.
8. The complete object MUST have exactly the top-level keys `idx`, `query`, and
   `plan`. Every plan entry MUST have exactly these eight keys and no others:
   `day`, `current_city`, `transportation`, `breakfast`, `attraction`, `lunch`,
   `dinner`, and `accommodation`. The singular `day` key is mandatory; `days`
   and every other extra or misspelled key are forbidden.
9. If the first validation is valid, the final planning Agent MUST submit the
   exact validated object unchanged. If invalid, repair only reported fields
   using the original typed candidates, call `validate_travel_plan` exactly once
   more, and submit the repaired object only when valid. If the second
   validation is invalid, submit exactly
   `{"idx": <case idx>, "query": <exact original query>, "plan": []}`.
   Never submit placeholder or fabricated day entries in this failure object.
   A field-name repair MUST replace or rename the invalid field and delete the
   old key, not merely add the corrected key.
   Before each validation and before submission, enforce the exact key sets in
   item 8.
10. Do NOT create separate selection, assembly, validator, pass-through, or repair
   Agent Steps. Those boundaries replay the same candidate context and can alter
   an already validated object.
11. After `run_flow` returns, emit its final Artifact unchanged inside the
   requested markdown JSON code block. Do not reselect candidates, rewrite
   fields, or add narration in the outer session.

"""


def _prompt(case: Case, arm: str, output_dir: Path) -> str:
    """Render the exact Claude Code MCP prompt, plus the registered treatment."""

    del output_dir  # The prompt is intentionally independent of the host output path.
    try:
        idx: int | str = int(case.case_id)
    except ValueError:
        # Official TravelPlanner IDs are integers. Quoting keeps unit-test or
        # diagnostic non-numeric IDs valid JSON without changing official runs.
        idx = json.dumps(case.case_id)
    render_args = {
        "idx": idx,
        "query": case.question,
        "query_json": json.dumps(case.question),
    }
    if arm == SKILL_ARM:
        return CC_SKILL_PROMPT_TEMPLATE.format(**render_args)
    common = CLAUDE_CC_PROMPT_TEMPLATE.format(
        **render_args,
    )
    treatment = ""
    if arm == WORKFLOW_ARM:
        prompt_variant = os.environ.get("TRAVELPLANNER_PROMPT_VARIANT", "v1").casefold()
        if prompt_variant == CC_DYNAMIC_PROMPT_VARIANT:
            return CC_DYNAMIC_WORKFLOW_PROMPT_TEMPLATE.format(**render_args)
        treatment = (
            (
                WORKFLOW_TREATMENT_V6_TOKEN_EFFICIENT
                if prompt_variant == TOKEN_EFFICIENT_PROMPT_VARIANT
                else WORKFLOW_TREATMENT_V4
                if prompt_variant == "v4"
                else WORKFLOW_TREATMENT_V3
                if prompt_variant == "v3"
                else WORKFLOW_TREATMENT_V2
                if prompt_variant == "v2"
                else "Please complete the task using workflow skill.\n\n"
            )
        )
    return treatment + common


_DAY_KEYS = {
    "day",
    "current_city",
    "transportation",
    "breakfast",
    "attraction",
    "lunch",
    "dinner",
    "accommodation",
}
_FLIGHT_FORMAT = re.compile(
    r"^Flight Number: [^,]+, from .+ to .+, Departure Time: (?:[01][0-9]|2[0-3]):[0-5][0-9], "
    r"Arrival Time: (?:[01][0-9]|2[0-3]):[0-5][0-9]$"
)


def _resource_format_errors(value: str, *, field: str, day: int) -> list[str]:
    if value == "-":
        return []
    errors: list[str] = []
    if ";" in value:
        errors.append(f"day {day} {field} must not contain semicolons")
    if "cost:" in value.casefold() or "price:" in value.casefold():
        errors.append(f"day {day} {field} must not append cost or price")
    if "," not in value:
        errors.append(f"day {day} {field} must use '<Name>, <City>'")
    else:
        name, city = value.rsplit(",", 1)
        if not name.strip() or not city.strip():
            errors.append(f"day {day} {field} must contain a non-empty name and city")
    return errors


def _answer_contract_errors(answer: dict[str, Any], case: Case) -> list[str]:
    """Validate only the public output contract, never benchmark semantics."""

    errors: list[str] = []
    top_level_keys = {"idx", "query", "plan"}
    missing_top_level = sorted(top_level_keys - set(answer))
    extra = sorted(set(answer) - top_level_keys)
    if missing_top_level:
        errors.append(f"top-level object is missing keys {missing_top_level}")
    if extra:
        errors.append(f"top-level object has unexpected keys {extra}")
    expected_idx: int | str = int(case.case_id) if case.case_id.isdigit() else case.case_id
    if answer.get("idx") != expected_idx:
        errors.append(f"top-level idx must equal {expected_idx!r}")
    if answer.get("query") != case.question:
        errors.append("top-level query must exactly reproduce the user query")
    plan = answer.get("plan")
    if not isinstance(plan, list) or not plan:
        return [*errors, "plan must be a non-empty list"]

    expected_days: int | None = None
    raw_days = case.metadata.get("days")
    try:
        if raw_days not in (None, ""):
            expected_days = int(raw_days)
    except (TypeError, ValueError):
        pass
    if expected_days is not None and len(plan) != expected_days:
        errors.append(f"plan must contain exactly {expected_days} days, found {len(plan)}")

    origin = str(case.metadata.get("org", "")).strip()
    for ordinal, value in enumerate(plan, 1):
        if not isinstance(value, dict):
            errors.append(f"day {ordinal} must be an object")
            continue
        missing = sorted(_DAY_KEYS - set(value))
        unexpected = sorted(set(value) - _DAY_KEYS)
        if missing:
            errors.append(f"day {ordinal} is missing keys {missing}")
        if unexpected:
            errors.append(f"day {ordinal} has unexpected keys {unexpected}")
        if value.get("day") != ordinal:
            errors.append(f"day {ordinal} must have integer day={ordinal}")
        for field in _DAY_KEYS - {"day"}:
            if not isinstance(value.get(field), str):
                errors.append(f"day {ordinal} {field} must be a string")
        if any(not isinstance(value.get(field), str) for field in _DAY_KEYS - {"day"}):
            continue

        current_city = value["current_city"].strip()
        transportation = value["transportation"].strip()
        route_day = current_city.casefold().startswith("from ") and " to " in current_city.casefold()
        if ordinal == 1 and origin and not current_city.casefold().startswith(f"from {origin.casefold()} to "):
            errors.append(f"day 1 current_city must start with 'from {origin} to '")
        if ordinal == len(plan):
            if not route_day:
                errors.append("last day current_city must be the return-home route 'from <city> to <origin>'")
            elif origin and current_city.casefold().rsplit(" to ", 1)[-1].strip() != origin.casefold():
                errors.append(f"last day current_city must return to {origin}")
        if route_day and transportation == "-":
            errors.append(f"day {ordinal} has an inter-city route but transportation is '-'")
        if not route_day and transportation != "-":
            errors.append(f"day {ordinal} has transportation but current_city is not a route")

        if transportation != "-":
            lowered = transportation.casefold()
            if transportation.startswith("Flight Number:"):
                if _FLIGHT_FORMAT.fullmatch(transportation) is None:
                    errors.append(f"day {ordinal} flight transportation does not match the exact format")
            elif lowered.startswith("self-driving,") or lowered.startswith("taxi,"):
                required_fragments = (" from ", " to ", "duration:", "distance:", "cost:")
                if any(fragment not in lowered for fragment in required_fragments):
                    errors.append(f"day {ordinal} ground transportation must include route, duration, distance, and cost")
            else:
                errors.append(f"day {ordinal} transportation must be an official flight, self-driving, taxi, or '-'")

        for field in ("breakfast", "lunch", "dinner", "accommodation"):
            errors.extend(_resource_format_errors(value[field].strip(), field=field, day=ordinal))

        attraction = value["attraction"].strip()
        if attraction != "-":
            if not attraction.endswith(";"):
                errors.append(f"day {ordinal} attraction must end with a semicolon")
            entries = [entry.strip() for entry in attraction.split(";") if entry.strip()]
            if not entries:
                errors.append(f"day {ordinal} attraction must contain at least one entry or '-'")
            for entry in entries:
                if "," not in entry:
                    errors.append(f"day {ordinal} attraction entry must use '<Name>, <City>': {entry!r}")
                    continue
                name, city = entry.rsplit(",", 1)
                if not name.strip() or not city.strip():
                    errors.append(f"day {ordinal} attraction entry has an empty name or city")
    return errors


def _extract_answer(raw: str) -> dict[str, Any]:
    matches = list(_JSON_BLOCK.finditer(raw))
    candidates = [match.group(1).strip() for match in reversed(matches)]
    # Match the comparison Claude runner's fallback: if the model omitted the
    # markdown fence but did return a complete JSON object, preserve that plan.
    # This is deterministic extraction of the one returned response, not a new
    # model sample and not an evaluator-informed repair.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for content in candidates:
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            # Models occasionally put a short lead-in before the object inside
            # a fence. Decode the first complete JSON value without truncating
            # nested plan objects.
            try:
                value, _ = json.JSONDecoder().raw_decode(content[content.find("{") :])
            except (ValueError, json.JSONDecodeError):
                continue
        if isinstance(value, dict):
            if isinstance(value.get("plan"), list):
                return value
            answer = value.get("answer") or value.get("plan") or value.get("itinerary")
            if isinstance(answer, str):
                return {**value, "answer": answer}
    # Preserve an unstructured answer for manual inspection; it is not silently scored.
    return {"answer": raw.strip(), "plan": [], "parse_warning": "response was not valid result JSON"}


def _find_workflow(workspace: Path, case_id: str) -> Path | None:
    expected = workspace / "flows" / case_id / "workflow.workflow"
    if expected.is_file():
        return expected
    # Workflow skill outputs belong under ``flows/`` and commonly use the
    # FusionFlow ``.g4`` suffix.  Restricting discovery to that directory is
    # important: the copied skill also contains its parser grammar at
    # ``skills/workflow/grammar/FusionFlow.g4``, which is reference material,
    # not evidence that the agent generated or ran a workflow for this case.
    flows_root = workspace / "flows"
    if not flows_root.is_dir():
        return None
    found = sorted(
        [*flows_root.rglob("*.workflow"), *flows_root.rglob("*.g4")],
        key=lambda path: str(path),
    )
    return found[-1] if found else None


def run_arm(
    *,
    arm: str,
    cases: list[Case],
    root: Path,
    psi: Path,
    env: dict[str, str],
    source_agent: Path,
    reuse_session: bool,
    max_attempts: int = 3,
) -> list[RunRecord]:
    if arm not in EXPERIMENT_ARMS:
        raise ValueError(f"unknown experiment arm: {arm}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    env = dict(env)
    prompt_variant = os.environ.get("TRAVELPLANNER_PROMPT_VARIANT", "v1").casefold()
    if arm == WORKFLOW_ARM and prompt_variant in {"v4", TOKEN_EFFICIENT_PROMPT_VARIANT}:
        env["TRAVELPLANNER_WORKFLOW_STEP_ONLY"] = "1"
        env["TRAVELPLANNER_TYPED_TOOLS"] = "1"
        env["TRAVELPLANNER_READ_ONCE_REFERENCES"] = "1"
    else:
        env.pop("TRAVELPLANNER_WORKFLOW_STEP_ONLY", None)
        env.pop("TRAVELPLANNER_TYPED_TOOLS", None)
        env.pop("TRAVELPLANNER_READ_ONCE_REFERENCES", None)
    arm_root = root / arm.replace("-", "_")
    if arm_root.exists() and not reuse_session:
        # Preserve prior runs; a new timestamped root makes accidental mixing impossible.
        arm_root = root / f"{arm.replace('-', '_')}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    agent = source_agent
    agent = _prepare_agent(source_agent, arm_root / "agent", arm)
    workflow_skill_path = agent / "skills" / "workflow" / "SKILL.md"
    travelplanner_skill_path = agent / "skills" / "travelplanner" / "SKILL.md"
    (arm_root / "model-config.json").write_text(
        json.dumps(
            {
                "provider": env.get("PSI_AI_PROVIDER", ""),
                "model": env.get("PSI_AI_MODEL", ""),
                "base_url": env.get("PSI_AI_BASE_URL", ""),
                "api_key_configured": bool(env.get("PSI_AI_API_KEY")),
                "api_key_recorded": False,
                "arm": arm,
                "recorded_at": _utc_now(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    uses_cc_dynamic_template = arm == WORKFLOW_ARM and prompt_variant == CC_DYNAMIC_PROMPT_VARIANT
    uses_direct_skill_template = arm == SKILL_ARM
    effective_template = (
        CC_SKILL_PROMPT_TEMPLATE
        if uses_direct_skill_template
        else CC_DYNAMIC_WORKFLOW_PROMPT_TEMPLATE
        if uses_cc_dynamic_template
        else CLAUDE_CC_PROMPT_TEMPLATE
    )
    (arm_root / "prompt-config.json").write_text(
        json.dumps(
            {
                "common_template_source": (
                    "cc_dynamic_v1 direct-skill carrier adaptation"
                    if uses_direct_skill_template
                    else "user-provided CC Dynamic Workflow prompt, minimally adapted for Haitun"
                    if uses_cc_dynamic_template
                    else env.get("TRAVELPLANNER_PROMPT_TEMPLATE_SOURCE", "")
                ),
                "common_template_sha256": _sha256_text(effective_template),
                "query_source": env.get("TRAVELPLANNER_PROMPT_QUERY_SOURCE", ""),
                "arm": arm,
                "prompt_variant": (
                    f"{CC_DYNAMIC_PROMPT_VARIANT}_direct_skill"
                    if uses_direct_skill_template
                    else prompt_variant
                ),
                "treatment_prefix": (
                    (
                        (
                            WORKFLOW_TREATMENT_V6_TOKEN_EFFICIENT
                            if prompt_variant == TOKEN_EFFICIENT_PROMPT_VARIANT
                            else WORKFLOW_TREATMENT_V4
                            if prompt_variant == "v4"
                            else WORKFLOW_TREATMENT_V3
                            if prompt_variant == "v3"
                            else WORKFLOW_TREATMENT_V2
                            if prompt_variant == "v2"
                            else ""
                            if prompt_variant == CC_DYNAMIC_PROMPT_VARIANT
                            else "Please complete the task using workflow skill.\n\n"
                        )
                    )
                    if arm == WORKFLOW_ARM
                    else ""
                ),
                "adapter_changes": (
                    [
                        {"source": source, "target": target}
                        for source, target in CC_SKILL_CARRIER_ADAPTERS
                    ]
                    if uses_direct_skill_template
                    else [
                        "Map the CC workflow invocation to Haitun workflow skill + run_flow.",
                        "Map CC subagents to Haitun Workflow Agent Steps.",
                    ]
                    if uses_cc_dynamic_template
                    else []
                ),
                "retry_policy": "process_failures_only",
                "retry_prompt_policy": "verbatim_original_prompt",
                "validation_context_policy": (
                    "structured_reference_and_case_constraints"
                    if arm in (WORKFLOW_ARM, SKILL_ARM)
                    else "text_reference_only"
                ),
                "workflow_skill_sha256": (
                    _sha256_text(workflow_skill_path.read_text(encoding="utf-8"))
                    if workflow_skill_path.is_file()
                    else None
                ),
                "travelplanner_skill_sha256": (
                    _sha256_text(travelplanner_skill_path.read_text(encoding="utf-8"))
                    if travelplanner_skill_path.is_file()
                    else None
                ),
                "recorded_at": _utc_now(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    service = DolphinService(psi=psi, env=env, arm_root=arm_root, agent_source=agent, arm=arm)
    records: list[RunRecord] = []
    try:
        ai_start_audit: list[dict[str, Any]] = []
        for ai_start_attempt in range(1, max_attempts + 1):
            try:
                service.start_ai(attempt=ai_start_attempt)
            except Exception as exc:
                ai_start_audit.append(
                    {
                        "attempt": ai_start_attempt,
                        "started": False,
                        "process_failure": True,
                        "retry_eligible": ai_start_attempt < max_attempts,
                        "error": repr(exc),
                    }
                )
                service.stop_ai()
                if ai_start_attempt == max_attempts:
                    (arm_root / "ai-start-attempts.json").write_text(
                        json.dumps(ai_start_audit, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    raise
                time.sleep(2 ** (ai_start_attempt - 1))
            else:
                ai_start_audit.append(
                    {
                        "attempt": ai_start_attempt,
                        "started": True,
                        "process_failure": False,
                        "retry_eligible": False,
                        "error": None,
                    }
                )
                break
        (arm_root / "ai-start-attempts.json").write_text(
            json.dumps(ai_start_audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for case in cases:
            started_at = _utc_now()
            case_root = arm_root / "cases" / _safe_case_component(case.case_id)
            case_root.mkdir(parents=True, exist_ok=True)
            original_prompt = _prompt(case, arm, case_root)
            prompt = original_prompt
            raw = ""
            answer: dict[str, Any] = {"plan": []}
            elapsed = 0.0
            returncode = 1
            final_errors: list[str] = []
            attempt_audit: list[dict[str, Any]] = []
            attempt_count = 0
            for attempt in range(1, max_attempts + 1):
                attempt_count = attempt
                attempt_elapsed = 0.0
                raw = ""
                answer = {"plan": []}
                process_failure = False
                retry_reason: str | None = None
                attempt_workflow: Path | None = None
                try:
                    attempt_raw, attempt_elapsed, returncode = service.run_case(
                        case,
                        prompt,
                        attempt=attempt,
                        reuse_session=reuse_session,
                    )
                except Exception as exc:  # process launch/runtime failure
                    raw = ""
                    returncode = 1
                    process_failure = True
                    retry_reason = repr(exc)
                    final_errors = [retry_reason]
                else:
                    raw = attempt_raw
                    elapsed += attempt_elapsed
                    process_failure = returncode != 0
                    if process_failure:
                        retry_reason = _process_failure_reason(raw, returncode)
                        final_errors = [retry_reason]
                    else:
                        # A zero-exit model invocation is sampled exactly once. Parsing,
                        # output-contract, task-quality, and Workflow-adherence failures
                        # are diagnostics only and never make this answer eligible for a
                        # second or third model call.
                        try:
                            answer = _extract_answer(raw) if raw else {"plan": []}
                            final_errors = _answer_contract_errors(answer, case)
                        except Exception as exc:
                            # Local output processing is deliberately ineligible
                            # for retry: the first process-success response is fixed.
                            answer = {"plan": [], "parse_warning": repr(exc)}
                            final_errors = [f"local output processing error: {exc!r}"]
                if arm == WORKFLOW_ARM:
                    try:
                        attempt_workflow = _find_workflow(
                            arm_root
                            / "workspace"
                            / _safe_case_component(case.case_id)
                            / f"attempt-{attempt}",
                            case.case_id,
                        )
                    except Exception as exc:
                        final_errors.append(f"local Workflow audit error: {exc!r}")
                    if not process_failure and attempt_workflow is None:
                        final_errors.append("agent returned without a generated workflow source")

                attempt_raw_path = case_root / f"response.attempt-{attempt}.txt"
                attempt_answer_path = case_root / f"answer.attempt-{attempt}.json"
                attempt_raw_path.write_text(raw, encoding="utf-8")
                attempt_answer_path.write_text(
                    json.dumps(answer, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                attempt_audit.append(
                    {
                        "attempt": attempt,
                        "returncode": returncode,
                        "elapsed_seconds": attempt_elapsed,
                        "response_sha256": _sha256_text(raw),
                        "validation_errors": final_errors,
                        "process_failure": process_failure,
                        "retry_eligible": process_failure and attempt < max_attempts,
                        "retry_reason": retry_reason,
                        "workflow_detected": attempt_workflow is not None,
                    }
                )
                if not process_failure:
                    break
                if attempt < max_attempts:
                    service.stop_session()
                    # A process failure is retried with the byte-identical user
                    # message. Never add parser, validator, Workflow, or evaluator
                    # feedback to a later attempt.
                    prompt = original_prompt

            raw_path = case_root / "response.txt"
            answer_path = case_root / "answer.json"
            raw_path.write_text(raw, encoding="utf-8")
            answer_path.write_text(json.dumps(answer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (case_root / "attempts.json").write_text(
                json.dumps(attempt_audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            workflow = (
                _find_workflow(
                    arm_root
                    / "workspace"
                    / _safe_case_component(case.case_id)
                    / f"attempt-{attempt_count}",
                    case.case_id,
                )
                if arm == WORKFLOW_ARM
                else None
            )
            workflow_path: str | None = None
            if workflow is not None:
                target = case_root / workflow.name
                shutil.copy2(workflow, target)
                workflow_path = str(target.relative_to(root))
            error = "; ".join(final_errors) if final_errors else None
            plan = answer.get("plan") if isinstance(answer, dict) else None
            if returncode != 0:
                status = "failed_process"
            elif isinstance(plan, list) and plan:
                # Intention-to-treat: a delivered plan is scored even when the
                # local format checker reports issues or auto did not adhere to
                # the Workflow treatment. Those facts remain in the audit files.
                status = "completed"
            else:
                status = "failed_output_contract"
            records.append(
                RunRecord(
                    arm,
                    case.case_id,
                    status,
                    started_at,
                    elapsed,
                    str(raw_path.relative_to(root)),
                    str(answer_path.relative_to(root)),
                    workflow_path,
                    _sha256_text(raw),
                    error,
                    attempt_count,
                    final_errors or None,
                )
            )
            if not reuse_session:
                service.stop_session()
                service.env.pop("TRAVELPLANNER_CASE_REFERENCE", None)
    finally:
        service.close()
    (arm_root / "records.json").write_text(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (arm_root / "predictions.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            answer = json.loads((root / record.answer_path).read_text(encoding="utf-8"))
            plan = answer.get("plan", [])
            # The official evaluator, not this runner's local validator or the
            # Workflow-adherence check, decides whether a delivered plan passes.
            evaluator_plan = plan if isinstance(plan, list) else []
            handle.write(json.dumps({"plan": evaluator_plan}, ensure_ascii=False) + "\n")
    return records


def _check_runtime(psi: Path, source_agent: Path, database: Path, env: dict[str, str]) -> None:
    required = ("PSI_AI_PROVIDER", "PSI_AI_MODEL", "PSI_AI_API_KEY", "PSI_AI_BASE_URL")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError("Missing model configuration: " + ", ".join(missing) + ". Set them in the environment or .secrets file.")
    if not psi.is_file():
        raise FileNotFoundError(f"psi-agent executable not found: {psi}")
    if not source_agent.is_dir():
        raise FileNotFoundError(f"Haitun agent package not found: {source_agent}")
    reference_roots = [database, database.parent]
    has_reference = any(any(root.glob("*_ref_info.jsonl")) for root in reference_roots if root.is_dir())
    if not database.is_dir() or not has_reference:
        raise FileNotFoundError(
            f"TravelPlanner database directory must contain *_ref_info.jsonl files in itself or its parent: {database}"
        )
    background = database / "background"
    if not background.is_dir() and (database / "database" / "background").is_dir():
        background = database / "database" / "background"
    if not background.is_dir():
        raise FileNotFoundError(
            f"TravelPlanner database is incomplete: missing {database / 'background'}; download the official archive"
        )


def _records_summary(records: list[RunRecord]) -> dict[str, Any]:
    completed = sum(r.status == "completed" for r in records)
    return {
        "completed_requests": completed,
        "failed_requests": len(records) - completed,
        "request_completion_rate": completed / len(records) if records else 0.0,
        "mean_elapsed_seconds_completed": (
            sum(r.elapsed_seconds for r in records if r.status == "completed") / completed if completed else None
        ),
        "official_travelplanner_metrics": "not computed by this runner; use --evaluator-cmd",
    }


def _merge_shards(
    *,
    manifest: Path,
    shards: list[list[str]],
    output_root: Path,
    arm: str = WORKFLOW_ARM,
) -> dict[str, Any]:
    """Merge explicitly ranged shards without choosing among duplicate answers."""

    cases = read_manifest(manifest)
    arm_name = arm.replace("-", "_")
    selected: dict[str, tuple[Path, Path]] = {}
    covered: set[int] = set()
    source_records: dict[Path, dict[str, RunRecord]] = {}
    source_arm_roots: list[Path] = []
    for raw_root, range_value in shards:
        root = Path(raw_root)
        selected_cases = select_case_range(cases, range_value)
        start, end = (int(part) for part in range_value.split(":", 1))
        expected_ordinals = set(range(start, end + 1))
        if covered & expected_ordinals:
            raise ValueError(f"overlapping shard range: {range_value}")
        covered.update(expected_ordinals)
        arm_root = root / arm_name
        source_arm_roots.append(arm_root)
        records_path = arm_root / "records.json"
        if records_path.is_file():
            rows = json.loads(records_path.read_text(encoding="utf-8"))
            source_records[root] = {row["case_id"]: RunRecord(**row) for row in rows}
        for case in selected_cases:
            if case.case_id in selected:
                raise ValueError(f"duplicate case id in shard ranges: {case.case_id}")
            case_root = arm_root / "cases" / _safe_case_component(case.case_id)
            if not (case_root / "answer.json").is_file():
                raise FileNotFoundError(f"missing answer for case {case.case_id}: {case_root / 'answer.json'}")
            selected[case.case_id] = (root, case_root)
    if covered != set(range(1, len(cases) + 1)):
        missing = sorted(set(range(1, len(cases) + 1)) - covered)
        raise ValueError(f"shard ranges must cover the full manifest; missing ordinals start at {missing[:10]}")
    output_arm = output_root / arm_name
    if output_arm.exists() and any(output_arm.iterdir()):
        raise FileExistsError(f"merge arm output is not empty: {output_arm}")
    output_cases = output_arm / "cases"
    output_cases.mkdir(parents=True, exist_ok=True)
    for config_name in ("prompt-config.json", "model-config.json"):
        config_paths = [arm_root / config_name for arm_root in source_arm_roots]
        existing = [path for path in config_paths if path.is_file()]
        if existing and len(existing) != len(config_paths):
            missing = [str(path) for path in config_paths if not path.is_file()]
            raise FileNotFoundError(
                f"{config_name} must exist in every shard when present; missing={missing}"
            )
        if not existing:
            continue
        configs = [json.loads(path.read_text(encoding="utf-8")) for path in existing]
        normalized = [
            {key: value for key, value in config.items() if key != "recorded_at"}
            for config in configs
        ]
        if any(config != normalized[0] for config in normalized[1:]):
            raise ValueError(f"shard {config_name} values differ after excluding recorded_at")
        shutil.copy2(existing[0], output_arm / config_name)
    merged: list[RunRecord] = []
    for case in cases:
        source_root, source_case = selected[case.case_id]
        target_case = output_cases / _safe_case_component(case.case_id)
        target_case.mkdir(parents=True, exist_ok=True)
        fixed_artifacts = (
            "answer.json",
            "response.txt",
            "reference_information.json",
            "reference_information.structured.json",
            "case_constraints.json",
            "workflow.workflow",
            "attempts.json",
        )
        attempt_artifacts = (
            *source_case.glob("answer.attempt-*.json"),
            *source_case.glob("response.attempt-*.txt"),
            *source_case.glob("session.attempt-*.log"),
            *source_case.glob("session.attempt-*.yml"),
        )
        for name in fixed_artifacts:
            source = source_case / name
            if source.is_file():
                shutil.copy2(source, target_case / name)
        for source in attempt_artifacts:
            shutil.copy2(source, target_case / source.name)
        source_record = source_records.get(source_root, {}).get(case.case_id)
        answer = json.loads((target_case / "answer.json").read_text(encoding="utf-8"))
        workflow = target_case / "workflow.workflow"
        if source_record is not None:
            status = source_record.status
            started_at = source_record.started_at
            elapsed = source_record.elapsed_seconds
            error = source_record.error
            stdout_sha256 = source_record.stdout_sha256
            attempt_count = source_record.attempt_count
            validation_errors = source_record.validation_errors
        else:
            plan = answer.get("plan") if isinstance(answer, dict) else None
            status = "completed" if isinstance(plan, list) and plan else "failed_output_contract"
            started_at = datetime.fromtimestamp((target_case / "answer.json").stat().st_mtime, UTC).isoformat()
            elapsed = 0.0
            error = answer.get("error") if isinstance(answer, dict) else "invalid answer object"
            stdout_sha256 = _sha256_text((target_case / "response.txt").read_text(encoding="utf-8")) if (target_case / "response.txt").is_file() else ""
            attempt_count = 1
            validation_errors = None
        merged.append(RunRecord(
            arm, case.case_id, status, started_at, elapsed,
            str((target_case / "response.txt").relative_to(output_root)),
            str((target_case / "answer.json").relative_to(output_root)),
            str(workflow.relative_to(output_root)) if workflow.is_file() else None,
            stdout_sha256, error, attempt_count, validation_errors,
        ))
    (output_arm / "records.json").write_text(json.dumps([asdict(record) for record in merged], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_arm / "predictions.jsonl").open("w", encoding="utf-8") as handle:
        for record in merged:
            answer = json.loads((output_root / record.answer_path).read_text(encoding="utf-8"))
            plan = answer.get("plan", [])
            evaluator_plan = plan if isinstance(plan, list) else []
            handle.write(json.dumps({"plan": evaluator_plan}, ensure_ascii=False) + "\n")
    return _records_summary(merged)


def _run_evaluator(command: str, *, root: Path, manifest: Path, arm: str) -> None:
    arm_name = arm.replace("-", "_")
    candidates = [root / arm_name, *sorted(root.glob(f"{arm_name}-*"))]
    arm_root = next((path for path in reversed(candidates) if (path / "predictions.jsonl").is_file()), root / arm_name)
    output = arm_root / "official-evaluator.json"
    rendered = command.format(manifest=str(manifest), predictions=str(arm_root / "predictions.jsonl"), output=str(output), arm=arm)
    result = subprocess.run(rendered, shell=True, cwd=REPO_ROOT, env=os.environ.copy(), text=True, capture_output=True, check=False)
    (output.with_suffix(".stdout.txt")).write_text(result.stdout, encoding="utf-8")
    (output.with_suffix(".stderr.txt")).write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"official evaluator failed for {arm} with exit code {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="normalize official benchmark JSON/JSONL into a manifest")
    prepare.add_argument("--data", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, default=ROOT / "cases.jsonl")
    run = sub.add_parser("run", help="run one or both experiment arms")
    run.add_argument("--manifest", type=Path, default=ROOT / "cases.jsonl")
    run.add_argument(
        "--prompt-queries",
        type=Path,
        help=(
            "NON-OFFICIAL comparison mode: overlay only idx/query from another JSONL; "
            "result/plan fields are ignored"
        ),
    )
    run.add_argument(
        "--arm",
        choices=(*EXPERIMENT_ARMS, "both", "workflow-vs-skill"),
        default="both",
        help=(
            "experiment arm; 'both' retains the historical auto-workflow/no-workflow pair, "
            "while 'workflow-vs-skill' runs the carrier-equivalence pair"
        ),
    )
    run.add_argument("--limit", type=int, default=0)
    run.add_argument(
        "--case-range",
        default="",
        metavar="START:END",
        help="run an inclusive 1-based manifest range in an isolated run root",
    )
    run.add_argument("--reuse-session", action="store_true", help="reuse one session; default is fresh session per question")
    run.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help=(
            "maximum process attempts; only launch/runtime timeout/nonzero-exit failures retry, "
            "never parsing, output-contract, task-quality, Workflow, or evaluator failures"
        ),
    )
    run.add_argument(
        "--parallel-arms",
        action="store_true",
        help="run the selected two-arm comparison concurrently",
    )
    run.add_argument("--psi", type=Path, default=DEFAULT_PSI)
    run.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    run.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    run.add_argument("--database", type=Path, required=True, help="directory containing the official *_ref_info.jsonl files")
    run.add_argument("--set-type", choices=("validation", "test"), default="validation")
    run.add_argument("--run-root", type=Path, default=RUN_ROOT)
    run.add_argument("--evaluator-cmd", default="", help="shell template with {manifest}, {predictions}, {output}, {arm}")
    score = sub.add_parser("summary", help="summarize saved request records without rerunning the model")
    score.add_argument("--run-root", type=Path, default=RUN_ROOT)
    merge = sub.add_parser("merge", help="merge explicitly ranged shards for one experiment arm")
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument("--arm", choices=EXPERIMENT_ARMS, default=WORKFLOW_ARM)
    merge.add_argument("--shard", nargs=2, action="append", required=True, metavar=("RUN_ROOT", "START:END"), help="source root and inclusive manifest range; repeat for every shard")
    merge.add_argument("--output-root", type=Path, required=True)
    doctor = sub.add_parser("doctor", help="check the local Dolphin runtime without calling the model")
    doctor.add_argument("--psi", type=Path, default=DEFAULT_PSI)
    doctor.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    doctor.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    args = parser.parse_args()

    if args.command == "prepare":
        cases = load_cases(args.data)
        write_manifest(cases, args.manifest)
        print(json.dumps({"manifest": str(args.manifest), "cases": len(cases)}, ensure_ascii=False))
        return 0
    if args.command == "summary":
        output: dict[str, Any] = {}
        for path in sorted(args.run_root.glob("*/records.json")):
            records = [RunRecord(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
            output[path.parent.name] = _records_summary(records)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    if args.command == "merge":
        summary = _merge_shards(
            manifest=args.manifest,
            shards=args.shard,
            output_root=args.output_root,
            arm=args.arm,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        env = dict(os.environ)
        env.update({key: value for key, value in _load_env_file(args.secrets).items() if key.startswith("PSI_AI_")})
        report = {
            "psi_agent": str(args.psi),
            "psi_agent_exists": args.psi.is_file(),
            "haitun_agent": str(args.agent),
            "haitun_agent_exists": args.agent.is_dir(),
            "model_variables": {key: bool(env.get(key)) for key in ("PSI_AI_PROVIDER", "PSI_AI_MODEL", "PSI_AI_API_KEY", "PSI_AI_BASE_URL")},
            "model_identity": {
                "provider": env.get("PSI_AI_PROVIDER", ""),
                "model": env.get("PSI_AI_MODEL", ""),
                "base_url": env.get("PSI_AI_BASE_URL", ""),
                "api_key_configured": bool(env.get("PSI_AI_API_KEY")),
                "api_key_recorded": False,
            },
            "travelplanner_data": "provide with --data to prepare",
            "official_evaluator": "provide with --evaluator-cmd",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    cases = read_manifest(args.manifest)
    if args.prompt_queries is not None:
        cases = align_cases_to_prompt_queries(cases, args.prompt_queries)
    if args.case_range and args.limit:
        raise ValueError("--case-range and --limit cannot be used together")
    if args.case_range:
        cases = select_case_range(cases, args.case_range)
    if args.limit:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("no cases selected")
    env = dict(os.environ)
    env.update({key: value for key, value in _load_env_file(args.secrets).items() if key.startswith("PSI_AI_")})
    database = args.database.resolve()
    _check_runtime(args.psi, args.agent, database, env)
    env["TRAVELPLANNER_DATABASE"] = str(database)
    env["TRAVELPLANNER_SET_TYPE"] = args.set_type
    env["TRAVELPLANNER_PROMPT_TEMPLATE_SOURCE"] = str(
        (REPO_ROOT / "travelplanner-mcp-env" / "scripts" / "eval_claude_code_travelplanner.py").resolve()
    )
    env["TRAVELPLANNER_PROMPT_QUERY_SOURCE"] = (
        str(args.prompt_queries.resolve()) if args.prompt_queries is not None else str(args.manifest.resolve())
    )
    args.run_root.mkdir(parents=True, exist_ok=True)
    if args.arm == "both":
        arms = (WORKFLOW_ARM, NO_WORKFLOW_ARM)
    elif args.arm == "workflow-vs-skill":
        arms = (WORKFLOW_ARM, SKILL_ARM)
        prompt_variant = os.environ.get("TRAVELPLANNER_PROMPT_VARIANT", "").casefold()
        if prompt_variant != CC_DYNAMIC_PROMPT_VARIANT:
            raise ValueError(
                "workflow-vs-skill compares the latest cc_dynamic_v1 protocol; set "
                "TRAVELPLANNER_PROMPT_VARIANT=cc_dynamic_v1"
            )
    else:
        arms = (args.arm,)
    all_summaries: dict[str, Any] = {}
    if args.parallel_arms and len(arms) == 2:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="travelplanner-arm") as pool:
            pending = {
                arm: pool.submit(
                    run_arm,
                    arm=arm,
                    cases=cases,
                    root=args.run_root,
                    psi=args.psi,
                    env=env,
                    source_agent=args.agent,
                    reuse_session=args.reuse_session,
                    max_attempts=args.max_attempts,
                )
                for arm in arms
            }
            arm_records = {arm: pending[arm].result() for arm in arms}
        for arm in arms:
            all_summaries[arm] = _records_summary(arm_records[arm])
            if args.evaluator_cmd:
                _run_evaluator(args.evaluator_cmd, root=args.run_root, manifest=args.manifest, arm=arm)
    else:
        for arm in arms:
            records = run_arm(
                arm=arm,
                cases=cases,
                root=args.run_root,
                psi=args.psi,
                env=env,
                source_agent=args.agent,
                reuse_session=args.reuse_session,
                max_attempts=args.max_attempts,
            )
            all_summaries[arm] = _records_summary(records)
            if args.evaluator_cmd:
                _run_evaluator(args.evaluator_cmd, root=args.run_root, manifest=args.manifest, arm=arm)
    print(json.dumps(all_summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, BrokenPipeError):
        raise SystemExit(130)
