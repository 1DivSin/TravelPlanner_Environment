from __future__ import annotations

import inspect
import json
from pathlib import Path

from experiment import runner


EXACT_30 = "1,11,14,17,28,33,38,41,46,48,70,72,77,81,83,100,110,113,116,118,123,124,138,144,146,151,159,161,162,163"


def test_parse_indices_spec_exact_30() -> None:
    indices = runner.parse_indices_spec(EXACT_30)

    assert len(indices) == 30
    assert indices == {
        1, 11, 14, 17, 28, 33, 38, 41, 46, 48,
        70, 72, 77, 81, 83, 100, 110, 113, 116, 118,
        123, 124, 138, 144, 146, 151, 159, 161, 162, 163,
    }


def test_build_claude_args_uses_strict_dont_ask_permissions(tmp_path: Path) -> None:
    prompt = "first line\nsecond line"
    args = runner.build_claude_args(tmp_path / "mcp.json", "sonnet")

    prompt_flag_index = args.index("-p")
    assert all(
        index < prompt_flag_index
        for index, arg in enumerate(args)
        if arg.startswith("-") and arg != "-p"
    )
    assert prompt not in args
    assert args[-1] == "-p"
    assert "bypassPermissions" not in args
    assert "--bare" not in args
    assert args[args.index("--effort") + 1] == "ultracode"
    assert args[args.index("--setting-sources") + 1] == "project,local"
    assert "user" not in args[args.index("--setting-sources") + 1].split(",")
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in args
    assert args[args.index("--allowed-tools") + 1] == "Workflow,mcp__travelplanner__*"
    assert args[args.index("--model") + 1] == "sonnet"


def test_run_claude_once_pipes_multiline_prompt_to_stdin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_process(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return type(
            "ProcessResult",
            (),
            {"stdout": b"", "stderr": b"failed", "returncode": 1},
        )()

    monkeypatch.setattr(runner.anyio, "run_process", fake_run_process)
    query = "Visit Paris\nwithout flying"

    runner.anyio.run(
        runner._run_claude_once,
        7,
        query,
        None,
        None,
        tmp_path / "mcp.json",
        tmp_path,
    )

    prompt = runner.PROMPT_TEMPLATE.format(
        idx=7,
        query=query,
        query_json=json.dumps(query),
    )
    assert captured["args"][-1] == "-p"
    assert captured["input"] == prompt.encode("utf-8")

    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert 'input=prompt.encode("utf-8")' in source or "input = prompt.encode(\"utf-8\")" in source


def test_load_done_indices_only_skips_nonempty_plans_without_errors(tmp_path: Path) -> None:
    output = tmp_path / "results.jsonl"
    records = [
        {"idx": 1, "plan": [{"day": 1}]},
        {"idx": 2, "plan": []},
        {"idx": 3, "plan": [{"day": 1}], "error": "failed"},
        {"idx": 4, "error": "failed"},
    ]
    output.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    assert runner.load_done_indices(output) == {1}


def test_sanitize_error_replaces_auth_token(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret-auth-token")

    sanitized = runner.sanitize_error("request failed: secret-auth-token")

    assert sanitized == "request failed: <redacted>"


def test_runner_source_has_no_raw_stream_cli() -> None:
    source = Path(runner.__file__).read_text(encoding="utf-8")

    assert "--save-stdout" not in source


def test_runner_signatures_have_no_raw_stream_parameter() -> None:
    for function in (
        runner._parse_process_result,
        runner._run_claude_once,
        runner.run_claude,
    ):
        assert "save_stdout" not in inspect.signature(function).parameters
