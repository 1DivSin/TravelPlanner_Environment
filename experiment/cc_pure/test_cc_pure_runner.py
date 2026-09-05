from __future__ import annotations

import json

from experiment.cc_pure import runner


def test_prompt_has_no_orchestration_cues() -> None:
    prompt = runner.build_prompt(123, "Visit two cities.").lower()
    for forbidden in ("dynamic", "workflow", "ultracode", "agent()", "parallel()", "pipeline()", "phase()"):
        assert forbidden not in prompt


def test_claude_args_are_fixed_to_direct_mcp() -> None:
    args = runner.build_claude_args()
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--setting-sources") + 1] == "project,local"
    assert args[args.index("--mcp-config") + 1] == ".\\experiment\\cc_pure\\mcp.json"
    assert args[args.index("--allowed-tools") + 1] == "mcp__travelplanner__*"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--model") + 1] == "claude-opus-5"
    assert "Workflow" not in args
    assert "--max-budget-usd" not in args
    assert args[-1] == "-p"


def test_extract_json_reads_fenced_object() -> None:
    value = runner.extract_json('answer\n```json\n{"idx": 1, "plan": [{"day": 1}]}\n```')
    assert value == {"idx": 1, "plan": [{"day": 1}]}


def test_update_timing_records_non_negative_duration() -> None:
    timings: dict[str, dict] = {}
    runner.update_timing(
        timings,
        {
            "idx": 4,
            "attempt": 1,
            "start_time": "2026-09-04T01:00:00.000+00:00",
            "end_time": "2026-09-04T01:00:01.000+00:00",
            "duration_seconds": 1.0,
            "plan": [{"day": 1}],
        },
    )
    assert timings["4"]["start_time"] < timings["4"]["end_time"]
    assert timings["4"]["duration_seconds"] >= 0
    json.dumps(timings)
