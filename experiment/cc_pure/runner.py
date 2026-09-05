"""Run one direct-MCP Claude Code session per TravelPlanner validation query."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio


PURE_ROOT = Path(__file__).resolve().parent
EXPERIMENT_ROOT = PURE_ROOT.parent
PROJECT_ROOT = EXPERIMENT_ROOT.parent
SOURCE_TRAVELPLANNER_ROOT = PROJECT_ROOT / "TravelPlanner"
DEFAULT_QUERIES = SOURCE_TRAVELPLANNER_ROOT / "postprocess" / "example_evaluation.jsonl"
DEFAULT_KEY_FILE = Path(
    os.environ.get("TRAVELPLANNER_GATEWAY_KEY_FILE", r"D:\Downloads\penguin_win_bq.txt")
)
DEFAULT_BASE_URL = "https://penguinapi.org"
MODEL = "claude-opus-5"
HAIKU_MODEL = "claude-haiku-4-5"
CLAUDE_CMD = shutil.which("claude") or "claude"
CHECKPOINTS = (50, 100, 150, 180)
SECRET_ENV_NAMES = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


PROMPT_TEMPLATE = """Create a complete TravelPlanner itinerary for the user query below using the available TravelPlanner MCP tools directly.

User query:
{query_json}

Requirements:
1. Use TravelPlanner MCP to search flights, accommodations, restaurants, attractions, and distances as needed.
2. Respect the budget, number of travelers, dates, number of cities, and every local constraint in the user query.
3. Every plan fact must come from data returned by TravelPlanner MCP; do not invent names, prices, times, or transport details.
4. Return exactly one JSON object and no explanation outside it, with this shape:

{{
  "idx": {idx},
  "query": {query_json},
  "plan": [
    {{
      "day": 1,
      "current_city": "from Origin to Destination",
      "transportation": "...",
      "breakfast": "...",
      "attraction": "...",
      "lunch": "...",
      "dinner": "...",
      "accommodation": "..."
    }}
  ]
}}

Field rules:
- day is a 1-indexed integer.
- On the first day current_city is "from <origin> to <destination>"; on the final day it is "from <current city> to <origin/home>"; otherwise use the city name.
- transportation uses the exact flight, self-driving, or taxi text returned by MCP, or "-" when there is no travel that day.
- breakfast, lunch, dinner, and accommodation use "<Name>, <City>" or "-".
- attraction uses semicolon-separated "<Name>, <City>;" entries, or "-".
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_indices_spec(spec: str) -> set[int]:
    indices: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, high = (int(part) for part in chunk.split("-", 1))
            if high < low:
                low, high = high, low
            indices.update(range(low, high + 1))
        else:
            indices.add(int(chunk))
    return indices


def load_queries(path: Path) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            queries.append({"idx": int(row["idx"]), "query": str(row["query"])})
    expected = set(range(1, 181))
    actual = {row["idx"] for row in queries}
    if len(queries) != 180 or actual != expected:
        raise ValueError(f"validation input must contain exactly indices 1..180; got {len(queries)} rows")
    return sorted(queries, key=lambda row: row["idx"])


def build_prompt(idx: int, query: str) -> str:
    return PROMPT_TEMPLATE.format(idx=idx, query_json=json.dumps(query, ensure_ascii=False))


def build_claude_args() -> list[str]:
    return [
        str(CLAUDE_CMD),
        "--output-format",
        "stream-json",
        "--verbose",
        "--effort",
        "high",
        "--setting-sources",
        "project,local",
        "--mcp-config",
        ".\\experiment\\cc_pure\\mcp.json",
        "--strict-mcp-config",
        "--allowed-tools",
        "mcp__travelplanner__*",
        "--permission-mode",
        "dontAsk",
        "--model",
        MODEL,
        "-p",
    ]


