"""Batch-evaluate Claude Code Dynamic Workflows on TravelPlanner queries.

This script triggers Claude Code's official dynamic-workflow mode by including
the word "workflow" in the prompt. Claude writes a JavaScript workflow that
orchestrates subagents; those subagents use the project `travelplanner` MCP
server to build itineraries.

Example (pilot on 3 queries):
    uv run python scripts/eval_claude_code_dynamic_workflow.py --limit 3 --expected-count 3

Example (selected batch through an API proxy):
    uv run python scripts/eval_claude_code_dynamic_workflow.py \\
        --api-base-url http://127.0.0.1:15721 \\
        --timeout 2400 \\
        --indices "26,27,40-46,55-59,61-62,67-78,97-102,109-119,140-144,151-161" \\
        --expected-count 59 \\
        --output runs/dw_cc8_rerun_fixable_59.jsonl \\
        --resume
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
from loguru import logger
from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRAVELPLANNER_ROOT = PROJECT_ROOT / "TravelPlanner"
DEFAULT_MCP_CONFIG = Path(__file__).resolve().with_name("mcp.json")
CLAUDE_CMD = shutil.which("claude") or "claude"


PROMPT_TEMPLATE = """Run a workflow to plan a complete TravelPlanner itinerary for the user query below.

User query:
{query}

Workflow design constraints (CRITICAL - follow exactly):
1. Keep the workflow SMALL and FAST. Use at most 3 phases and at most 5 subagents total.
2. Prefer SEQUENTIAL phases over deep nesting or excessive parallelism.
3. Each subagent should make at most 5 tool calls. If a tool returns no results, try ONE alternative and then move on - do not loop or retry repeatedly.
4. The entire workflow must complete within 10 minutes. Bias toward producing a good-enough plan quickly rather than an optimal plan slowly.
5. Do not spawn subagents for tasks that can be done inline. Only parallelize independent searches (e.g. different cities).

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


def parse_indices_spec(spec: str) -> set[int]:
    """Parse an indices CLI spec like "26,27,40-46,55-59" into a set of ints."""
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, hi_s = chunk.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                lo, hi = hi, lo
            for i in range(lo, hi + 1):
                out.add(i)
        else:
            out.add(int(chunk))
    return out


def sanitize_error(text: Any) -> str:
    """Redact inherited Anthropic credentials and bound persisted error text."""
    sanitized = str(text)
    for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    return sanitized[:2000]


def build_claude_args(mcp_config: Path, model: str | None = None) -> list[str]:
    args = [
        str(CLAUDE_CMD),
        "--output-format",
        "stream-json",
        "--verbose",
        "--effort",
        "ultracode",
        "--setting-sources", "project,local",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--allowed-tools",
        "Workflow,mcp__travelplanner__*",
        "--permission-mode",
        "dontAsk",
    ]
    if model:
        args += ["--model", model]
    args += ["-p"]
    return args


def load_done_indices(path: Path) -> set[int]:
    """Return only completed records that have a non-empty plan and no error."""
    if not path.exists():
        return set()
    done: set[int] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("plan") and not obj.get("error"):
                done.add(obj["idx"])
    return done


