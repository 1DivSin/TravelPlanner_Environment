# CC direct-MCP environment

This is the pure CC baseline from the `cc+旧` session. Each validation query
runs in a fresh Claude Code process and may call only the TravelPlanner MCP
tools. It does not use Workflow, `agent()`, `parallel()`, `pipeline()`, or
any other orchestration.

## Claude Code invocation

`runner.py` keeps the baseline prompt in one place and launches:

```text
--output-format stream-json
--verbose
--effort high
--setting-sources project,local
--mcp-config .\experiment\cc_pure\mcp.json
--strict-mcp-config
--allowed-tools mcp__travelplanner__*
--permission-mode dontAsk
--model claude-opus-5
-p
```

There is no `--max-budget-usd` and no other forced budget limit. The prompt
contains no Dynamic/Workflow/Ultracode routing cue; every plan fact must come
from the MCP response and the final response must be one JSON object.

## Environment

| Variable | Value or source |
| --- | --- |
| `ANTHROPIC_BASE_URL` | `https://penguinapi.org` |
| `ANTHROPIC_API_BASE` | `https://penguinapi.org` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `claude-opus-5` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `claude-opus-5` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `claude-haiku-4-5` |
| `CLAUDE_CODE_DISABLE_WORKFLOWS` | `1` |
| `CLAUDE_CODE_DISABLE_NATIVE_AUTH` | `1` |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | Read from the local key file, selected by gateway preflight |
| `CLAUDE_CONFIG_DIR` | Unique `runs/<run-id>/claude-config/<idx>/attempt-N` |
| `TEMP`, `TMP` | Unique `runs/<run-id>/temp/<idx>/attempt-N` |
| `TRAVELPLANNER_GATEWAY_KEY_FILE` | Optional override; default is `D:\Downloads\penguin_win_bq.txt` |

The runner caps outer concurrency at three. Every query gets its own
model-visible worktree, config directory, temporary directory, and up to
three independent attempts. Only the MCP server, the runner, the TravelPlanner
tools/utilities, the 180-query input, and the MCP-required database files are
copied or linked into that worktree.

## Local setup

The repository intentionally does not commit the roughly 342 MB database.
Populate `TravelPlanner/database/` from the same TravelPlanner checkout used by
the recorded run, and install `experiment/requirements.lock.txt` with `uv`.
The committed `TravelPlanner/tools/`, `TravelPlanner/utils/`, and
`TravelPlanner/postprocess/example_evaluation.jsonl` files document the code
and query-set boundary.

Run a full attempt with:

```powershell
$env:PYTHONPATH = "experiment"
uv run python experiment/cc_pure/runner.py `
  --queries TravelPlanner/postprocess/example_evaluation.jsonl `
  --run-id cc-pure-local `
  --key-file $env:TRAVELPLANNER_GATEWAY_KEY_FILE `
  --concurrency 3
```

The runner writes `attempts.jsonl`, `timing.json`, `gateway.json`, and
checkpoint score/failure files under `runs/<run-id>/`. Credentials are removed
from child environments and redacted from persisted errors.
