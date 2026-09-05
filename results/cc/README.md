# CC direct-MCP result

Run: `cc-pure-formal-20260904-1118` from the `cc+旧` session.

## Outcome

- 180/180 validation indices were attempted exactly once as unique query
  targets; 53 produced a plan and 127 ended in gateway quota failures.
- The run used three outer workers, one isolated Claude Code process per
  attempt, and no Workflow/subagent orchestration.
- The first reachable checkpoint (50 successful query sessions) scored
  **36/50 final pass (72.00%)**.
- Checkpoints 100, 150, and 180 were not reached, so no score is inferred for
  them.

The per-query score evidence is in [scores-50.json](scores-50.json). The
sanitized attempt ledger keeps query, plan, timing, token, cost, and error
fields while removing local paths, raw provider errors, and model-detail
payloads.

## Interpretation

This is a direct-MCP baseline, not a failed full-180 evaluation: the runner
did finish the requested 180-index attempt schedule, but the gateway stopped
returning usable sessions after 53 successes. The result should therefore be
reported as “180 attempted / 53 successful / 50 scored,” rather than as a
72% score over all 180 queries.

The final checkpoint cost was `$54.2519238` for 50 scored successful plans.
The gateway metadata is retained in [gateway.json](gateway.json); it contains
model/auth-mode metadata only and no credential.