def load_queries(
    path: Path,
    limit: int | None = None,
    offset: int = 0,
    indices_whitelist: set[int] | None = None,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if indices_whitelist is not None and obj.get("idx") not in indices_whitelist:
                continue
            if offset > 0:
                offset -= 1
                continue
            queries.append(obj)
            if limit and len(queries) >= limit:
                break
    # If whitelist provided, sort the returned list by idx so runs are in a
    # deterministic, human-traceable order regardless of file order.
    if indices_whitelist is not None:
        queries.sort(key=lambda q: q.get("idx", 0))
    return queries


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from Claude's response."""
    block = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if block:
        candidate = block.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _parse_process_result(
    idx: int,
    query: str,
    returncode: int,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Parse Claude stream-json output into one bounded archival record."""
    envelope: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            envelope = event

    record: dict[str, Any] = {"idx": idx, "query": query}
    if envelope is not None:
        model_usage = envelope.get("modelUsage", {}) or {}
        record.update(
            {
                "cost_usd": envelope.get("total_cost_usd"),
                "input_tokens": envelope.get("usage", {}).get("input_tokens"),
                "output_tokens": envelope.get("usage", {}).get("output_tokens"),
                "model_usage_total_input": sum(m.get("inputTokens", 0) for m in model_usage.values()) or None,
                "model_usage_total_output": sum(m.get("outputTokens", 0) for m in model_usage.values()) or None,
                "model_usage_total_cache_read": sum(m.get("cacheReadInputTokens", 0) for m in model_usage.values()) or None,
                "model_usage_total_cache_create": sum(m.get("cacheCreationInputTokens", 0) for m in model_usage.values()) or None,
                "model_usage_detail": model_usage or None,
                "num_turns": envelope.get("num_turns"),
                "model": next(iter(model_usage), None),
            }
        )

    if envelope is not None and envelope.get("is_error") and envelope.get("terminal_reason") == "api_error":
        record.update(
            {
                "error": "api_error",
                "api_error": sanitize_error(envelope.get("result", "unknown API error")),
                "api_error_status": envelope.get("api_error_status"),
            }
        )
        return record

    if returncode != 0:
        detail = stderr or (envelope or {}).get("result") or stdout
        record.update({"error": f"exit {returncode}", "error_detail": sanitize_error(detail)})
        return record

    if envelope is None:
        record.update(
            {
                "error": "invalid JSON envelope",
                "error_detail": sanitize_error(stderr or stdout),
            }
        )
        return record

    result_text = str(envelope.get("result", ""))
    parsed = extract_json(result_text)
    if parsed is None:
        record.update(
            {
                "error": "plan JSON extraction failed",
                "error_detail": sanitize_error(result_text),
            }
        )
    else:
        record["plan"] = parsed.get("plan", [])
        record["parsed_query"] = parsed.get("query", query)
    return record


async def _run_claude_once(
    idx: int,
    query: str,
    model: str | None,
    api_base_url: str | None,
    mcp_config: Path,
    workdir: Path,
) -> dict[str, Any]:
    prompt = PROMPT_TEMPLATE.format(
        idx=idx,
        query=query,
        query_json=json.dumps(query),
    )
    args = build_claude_args(mcp_config, model)

    env = os.environ.copy()
    if api_base_url:
        env["ANTHROPIC_BASE_URL"] = api_base_url
        env["ANTHROPIC_API_BASE"] = api_base_url

    proc = await anyio.run_process(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        env=env, cwd=workdir,
        input=prompt.encode("utf-8"),
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")

    record = _parse_process_result(idx, query, proc.returncode, stdout, stderr)

    if "error" in record:
        detail = record.get("api_error") or record.get("error_detail") or record["error"]
        logger.error(f"Query {idx}: {record['error']}: {detail[:500]}")
    return record


async def run_claude(
    idx: int, query: str, model: str | None,
    api_base_url: str | None, mcp_config: Path, workdir: Path,
) -> dict[str, Any]:
    logger.info(f"Query {idx}: calling claude...")
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            record = await _run_claude_once(
                idx, query, model, api_base_url, mcp_config, workdir,
            )
            if "error" not in record:
                return record
            if attempt < max_retries:
                # API 503 errors come from gateway capacity; wait longer than generic
                # process errors before retrying.
                if record.get("error") == "api_error":
                    wait = 30 * (2 ** attempt)
                    logger.warning(
                        f"Query {idx}: upstream API error on attempt {attempt + 1}, "
                        f"retrying in {wait}s..."
                    )
                else:
                    wait = 2 ** attempt
                    logger.warning(f"Query {idx}: attempt {attempt + 1} failed, retrying in {wait}s...")
                await anyio.sleep(wait)
            else:
                return record
        except Exception as exc:  # noqa: BLE001
            error = sanitize_error(f"{type(exc).__name__}: {exc}")
            logger.error(f"Query {idx}: attempt {attempt + 1} raised {error}")
            if attempt < max_retries:
                await anyio.sleep(2 ** attempt)
            else:
                return {"idx": idx, "query": query, "error": error}
    return {"idx": idx, "query": query, "error": "unexpected loop exit"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Claude Code Dynamic Workflows on TravelPlanner")
    parser.add_argument("--queries", type=Path, default=DEFAULT_TRAVELPLANNER_ROOT / "postprocess" / "example_evaluation.jsonl")
    parser.add_argument("--output", type=Path, default=Path("runs") / "claude_code_dynamic_workflow_validation.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N queries (applies AFTER --indices whitelist)")
    parser.add_argument("--model", type=str, default=None, help="Claude model alias (e.g. sonnet, opus)")
    parser.add_argument("--resume", action="store_true", help="Skip already-processed indices in output file")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-query timeout in seconds")
    parser.add_argument("--mcp-config", type=Path, default=DEFAULT_MCP_CONFIG)
    parser.add_argument("--workdir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument(
        "--indices",
        type=str,
        default=None,
        help='Comma-separated idx whitelist. Supports ranges, e.g. "26,27,40-46,55-59,61-62,67-78". '
             "If set, only these idx are run; --offset/--limit apply after filtering.",
    )
    parser.add_argument(
        "--api-base-url",
        type=str,
        default=None,
        help="If set, overrides ANTHROPIC_BASE_URL for every subprocess call. "
             "Use http://127.0.0.1:15721 to force CC-Switch proxy.",
    )
    args = parser.parse_args()
    mcp_config = args.mcp_config.resolve()
    workdir = args.workdir.resolve()

    indices_whitelist: set[int] | None = None
    if args.indices:
        indices_whitelist = parse_indices_spec(args.indices)
        if not indices_whitelist:
            logger.critical(f"[PREFLIGHT FAIL] --indices {args.indices!r} parsed to empty set, nothing to run.")
            raise SystemExit(2)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    queries = load_queries(args.queries, args.limit, args.offset, indices_whitelist)
    if len(queries) != args.expected_count:
        logger.critical(
            f"Expected {args.expected_count} queries but loaded {len(queries)} from {args.queries}"
        )
        raise SystemExit(2)
    logger.info(
        f"Loaded {len(queries)} queries from {args.queries} "
        f"(indices={'%d idx' % len(indices_whitelist) if indices_whitelist else 'ALL'}, "
        f"offset={args.offset}, limit={args.limit})"
    )
    auth_set = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    logger.info(f"Claude auth={'set' if auth_set else 'unset'}")

    done_indices = load_done_indices(args.output) if args.resume else set()
    if args.resume:
        logger.info(f"Resuming, {len(done_indices)} queries already done")

    success = 0
    failed = 0
    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_model_input = 0
    total_model_output = 0

    pending_queries = [q for q in queries if q["idx"] not in done_indices]
    progress = tqdm(
        total=len(pending_queries),
        desc="TravelPlanner dynamic workflow eval",
        unit="query",
        ncols=120,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
    )

    with open(args.output, "a", encoding="utf-8") as out_f:
        for query_obj in pending_queries:
            idx = query_obj["idx"]
            try:
                with anyio.fail_after(args.timeout):
                    record = await run_claude(idx, query_obj["query"], args.model,
                                              args.api_base_url, mcp_config, workdir)
            except TimeoutError:
                logger.error(f"Query {idx}: timed out after {args.timeout}s")
                record = {"idx": idx, "query": query_obj["query"], "error": f"timeout after {args.timeout}s"}
            # Also persist which env configuration this run used, so the next
            # merge pass can tell if this run actually hit CC-Switch vs cc7.
            if args.api_base_url:
                record.setdefault("api_base_url_used", args.api_base_url)
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            out_f.flush()

            if "error" in record:
                failed += 1
            else:
                success += 1
            total_cost += record.get("cost_usd") or 0.0
            total_input_tokens += record.get("input_tokens") or 0
            total_output_tokens += record.get("output_tokens") or 0
            total_model_input += record.get("model_usage_total_input") or 0
            total_model_output += record.get("model_usage_total_output") or 0
            cost_str = f"${record.get('cost_usd'):.4f}" if record.get('cost_usd') is not None else "N/A"
            turns_str = record.get("num_turns") if record.get("num_turns") is not None else "N/A"
            # If upstream returned a structured modelUsage, also log which
            # canonical model answered, so the user can cross-check "did this
            # really go through CC-Switch?".
            model_tag = record.get("model") or "-"
            logger.info(
                f"Query {idx}: {'OK' if 'error' not in record else 'FAIL'} "
                f"model={model_tag} cost={cost_str} turns={turns_str}"
            )

            progress.set_postfix(
                ok=success,
                fail=failed,
                cost=f"${total_cost:.2f}",
                last=idx,
            )
            progress.update(1)

    progress.close()
    logger.info("=" * 60)
    logger.info(f"Done: {success} succeeded, {failed} failed")
    logger.info(f"Total cost: ${total_cost:.4f}")
    logger.info(f"Total tokens (usage.*, main conversation only): {total_input_tokens} in / {total_output_tokens} out")
    logger.info(f"Total tokens (model_usage_total.*, all API calls incl. sub-agents): {total_model_input} in / {total_model_output} out")
    logger.info(f"Output written to {args.output}")


if __name__ == "__main__":
    anyio.run(main)
