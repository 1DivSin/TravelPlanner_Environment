#!/usr/bin/env python3
"""Run the complete frozen 30-case sample with neutral experiment plumbing."""

from __future__ import annotations

import argparse
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
from typing import Any


ROOT = Path(__file__).resolve().parent
TRAVELPLANNER_ROOT = ROOT.parent
WORKFLOW_ROOT = TRAVELPLANNER_ROOT.parent
ASSETS = ROOT / "assets"
MANIFEST = ASSETS / "manifest.jsonl"
PROMPT_QUERIES = ASSETS / "prompt-queries.jsonl"
CITY_STATE_INDEX = ASSETS / "citySet_with_states.txt"
PROMPT_TEMPLATE = ROOT / "prompt_template.txt"
TASK_TOOLS = ROOT / "task_tools"
DEFAULT_SOURCE = TRAVELPLANNER_ROOT / "psi-agent"
# The source snapshot is kept inside the experiment root, while its ignored
# virtual environment is shared with the sibling psi-agent checkout.
DEFAULT_PSI = WORKFLOW_ROOT / "psi-agent" / ".venv" / "bin" / "psi-agent"
EXPECTED_SOURCE_COMMIT = "6d22e72b31c28c1fb935f89bf21894c5853de059"
EXPECTED_BASE_COMMIT = EXPECTED_SOURCE_COMMIT
EXPECTED_CASE_IDS = (
    "1", "11", "14", "17", "28", "33", "38", "41", "46", "48",
    "70", "72", "77", "81", "83", "100", "110", "113", "116", "118",
    "123", "124", "138", "144", "146", "151", "159", "161", "162", "163",
)
EXPECTED_MANIFEST_SHA256 = "a55b1ba56722c4fae7f020b88a95bc18b171a278088163c4db22b2f804690045"
EXPECTED_QUERIES_SHA256 = "e026647b205dedce0d9ebb2f2a659c2e710ba9288b51704037b2d2cb7c61a9b4"
EXPECTED_TEMPLATE_SHA256 = "08216d1b8ac0ddac402f11c8d9d3c9333e69a5345c9f92f706029ec8b4aac346"
EXPECTED_SKILL_SHA256 = "2b02fcd80ff62ee4eeb4a7329e77f75e7b07e508cd958a376686cdad4ded0fa6"
EXPECTED_GRAMMAR_SHA256 = "72a7765b626a1447d0557bf8fcef1aa5e38167249c4af57daec93e76242c4328"
EXPECTED_RUNNER_SHA256 = "3d0593e89f26d29435c7457e4f17e87000fed99f8c9b85260f9516843ced182f"
TREATMENT_PREFIX = "Please complete the task using workflow skill.\n\n"
JSON_BLOCK = re.compile(r"```json[ \t]*\n(.*?)\s*```", re.S | re.I)
SESSION_FAILURE_MARKERS = (
    "Session request incomplete:",
    "Session request completed without a terminal result",
)
SESSION_FAILURE_EXIT_CODE = 75
SESSION_FAILURE_RESPONSE_MARKER = "[runner session infrastructure failure]"


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    question: str
    source_sha256: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunRecord:
    case_id: str
    status: str
    started_at: str
    elapsed_seconds: float
    raw_response_path: str
    answer_path: str
    workflow_path: str | None
    response_sha256: str
    prompt_sha256: str
    attempt_count: int
    process_error: str | None
    parse_warning: str | None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:96] or "case"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_env_file(path: Path, visited: set[Path] | None = None) -> dict[str, str]:
    """Load KEY=VALUE lines and explicit source includes without shell execution."""

    result: dict[str, str] = {}
    seen = visited if visited is not None else set()
    resolved = path.resolve()
    if resolved in seen or not resolved.is_file():
        return result
    seen.add(resolved)
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("source ", ". ")):
            include = Path(line.split(None, 1)[1].strip())
            if not include.is_absolute():
                include = resolved.parent / include
            result.update(load_env_file(include, seen))
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
        result[key] = value
    return result


