# CC Dynamic Workflow experiment

This repository's Claude Code experiment is the Dynamic Workflow variant. `CC` names the execution host; it is not a separate non-workflow baseline.

## Trigger and isolation

`experiment/runner.py` keeps the original `PROMPT_TEMPLATE` and invokes Claude Code non-interactively with:

```text
--output-format stream-json
--verbose
--effort ultracode
--setting-sources project,local
--mcp-config experiment/mcp.json
--strict-mcp-config
--allowed-tools Workflow,mcp__travelplanner__*
--permission-mode dontAsk
--model claude-opus-5
-p
```

Do not add `--bare`: on Claude Code 2.1.220 it reduced the registered built-in tools to `Bash`, `Edit`, and `Read`, so `Workflow` was unavailable even though Ultracode mapped the session to `xhigh` effort.

`experiment/run_penguin_30.ps1` creates a unique `runs/dynamic/<run-id>/` directory with run-isolated Claude config and temporary directories. It keeps all canonical outputs there instead of copying them to the older shared output folder.

## Prompt

The original prompt is embedded as `PROMPT_TEMPLATE` in `experiment/runner.py`. It asks Claude to:

- run a small workflow with at most 3 phases and 5 subagents;
- prefer sequential phases and parallelize only independent searches;
- limit each subagent to 5 tool calls and one fallback search;
- use TravelPlanner MCP data for flights, accommodations, restaurants, attractions, and distance;
- return only the required day-by-day TravelPlanner JSON.

No extra `dynamic` or `ultracode` wording was added to the prompt. Workflow routing comes from `--effort ultracode`.

## Verified runtime behavior

The no-`bare` pilot produced one `Workflow` call, a generated JavaScript workflow, and three workflow subagents. The main conversation made no direct TravelPlanner MCP calls.

The checked-in launcher is conservative and sequential. During the recorded experiment, a separate one-off parallel harness gave every process its own `CLAUDE_CONFIG_DIR` and temporary directory. Six concurrent query shards caused gateway queueing and 2400-second timeouts. Three shards were the highest effective concurrency observed.

The measured run and score artifacts are intentionally published in a separate
results PR so this environment change cannot alter the reported baseline.