def sanitize_error(text: Any, secrets: tuple[str, ...] = ()) -> str:
    value = str(text)
    all_secrets = set(secrets)
    all_secrets.update(os.environ.get(name, "") for name in SECRET_ENV_NAMES)
    for secret in all_secrets:
        if secret:
            value = value.replace(secret, "<redacted>")
    return value[:2000]


def extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    decoder = json.JSONDecoder()
    for candidate_text in candidates:
        for match in re.finditer(r"\{", candidate_text):
            try:
                value, _ = decoder.raw_decode(candidate_text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _number(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        return float(value) if isinstance(value, float) else int(value)
    except (TypeError, ValueError):
        return None


def parse_process_result(
    idx: int,
    query: str,
    returncode: int,
    stdout: str,
    stderr: str,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    envelope: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            envelope = event

    record: dict[str, Any] = {
        "idx": idx,
        "query": query,
        "model": MODEL,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }
    if envelope is None:
        record.update(error="invalid JSON envelope", error_detail=sanitize_error(stderr or stdout, secrets))
        return record

    usage = envelope.get("usage") or {}
    model_usage = envelope.get("modelUsage") or {}
    record.update(
        {
            "input_tokens": _number(usage.get("input_tokens")),
            "output_tokens": _number(usage.get("output_tokens")),
            "cost_usd": envelope.get("total_cost_usd"),
            "num_turns": envelope.get("num_turns"),
            "model_usage": model_usage or None,
            "model_usage_total_input": sum(int(item.get("inputTokens", 0) or 0) for item in model_usage.values()) or None,
            "model_usage_total_output": sum(int(item.get("outputTokens", 0) or 0) for item in model_usage.values()) or None,
        }
    )
    if envelope.get("is_error"):
        record.update(
            error="api_error" if envelope.get("terminal_reason") == "api_error" else "claude_error",
            error_detail=sanitize_error(envelope.get("result", "Claude returned an error"), secrets),
            api_error_status=envelope.get("api_error_status"),
        )
        return record
    if returncode != 0:
        record.update(error=f"exit {returncode}", error_detail=sanitize_error(stderr or envelope.get("result", ""), secrets))
        return record

    parsed = extract_json(str(envelope.get("result", "")))
    if parsed is None or not isinstance(parsed.get("plan"), list) or not parsed.get("plan"):
        record.update(error="plan JSON extraction failed", error_detail=sanitize_error(envelope.get("result", ""), secrets))
    else:
        record["plan"] = parsed["plan"]
        record["parsed_query"] = parsed.get("query", query)
    return record


def _hardlink_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, target)


def link_tree(source: Path, destination: Path) -> None:
    try:
        os.symlink(source, destination, target_is_directory=True)
    except OSError:
        _hardlink_tree(source, destination)


def prepare_clean_tree(run_dir: Path) -> Path:
    model_root = run_dir / "model"
    if model_root.exists():
        required = (
            model_root / "experiment" / "cc_pure" / "mcp.json",
            model_root / "experiment" / "runner.py",
            model_root / "experiment" / "mcp_server.py",
            model_root / "TravelPlanner" / "tools",
            model_root / "TravelPlanner" / "utils",
            model_root / "TravelPlanner" / "postprocess" / "example_evaluation.jsonl",
        )
        if all(path.exists() for path in required):
            return model_root
        raise RuntimeError(f"incomplete clean model tree already exists: {model_root}")
    (model_root / "experiment" / "cc_pure").mkdir(parents=True, exist_ok=True)
    (model_root / "TravelPlanner" / "postprocess").mkdir(parents=True, exist_ok=True)
    # Keep the model-visible runner pure as well; the historical runner is not
    # needed by Claude and contains unrelated orchestration guidance.
    shutil.copy2(PURE_ROOT / "runner.py", model_root / "experiment" / "runner.py")
    shutil.copy2(EXPERIMENT_ROOT / "mcp_server.py", model_root / "experiment" / "mcp_server.py")
    shutil.copy2(PURE_ROOT / "mcp.json", model_root / "experiment" / "cc_pure" / "mcp.json")
    ignore_runtime = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(
        SOURCE_TRAVELPLANNER_ROOT / "tools",
        model_root / "TravelPlanner" / "tools",
        ignore=ignore_runtime,
    )
    shutil.copytree(
        SOURCE_TRAVELPLANNER_ROOT / "utils",
        model_root / "TravelPlanner" / "utils",
        ignore=ignore_runtime,
    )
    shutil.copy2(
        SOURCE_TRAVELPLANNER_ROOT / "postprocess" / "example_evaluation.jsonl",
        model_root / "TravelPlanner" / "postprocess" / "example_evaluation.jsonl",
    )
    database_files = (
        "flights/clean_Flights_2022.csv",
        "accommodations/clean_accommodations_2022.csv",
        "attractions/attractions.csv",
        "restaurants/clean_restaurant_2022.csv",
        "googleDistanceMatrix/distance.csv",
        "background/citySet_with_states.txt",
    )
    for relative in database_files:
        source = SOURCE_TRAVELPLANNER_ROOT / "database" / relative
        target = model_root / "TravelPlanner" / "database" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return model_root


def prepare_question_workdir(model_root: Path, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "experiment" / "cc_pure").mkdir(parents=True, exist_ok=True)
    shutil.copy2(model_root / "experiment" / "runner.py", workdir / "experiment" / "runner.py")
    shutil.copy2(model_root / "experiment" / "mcp_server.py", workdir / "experiment" / "mcp_server.py")
    config = json.loads((model_root / "experiment" / "cc_pure" / "mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["travelplanner"]
    server["command"] = str(Path(sys.executable).resolve())
    server["args"] = ["experiment\\mcp_server.py"]
    server["env"]["TRAVELPLANNER_ROOT"] = "TravelPlanner"
    (workdir / "experiment" / "cc_pure" / "mcp.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    link_tree(model_root / "TravelPlanner", workdir / "TravelPlanner")


def gateway_preflight(key_file: Path, base_url: str, gateway_path: Path) -> tuple[dict[str, Any], str, str]:
    inserted = str(EXPERIMENT_ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(EXPERIMENT_ROOT))
    try:
        import gateway_preflight as helper

        key = helper.load_key(key_file)
        model_ids, auth_mode = helper.probe_gateway(base_url, key)
        main_model, haiku_model = helper.select_models(model_ids)
        metadata = {
            "status": "passed",
            "base_url": base_url.rstrip("/"),
            "auth_mode": auth_mode,
            "main_model": main_model,
            "haiku_model": haiku_model,
        }
        gateway_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return metadata, key, auth_mode
    except Exception as exc:  # noqa: BLE001
        metadata = {
            "status": "failed",
            "base_url": base_url.rstrip("/"),
            "main_model": MODEL,
            "haiku_model": HAIKU_MODEL,
            "error": sanitize_error(f"{type(exc).__name__}: {exc}"),
        }
        gateway_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        raise
    finally:
        if inserted and str(EXPERIMENT_ROOT) in sys.path:
            sys.path.remove(str(EXPERIMENT_ROOT))


def child_env(config_dir: Path, temp_dir: Path, key: str, auth_mode: str, base_url: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in SECRET_ENV_NAMES:
        env.pop(name, None)
    env.update(
        {
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
            "CLAUDE_CODE_DISABLE_NATIVE_AUTH": "1",
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_BASE": base_url,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU_MODEL,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env["ANTHROPIC_AUTH_TOKEN" if auth_mode == "bearer" else "ANTHROPIC_API_KEY"] = key
    config_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    return env


async def run_attempt(
    idx: int,
    query: str,
    attempt: int,
    run_dir: Path,
    key: str,
    auth_mode: str,
    base_url: str,
    timeout: int,
) -> dict[str, Any]:
    workdir = run_dir / "workdirs" / str(idx) / f"attempt-{attempt}"
    config_dir = run_dir / "claude-config" / str(idx) / f"attempt-{attempt}"
    temp_dir = run_dir / "temp" / str(idx) / f"attempt-{attempt}"
    start_time = utc_now()
    start_mono = anyio.current_time()
    base: dict[str, Any] = {
        "idx": idx,
        "attempt": attempt,
        "query": query,
        "model_requested": MODEL,
        "model": MODEL,
        "start_time": start_time,
        "workdir": str(workdir),
    }
    try:
        prepare_question_workdir(run_dir / "model", workdir)
        env = child_env(config_dir, temp_dir, key, auth_mode, base_url)
        try:
            with anyio.fail_after(timeout):
                process = await anyio.run_process(
                    build_claude_args(),
                    input=build_prompt(idx, query).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    cwd=workdir,
                    env=env,
                )
        except TimeoutError:
            parsed = {"error": f"timeout after {timeout}s"}
            process = None
        else:
            parsed = parse_process_result(
                idx,
                query,
                process.returncode,
                process.stdout.decode("utf-8", errors="replace"),
                process.stderr.decode("utf-8", errors="replace"),
                (key,),
            )
    except Exception as exc:  # noqa: BLE001
        parsed = {"error": f"{type(exc).__name__}: {sanitize_error(exc, (key,))}"}
    end_time = utc_now()
    base.update(parsed)
    base["end_time"] = end_time
    base["duration_seconds"] = round(max(0.0, anyio.current_time() - start_mono), 6)
    return base


async def run_question(
    query: dict[str, Any],
    run_dir: Path,
    key: str,
    auth_mode: str,
    base_url: str,
    timeout: int,
    send: anyio.abc.ObjectSendStream[dict[str, Any]],
) -> None:
    attempt = 1
    question_root = run_dir / "workdirs" / str(query["idx"])
    while (question_root / f"attempt-{attempt}").exists():
        attempt += 1
    for _ in range(3):
        record = await run_attempt(
            int(query["idx"]),
            str(query["query"]),
            attempt,
            run_dir,
            key,
            auth_mode,
            base_url,
            timeout,
        )
        await send.send(record)
        if not record.get("error"):
            return
        # ponytail: a confirmed quota rejection cannot recover by retrying;
        # transient API failures still receive the two bounded retries below.
        if record.get("error") == "api_error" and (
            record.get("api_error_status") == 403
            or "quota" in str(record.get("error_detail", "")).lower()
        ):
            return
        if attempt < 3:
            await anyio.sleep(2**attempt)
        attempt += 1


def update_timing(timings: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    key = str(record["idx"])
    item = timings.setdefault(key, {"idx": record["idx"], "attempts": []})
    item["attempts"].append(
        {
            "attempt": record.get("attempt"),
            "start_time": record.get("start_time"),
            "end_time": record.get("end_time"),
            "duration_seconds": record.get("duration_seconds"),
            "status": "success" if not record.get("error") else "failed",
        }
    )
    starts = [part["start_time"] for part in item["attempts"] if part.get("start_time")]
    ends = [part["end_time"] for part in item["attempts"] if part.get("end_time")]
    item["start_time"] = min(starts) if starts else None
    item["end_time"] = max(ends) if ends else None
    item["duration_seconds"] = round(sum(float(part.get("duration_seconds") or 0) for part in item["attempts"]), 6)
    item["status"] = "success" if not record.get("error") else "failed"


def write_not_reached(run_dir: Path, threshold: int, success_count: int) -> None:
    path = run_dir / f"scores-{threshold}-not-reached.log"
    if path.exists() or (run_dir / f"scores-{threshold}.json").exists() or (run_dir / f"scores-{threshold}-evaluation-failed.log").exists():
        return
    path.write_text(
        json.dumps(
            {
                "status": "checkpoint_not_reached",
                "checkpoint": threshold,
                "successful_queries": success_count,
                "reason": "The run ended before enough successful query sessions were available.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def execute(args: argparse.Namespace) -> Path:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("cc-pure-%Y%m%d-%H%M%S")
    run_dir = (args.run_dir or (PROJECT_ROOT / "runs" / "cc_pure" / run_id)).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_started = utc_now()
    gateway_path = run_dir / "gateway.json"
    timing_path = run_dir / "timing.json"
    attempts_path = run_dir / "attempts.jsonl"
    queries = load_queries(args.queries.resolve())
    timings: dict[str, dict[str, Any]] = {}
    successful: set[int] = set()
    if args.resume and attempts_path.exists():
        with attempts_path.open("r", encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    record = json.loads(line)
                    update_timing(timings, record)
                    if record.get("plan") and not record.get("error"):
                        successful.add(int(record["idx"]))

    try:
        gateway, key, auth_mode = gateway_preflight(args.key_file.resolve(), args.base_url, gateway_path)
    except Exception:
        for threshold in CHECKPOINTS:
            write_not_reached(run_dir, threshold, len(successful))
        timing_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "started_at": run_started,
                    "ended_at": utc_now(),
                    "status": "gateway_preflight_failed",
                    "queries_total": len(queries),
                    "successful_queries": len(successful),
                    "per_query": [timings[key] for key in sorted(timings, key=int)],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return run_dir

    model_root = prepare_clean_tree(run_dir)
    del model_root, gateway
    pending = [query for query in queries if int(query["idx"]) not in successful]
    evaluated: set[int] = set()
    from evaluate_selected import write_checkpoint

    # Resume runs may already have crossed a checkpoint before this process
    # started; score it once before launching any new sessions.
    for threshold in CHECKPOINTS:
        score_path = run_dir / f"scores-{threshold}.json"
        if score_path.exists():
            evaluated.add(threshold)
        elif len(successful) >= threshold:
            selected = set(sorted(successful)[:threshold])
            await anyio.to_thread.run_sync(
                write_checkpoint,
                attempts_path,
                args.queries.resolve(),
                selected,
                SOURCE_TRAVELPLANNER_ROOT,
                score_path,
                run_dir / f"scores-{threshold}-evaluation-failed.log",
            )
            evaluated.add(threshold)

    send, receive = anyio.create_memory_object_stream[dict[str, Any]](0)

    async def produce() -> None:
        limiter = anyio.CapacityLimiter(args.concurrency)

        async def limited(query: dict[str, Any]) -> None:
            async with limiter:
                await run_question(query, run_dir, key, auth_mode, args.base_url, args.timeout, send)

        async with anyio.create_task_group() as task_group:
            for query in pending:
                task_group.start_soon(limited, query)
        await send.aclose()

    with attempts_path.open("a", encoding="utf-8") as output:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(produce)
            async with receive:
                async for record in receive:
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output.flush()
                    update_timing(timings, record)
                    if record.get("plan") and not record.get("error"):
                        successful.add(int(record["idx"]))
                    for threshold in CHECKPOINTS:
                        if len(successful) >= threshold and threshold not in evaluated:
                            evaluated.add(threshold)
                            selected = set(sorted(successful)[:threshold])
                            await anyio.to_thread.run_sync(
                                write_checkpoint,
                                attempts_path,
                                args.queries.resolve(),
                                selected,
                                SOURCE_TRAVELPLANNER_ROOT,
                                run_dir / f"scores-{threshold}.json",
                                run_dir / f"scores-{threshold}-evaluation-failed.log",
                            )

    for threshold in CHECKPOINTS:
        if threshold not in evaluated:
            write_not_reached(run_dir, threshold, len(successful))
    timing_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_at": run_started,
                "ended_at": utc_now(),
                "status": "completed",
                "queries_total": len(queries),
                "successful_queries": len(successful),
                "failed_queries": len(queries) - len(successful),
                "concurrency": args.concurrency,
                "per_query": [timings[key] for key in sorted(timings, key=int)],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--run-id")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 3:
        parser.error("--concurrency must be between 1 and 3")
    run_dir = anyio.run(execute, args)
    print(f"Run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