def load_cases() -> list[Case]:
    if sha256_file(MANIFEST) != EXPECTED_MANIFEST_SHA256:
        raise ValueError("frozen manifest hash mismatch")
    if sha256_file(PROMPT_QUERIES) != EXPECTED_QUERIES_SHA256:
        raise ValueError("frozen prompt query hash mismatch")
    cases: list[Case] = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cases.append(
            Case(
                case_id=str(row["case_id"]),
                question=str(row["question"]),
                source_sha256=str(row["source_sha256"]),
                metadata=dict(row.get("metadata", {})),
            )
        )
    query_rows = [
        json.loads(line)
        for line in PROMPT_QUERIES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if tuple(case.case_id for case in cases) != EXPECTED_CASE_IDS:
        raise ValueError("frozen manifest case IDs or order changed")
    if len(query_rows) != len(cases):
        raise ValueError("prompt query count does not match manifest")
    aligned: list[Case] = []
    for position, (case, query_row) in enumerate(zip(cases, query_rows, strict=True), 1):
        if str(query_row.get("idx")) != case.case_id:
            raise ValueError(f"prompt query ID mismatch at position {position}")
        query = query_row.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"prompt query is empty at position {position}")
        aligned.append(Case(case.case_id, query, case.source_sha256, case.metadata))
    return aligned


def render_prompt(case: Case) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    if sha256_text(template) != EXPECTED_TEMPLATE_SHA256:
        raise ValueError("frozen v1 prompt template hash mismatch")
    return TREATMENT_PREFIX + template.format(
        idx=int(case.case_id),
        query=case.question,
        query_json=json.dumps(case.question),
    )


def git_output(source: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_agent(source: Path, destination: Path) -> None:
    """Copy only the general method runtime and the registered task tools."""

    source = source.resolve()
    head = git_output(source, "rev-parse", "HEAD")
    if head != EXPECTED_SOURCE_COMMIT:
        raise ValueError(f"source worktree must be exact baseline commit {EXPECTED_SOURCE_COMMIT}, got {head}")
    if git_output(source, "status", "--porcelain"):
        raise ValueError("source worktree must be clean")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"agent destination must be new or empty: {destination}")
    (destination / "tools").mkdir(parents=True)
    (destination / "skills").mkdir(parents=True)
    source_agent = source / "examples" / "haitun-workspace"
    shutil.copytree(source_agent / "systems", destination / "systems")
    shutil.copytree(source_agent / "skills" / "workflow", destination / "skills" / "workflow")
    for name in ("_runtime_paths.py", "read.py", "write.py", "run_flow.py"):
        shutil.copy2(source_agent / "tools" / name, destination / "tools" / name)
    for path in sorted(TASK_TOOLS.glob("*.py")):
        shutil.copy2(path, destination / "tools" / path.name)
    skill = destination / "skills" / "workflow" / "SKILL.md"
    runner = destination / "skills" / "workflow" / "fusion_flow" / "workflow_runner.py"
    if sha256_file(skill) != EXPECTED_SKILL_SHA256:
        raise ValueError("copied Workflow Skill differs from the baseline commit")
    grammar = destination / "skills" / "workflow" / "grammar" / "FusionFlow.g4"
    if sha256_file(grammar) != EXPECTED_GRAMMAR_SHA256:
        raise ValueError("copied Workflow grammar differs from the baseline commit")
    if sha256_file(runner) != EXPECTED_RUNNER_SHA256:
        raise ValueError("copied Workflow runtime differs from the baseline commit")


def prepare_workspace(agent: Path, workspace: Path) -> None:
    """Expose the immutable authoring references inside one isolated workspace."""

    target = workspace / "skills" / "workflow"
    target.mkdir(parents=True, exist_ok=True)
    source = agent / "skills" / "workflow"
    shutil.copy2(source / "SKILL.md", target / "SKILL.md")
    shutil.copy2(source / "README.md", target / "README.md")
    shutil.copytree(source / "grammar", target / "grammar")


def write_ai_config(path: Path, socket: Path) -> None:
    path.write_text(
        "- type: ai\n"
        f"  session_socket: {json.dumps(str(socket))}\n"
        "  provider: \"\"\n"
        "  model: \"\"\n"
        "  api_key: \"\"\n"
        "  base_url: \"\"\n",
        encoding="utf-8",
    )


