# CC Dynamic Workflow result

Run: `retry17-dynamic-20260831-233258` from the Dynamic session, using the
original archive-compatible prompt.

## Outcome

- Selected queries: 30
- Delivery: **30/30 (100.00%)**
- Commonsense: **97.08% micro / 76.67% macro**
- Hard constraints: **98.41% micro / 86.67% macro**
- Final pass: **22/30 (73.33%)**
- Recorded successful-attempt cost: **$52.065332**
- Effective compute time accumulated across the initial segment and the final
  three-shard retry: **9472.012292 seconds**

The final retry processed 17 remaining queries with three isolated shards and
17/17 successful deliveries. A six-shard attempt had gateway queueing and
2400-second timeouts; it is not presented as the final effective concurrency.

## Dynamic evidence

The one-query gate recorded one `Workflow` call, one generated JavaScript
workflow, three Workflow subagents, and zero direct TravelPlanner MCP calls in
the main session. The run therefore exercised Dynamic Workflow routing rather
than ordinary direct-MCP planning.

The prompt itself remains the original `Run a workflow...` template. The later
prompt that explicitly prescribed `RECON → ASSEMBLE → VERIFY` is outside this
result and is intentionally not represented here.

`formal-30-scores.json` contains the per-query scorer output; the compact
report is in `formal-30-report.md`, and the sanitized attempt ledger is in
`formal-30-attempts.jsonl`.