def write_session_config(
    path: Path,
    *,
    socket: Path,
    ai_socket: Path,
    workspace: Path,
    agent: Path,
    appdata: Path,
    session_id: str,
) -> None:
    path.write_text(
        "- type: session\n"
        f"  ai_socket: {json.dumps(str(ai_socket))}\n"
        f"  channel_socket: {json.dumps(str(socket))}\n"
        f"  workspace: {json.dumps(str(workspace))}\n"
        f"  agent: {json.dumps(str(agent))}\n"
        f"  appdata: {json.dumps(str(appdata))}\n"
        f"  session_id: {json.dumps(session_id)}\n"
        "  max_tool_rounds: 256\n",
        encoding="utf-8",
    )


def wait_for_socket(socket: Path, process: subprocess.Popen[str], timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket.exists():
            return
        if process.poll() is not None:
            raise RuntimeError(f"psi-agent exited during startup with code {process.returncode}")
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for socket: {socket}")


def terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
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


def session_failure(log_text: str) -> str | None:
    for line in reversed(log_text.splitlines()):
        if any(marker in line for marker in SESSION_FAILURE_MARKERS):
            return line.split(" - ", 1)[-1].strip()
    return None


def extract_answer(raw: str) -> tuple[dict[str, Any], str | None]:
    candidates = [match.group(1).strip() for match in reversed(list(JSON_BLOCK.finditer(raw)))]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for content in candidates:
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            object_start = content.find("{")
            if object_start == -1:
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(content[object_start:])
            except (ValueError, json.JSONDecodeError):
                continue
        if isinstance(value, dict) and isinstance(value.get("plan"), list):
            return value, None
    return {
        "answer": raw.strip(),
        "plan": [],
        "parse_warning": "response was not valid result JSON",
    }, "response was not valid result JSON"


def find_workflow(workspace: Path) -> Path | None:
    flows = workspace / "flows"
    if not flows.is_dir():
        return None
    candidates = sorted(
        [*flows.rglob("*.workflow"), *flows.rglob("*.g4")],
        key=lambda path: str(path),
    )
    return candidates[-1] if candidates else None


class ShardService:
    def __init__(
        self,
        *,
        psi: Path,
        env: dict[str, str],
        shard_root: Path,
        agent: Path,
    ) -> None:
        self.psi = psi
        self.env = dict(env)
        self.shard_root = shard_root
        self.agent = agent
        self.socket_root = Path(tempfile.mkdtemp(prefix="clean-workflow-psi-"))
        self.ai_socket = self.socket_root / "ai.sock"
        self.ai_process: subprocess.Popen[str] | None = None
        self.session_process: subprocess.Popen[str] | None = None

    def start_ai(self, attempt: int) -> None:
        self.shard_root.mkdir(parents=True, exist_ok=True)
        self.ai_socket.unlink(missing_ok=True)
        config = self.shard_root / "ai.yml"
        write_ai_config(config, self.ai_socket)
        log_path = self.shard_root / "ai.log"
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[runner] AI process start attempt {attempt}\n")
            log.flush()
            self.ai_process = subprocess.Popen(
                [str(self.psi), "run", str(config)],
                cwd=ROOT,
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        assert self.ai_process is not None
        wait_for_socket(self.ai_socket, self.ai_process)

    def stop_session(self) -> None:
        terminate(self.session_process)
        self.session_process = None
        for socket in self.socket_root.glob("session-*.sock"):
            socket.unlink(missing_ok=True)

    def close(self) -> None:
        self.stop_session()
        terminate(self.ai_process)
        self.ai_process = None
        self.ai_socket.unlink(missing_ok=True)
        shutil.rmtree(self.socket_root, ignore_errors=True)

    def invoke(
        self,
        *,
        case: Case,
        prompt: str,
        attempt: int,
        case_root: Path,
    ) -> tuple[str, float, int, Path, Path]:
        self.stop_session()
        suffix = f"attempt-{attempt}"
        workspace = self.shard_root / "workspaces" / safe_component(case.case_id) / suffix
        prepare_workspace(self.agent, workspace)
        appdata = case_root / f"appdata-{suffix}"
        reference_path = case_root / "reference_information.json"
        reference = case.metadata.get("reference_information")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"case {case.case_id} has no official reference_information")
        reference_path.write_text(reference, encoding="utf-8")
        socket = self.socket_root / f"session-{safe_component(case.case_id)}-{attempt}.sock"
        config = case_root / f"session.{suffix}.yml"
        log_path = case_root / f"session.{suffix}.log"
        write_session_config(
            config,
            socket=socket,
            ai_socket=self.ai_socket,
            workspace=workspace,
            agent=self.agent,
            appdata=appdata,
            session_id=f"clean-workflow-{case.case_id}-{suffix}",
        )
        case_env = dict(self.env)
        case_env["CLEAN_CASE_REFERENCE"] = str(reference_path.resolve())
        case_env["CLEAN_CITY_STATE_INDEX"] = str(CITY_STATE_INDEX.resolve())
        with log_path.open("w", encoding="utf-8") as log:
            self.session_process = subprocess.Popen(
                [str(self.psi), "run", str(config)],
                cwd=ROOT,
                env=case_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        assert self.session_process is not None
        wait_for_socket(socket, self.session_process)
        started = time.monotonic()
        completed = subprocess.run(
            [
                str(self.psi), "channel", "cli",
                "--session-socket", str(socket),
                "--message", "-",
            ],
            input=prompt,
            cwd=ROOT,
            env=case_env,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        elapsed = time.monotonic() - started
        response = completed.stdout
        returncode = completed.returncode
        failure = session_failure(log_path.read_text(encoding="utf-8", errors="replace"))
        if returncode == 0 and failure is not None:
            returncode = SESSION_FAILURE_EXIT_CODE
            response += f"\n{SESSION_FAILURE_RESPONSE_MARKER}\n{failure}\n"
        if completed.returncode != 0 and completed.stderr:
            response += f"\n[runner stderr]\n{completed.stderr}"
        return response, elapsed, returncode, workspace, log_path


def process_failure_reason(response: str, returncode: int) -> str:
    marker = f"{SESSION_FAILURE_RESPONSE_MARKER}\n"
    if marker in response:
        value = response.rsplit(marker, 1)[-1].splitlines()[0].strip()
        if value:
            return value
    return f"channel exit code {returncode}"


def run_case(
    service: ShardService,
    case: Case,
    *,
    run_root: Path,
    max_attempts: int,
) -> RunRecord:
    case_root = service.shard_root / "cases" / safe_component(case.case_id)
    case_root.mkdir(parents=True, exist_ok=True)
    prompt = render_prompt(case)
    prompt_hash = sha256_text(prompt)
    (case_root / "prompt.txt").write_text(prompt, encoding="utf-8")
    started_at = utc_now()
    total_elapsed = 0.0
    attempt_rows: list[dict[str, Any]] = []
    final_raw = ""
    final_answer: dict[str, Any] = {"plan": []}
    final_parse_warning: str | None = None
    final_process_error: str | None = None
    final_workflow: Path | None = None
    final_returncode = 1
    attempt_count = 0

    for attempt in range(1, max_attempts + 1):
        attempt_count = attempt
        attempt_started = utc_now()
        try:
            raw, elapsed, returncode, workspace, _ = service.invoke(
                case=case,
                prompt=prompt,
                attempt=attempt,
                case_root=case_root,
            )
        except Exception as exc:
            raw = ""
            elapsed = 0.0
            returncode = 1
            workspace = service.shard_root / "workspaces" / safe_component(case.case_id) / f"attempt-{attempt}"
            process_error = repr(exc)
        else:
            process_error = None if returncode == 0 else process_failure_reason(raw, returncode)
        total_elapsed += elapsed
        answer, parse_warning = extract_answer(raw) if returncode == 0 else ({"plan": []}, None)
        workflow = find_workflow(workspace)
        workflow_relative: str | None = None
        if workflow is not None:
            target = case_root / f"workflow.attempt-{attempt}{workflow.suffix}"
            shutil.copy2(workflow, target)
            workflow_relative = str(target.relative_to(run_root))
        response_path = case_root / f"response.attempt-{attempt}.txt"
        answer_path = case_root / f"answer.attempt-{attempt}.json"
        response_path.write_text(raw, encoding="utf-8")
        write_json(answer_path, answer)
        attempt_rows.append(
            {
                "attempt": attempt,
                "started_at": attempt_started,
                "elapsed_seconds": elapsed,
                "returncode": returncode,
                "process_failure": returncode != 0,
                "retry_eligible": returncode != 0 and attempt < max_attempts,
                "retry_reason": process_error,
                "prompt_sha256": prompt_hash,
                "response_sha256": sha256_text(raw),
                "workflow_detected": workflow is not None,
                "workflow_path": workflow_relative,
                "parse_warning": parse_warning,
            }
        )
        final_raw = raw
        final_answer = answer
        final_parse_warning = parse_warning
        final_process_error = process_error
        final_workflow = workflow
        final_returncode = returncode
        if returncode == 0:
            break
        # No model, parser, task, Workflow, or evaluator feedback is added.
        # The next attempt receives the byte-identical prompt in a fresh Session.

    raw_path = case_root / "response.txt"
    answer_path = case_root / "answer.json"
    raw_path.write_text(final_raw, encoding="utf-8")
    write_json(answer_path, final_answer)
    write_json(case_root / "attempts.json", attempt_rows)
    workflow_path: str | None = None
    if final_workflow is not None:
        target = case_root / f"workflow-source{final_workflow.suffix}"
        shutil.copy2(final_workflow, target)
        workflow_path = str(target.relative_to(run_root))
    return RunRecord(
        case_id=case.case_id,
        status="completed_process" if final_returncode == 0 else "failed_process",
        started_at=started_at,
        elapsed_seconds=total_elapsed,
        raw_response_path=str(raw_path.relative_to(run_root)),
        answer_path=str(answer_path.relative_to(run_root)),
        workflow_path=workflow_path,
        response_sha256=sha256_text(final_raw),
        prompt_sha256=prompt_hash,
        attempt_count=attempt_count,
        process_error=final_process_error,
        parse_warning=final_parse_warning,
    )


def run_shard(
    *,
    shard_number: int,
    cases: list[Case],
    run_root: Path,
    agent: Path,
    psi: Path,
    env: dict[str, str],
    max_attempts: int,
) -> list[RunRecord]:
    shard_root = run_root / "shards" / f"shard-{shard_number:02d}"
    service = ShardService(psi=psi, env=env, shard_root=shard_root, agent=agent)
    ai_attempts: list[dict[str, Any]] = []
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                service.start_ai(attempt)
            except Exception as exc:
                ai_attempts.append(
                    {
                        "attempt": attempt,
                        "started": False,
                        "process_failure": True,
                        "retry_eligible": attempt < max_attempts,
                        "error": repr(exc),
                    }
                )
                terminate(service.ai_process)
                service.ai_process = None
            else:
                ai_attempts.append(
                    {
                        "attempt": attempt,
                        "started": True,
                        "process_failure": False,
                        "retry_eligible": False,
                        "error": None,
                    }
                )
                break
        else:
            raise RuntimeError(f"shard {shard_number} AI process failed to start")
        write_json(shard_root / "ai-start-attempts.json", ai_attempts)
        records = [
            run_case(service, case, run_root=run_root, max_attempts=max_attempts)
            for case in cases
        ]
        write_json(shard_root / "records.json", [asdict(record) for record in records])
        return records
    finally:
        service.close()


def partition_cases(cases: list[Case], concurrency: int) -> list[list[Case]]:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    workers = min(concurrency, len(cases))
    base, extra = divmod(len(cases), workers)
    shards: list[list[Case]] = []
    offset = 0
    for index in range(workers):
        size = base + (1 if index < extra else 0)
        shards.append(cases[offset : offset + size])
        offset += size
    return shards


def merge_results(run_root: Path, cases: list[Case], records: list[RunRecord]) -> Path:
    by_case = {record.case_id: record for record in records}
    selected_ids = {case.case_id for case in cases}
    if len(records) != len(cases) or set(by_case) != selected_ids:
        raise ValueError("run records do not cover the selected frozen case set exactly once")
    merged = run_root / "merged" / "auto_workflow"
    merged_cases = merged / "cases"
    merged_cases.mkdir(parents=True, exist_ok=False)
    ordered: list[RunRecord] = []
    prediction_lines: list[str] = []
    for case in cases:
        record = by_case[case.case_id]
        ordered.append(record)
        source_case = run_root / record.answer_path
        source_case = source_case.parent
        target_case = merged_cases / safe_component(case.case_id)
        target_case.mkdir()
        for pattern in (
            "prompt.txt", "reference_information.json", "response.txt", "answer.json",
            "attempts.json", "response.attempt-*.txt", "answer.attempt-*.json",
            "session.attempt-*.log", "session.attempt-*.yml", "workflow*",
        ):
            for source in sorted(source_case.glob(pattern)):
                if source.is_file() and not (target_case / source.name).exists():
                    shutil.copy2(source, target_case / source.name)
        answer = json.loads((target_case / "answer.json").read_text(encoding="utf-8"))
        plan = answer.get("plan") if isinstance(answer, dict) else None
        prediction_lines.append(json.dumps({"plan": plan if isinstance(plan, list) else []}, ensure_ascii=False))
    write_json(merged / "records.json", [asdict(record) for record in ordered])
    (merged / "predictions.jsonl").write_text("\n".join(prediction_lines) + "\n", encoding="utf-8")
    process_completed = sum(record.status == "completed_process" for record in ordered)
    summary = {
        "case_count": len(ordered),
        "process_completed": process_completed,
        "process_failed": len(ordered) - process_completed,
        "workflow_detected": sum(record.workflow_path is not None for record in ordered),
        "parse_warnings": sum(record.parse_warning is not None for record in ordered),
        "total_process_attempts": sum(record.attempt_count for record in ordered),
        "retry_count": sum(record.attempt_count - 1 for record in ordered),
        "mean_elapsed_seconds": sum(record.elapsed_seconds for record in ordered) / len(ordered),
        "evaluator_used_during_inference": False,
        "quality_driven_retry": False,
    }
    write_json(merged / "summary.json", summary)
    if process_completed != len(ordered):
        raise RuntimeError("one or more cases exhausted process retries; evaluator must not run yet")
    return merged


def provenance(
    *,
    source: Path,
    agent: Path,
    psi: Path,
    env: dict[str, str],
    concurrency: int,
    max_attempts: int,
    selected_cases: list[Case],
    case_selection_policy: str,
) -> dict[str, Any]:
    changed_files = git_output(source, "diff", "--name-only", f"{EXPECTED_BASE_COMMIT}..HEAD").splitlines()
    selected_ids = tuple(case.case_id for case in selected_cases)
    complete_frozen_sample = selected_ids == EXPECTED_CASE_IDS
    return {
        "recorded_at": utc_now(),
        "method_hypothesis": (
            "The released main-branch Workflow method can execute a frozen, "
            "domain-independent orchestration contract reproducibly across unrelated tasks "
            "without task-specific validation or repair feedback."
        ),
        "psi_agent_repository": "https://github.com/1DivSin/psi-agent.git",
        "psi_agent_branch": git_output(source, "branch", "--show-current") or "(detached origin/main)",
        "psi_agent_commit": git_output(source, "rev-parse", "HEAD"),
        "psi_agent_base_commit": EXPECTED_BASE_COMMIT,
        "included_method_prs": [
            {"pr": 21, "commit": "4548a1f1", "scope": "domain-neutral adversarial verifier authoring"},
            {"pr": 22, "commit": "8c7f51e6", "scope": "programmatic Artifact schema execution"},
            {"pr": 24, "commit": "6d22e72b", "scope": "revert benchmark-related authoring defaults"},
        ],
        "candidate_pr": None,
        "candidate_commits": [],
        "candidate_changed_files": changed_files,
        "agent_snapshot_sha256": tree_digest(agent),
        "workflow_skill_sha256": sha256_file(agent / "skills" / "workflow" / "SKILL.md"),
        "workflow_grammar_sha256": sha256_file(agent / "skills" / "workflow" / "grammar" / "FusionFlow.g4"),
        "workflow_runner_sha256": sha256_file(agent / "skills" / "workflow" / "fusion_flow" / "workflow_runner.py"),
        "harness_sha256": sha256_file(Path(__file__)),
        "task_tools_sha256": tree_digest(TASK_TOOLS),
        "manifest_sha256": sha256_file(MANIFEST),
        "prompt_queries_sha256": sha256_file(PROMPT_QUERIES),
        "prompt_template_sha256": sha256_file(PROMPT_TEMPLATE),
        "city_state_index_sha256": sha256_file(CITY_STATE_INDEX),
        "case_selection_policy": case_selection_policy,
        "result_selection_disclosure": (
            "All cases in the frozen 30-case manifest are included in their registered order; "
            "no model output, case trace, or evaluator result selected this run."
            if complete_frozen_sample
            else "This run is a disclosed subset and is not an estimate for the complete frozen sample."
        ),
        "case_ids": [int(case.case_id) for case in selected_cases],
        "case_count": len(selected_cases),
        "arm": "current-main-baseline",
        "prompt_variant": "v1",
        "treatment_prefix": TREATMENT_PREFIX,
        "model_provider": env.get("PSI_AI_PROVIDER", ""),
        "model": env.get("PSI_AI_MODEL", ""),
        "base_url": env.get("PSI_AI_BASE_URL", ""),
        "api_key_configured": bool(env.get("PSI_AI_API_KEY")),
        "api_key_recorded": False,
        "psi_executable": str(psi.resolve()),
        "fresh_session_per_attempt": True,
        "concurrency": concurrency,
        "maximum_process_attempts": max_attempts,
        "retry_policy": "process and transport failures only",
        "retry_prompt_policy": "byte-identical original prompt",
        "quality_or_evaluator_driven_retry": False,
        "task_data_policy": "text reference only; identical query interface",
        "evaluator_visibility": "post-inference only",
        "excluded_components": [
            "execution-time task quality validator",
            "structured reference side channel",
            "structured case constraints",
            "Skill activation injection",
            "benchmark-specific treatment or repair prompt",
        ],
    }


def check_environment(source: Path, psi: Path, env: dict[str, str]) -> None:
    required = ("PSI_AI_PROVIDER", "PSI_AI_MODEL", "PSI_AI_API_KEY", "PSI_AI_BASE_URL")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise RuntimeError("missing model configuration: " + ", ".join(missing))
    if not psi.is_file():
        raise FileNotFoundError(f"psi-agent executable not found: {psi}")
    if git_output(source, "rev-parse", "HEAD") != EXPECTED_SOURCE_COMMIT:
        raise ValueError("source is not the expected baseline commit")
    load_cases()
    if sha256_file(PROMPT_TEMPLATE) != EXPECTED_TEMPLATE_SHA256:
        raise ValueError("prompt template hash mismatch")
    if not CITY_STATE_INDEX.is_file():
        raise FileNotFoundError(f"missing frozen city/state index: {CITY_STATE_INDEX}")


def run(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    psi = args.psi.resolve()
    run_root = args.run_root.resolve()
    if run_root.exists() and any(run_root.iterdir()):
        raise FileExistsError(f"run root must be new or empty: {run_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(load_env_file(args.secrets))
    source_path = str(source / "src")
    prior_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_path if not prior_pythonpath else source_path + os.pathsep + prior_pythonpath
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    check_environment(source, psi, env)
    all_cases = load_cases()
    if args.case_ids:
        requested_ids = tuple(part.strip() for part in args.case_ids.split(",") if part.strip())
        if not requested_ids or len(set(requested_ids)) != len(requested_ids):
            raise ValueError("case-ids must be a non-empty, duplicate-free comma-separated list")
        by_id = {case.case_id: case for case in all_cases}
        unknown = [case_id for case_id in requested_ids if case_id not in by_id]
        if unknown:
            raise ValueError(f"case-ids are outside the frozen manifest: {unknown}")
        frozen_order = tuple(case.case_id for case in all_cases if case.case_id in set(requested_ids))
        if requested_ids != frozen_order:
            raise ValueError("case-ids must retain the frozen manifest order")
        cases = [by_id[case_id] for case_id in frozen_order]
        case_selection_policy = args.selection_label
    else:
        if args.case_count < 1 or args.case_count > len(all_cases):
            raise ValueError(f"case-count must be between 1 and {len(all_cases)}")
        cases = all_cases[: args.case_count]
        case_selection_policy = "first N cases in frozen manifest order"
    agent = run_root / "agent"
    prepare_agent(source, agent)
    write_json(
        run_root / "provenance.json",
        provenance(
            source=source,
            agent=agent,
            psi=psi,
            env=env,
            concurrency=args.concurrency,
            max_attempts=args.max_attempts,
            selected_cases=cases,
            case_selection_policy=case_selection_policy,
        ),
    )
    write_json(
        run_root / "model-config.json",
        {
            "provider": env["PSI_AI_PROVIDER"],
            "model": env["PSI_AI_MODEL"],
            "base_url": env["PSI_AI_BASE_URL"],
            "api_key_configured": True,
            "api_key_recorded": False,
        },
    )
    shards = partition_cases(cases, args.concurrency)
    write_json(
        run_root / "shards.json",
        [
            {
                "shard": index,
                "case_ids": [case.case_id for case in shard],
            }
            for index, shard in enumerate(shards, 1)
        ],
    )
    started = time.monotonic()
    records: list[RunRecord] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(shards)) as executor:
        futures = [
            executor.submit(
                run_shard,
                shard_number=index,
                cases=shard,
                run_root=run_root,
                agent=agent,
                psi=psi,
                env=env,
                max_attempts=args.max_attempts,
            )
            for index, shard in enumerate(shards, 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            shard_records = future.result()
            records.extend(shard_records)
            completed = len(records)
            print(json.dumps({"event": "progress", "completed": completed, "total": len(cases)}), flush=True)
    merged = merge_results(run_root, cases, records)
    wall = time.monotonic() - started
    write_json(
        run_root / "run-complete.json",
        {
            "completed_at": utc_now(),
            "wall_seconds": wall,
            "merged_root": str(merged),
            "predictions": str(merged / "predictions.jsonl"),
        },
    )
    print(json.dumps({"event": "complete", "wall_seconds": wall, "merged": str(merged)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="build and audit the minimal agent snapshot")
    prepare.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    prepare.add_argument("--agent-root", type=Path, required=True)
    doctor = subparsers.add_parser("doctor", help="check frozen assets and runtime configuration")
    doctor.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    doctor.add_argument("--psi", type=Path, default=DEFAULT_PSI)
    doctor.add_argument("--secrets", type=Path, required=True)
    execute = subparsers.add_parser("run", help="run all 30 frozen cases and merge first-success outputs")
    execute.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    execute.add_argument("--psi", type=Path, default=DEFAULT_PSI)
    execute.add_argument("--secrets", type=Path, required=True)
    execute.add_argument("--run-root", type=Path, required=True)
    execute.add_argument("--concurrency", type=int, default=10)
    execute.add_argument("--max-attempts", type=int, default=3)
    execute.add_argument(
        "--case-count",
        type=int,
        default=30,
        help="take the first N cases in frozen manifest order",
    )
    execute.add_argument(
        "--case-ids",
        help="run these comma-separated IDs while retaining frozen manifest order",
    )
    execute.add_argument(
        "--selection-label",
        default="explicit diagnostic subset selected by the user",
        help="provenance label describing how --case-ids was selected",
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare_agent(args.source, args.agent_root.resolve())
        print(json.dumps({"agent": str(args.agent_root.resolve()), "sha256": tree_digest(args.agent_root.resolve())}))
        return 0
    if args.command == "doctor":
        env = os.environ.copy()
        env.update(load_env_file(args.secrets))
        check_environment(args.source.resolve(), args.psi.resolve(), env)
        print(json.dumps({"status": "ok", "cases": len(load_cases())}))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
