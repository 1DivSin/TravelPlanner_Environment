# Experiment provenance boundary

The imported material is limited to the conditions that were still aligned
with the attached experiment archive when the two sessions ran.

## Sources

- Archive: `TravelPlanner_experiments.zip`
- Archive SHA-256: `0C9D4E7EB77C1DF9F8F17EF7F3642449B3C69DCE284818945A6B45CE64F0BABE`
- Dynamic archive baseline: `archive/pre-03-dynamic-review-20260831` at
  `56c5bf7c9636357419a1e0dd87b2b8f2a3c5b992`
- Verified Dynamic runtime plumbing: `a56a84680ff3fb10cf008c4d28325edfbce17164`

## Included boundary

- CC: the direct-MCP prompt and the isolated 180-query run from
  `cc-pure-formal-20260904-1118`.
- CC Dynamic: the original archive-compatible workflow prompt and the merged
  30-query run from `retry17-dynamic-20260831-233258`.
- The Dynamic run keeps the original prompt and adds only runtime plumbing
  needed to make the verified Workflow tool available (`--effort ultracode`,
  removal of `--bare`, isolated config/temp directories, and bounded
  concurrency).

## Explicitly excluded

- The later prompt that explicitly prescribed `RECON → ASSEMBLE → VERIFY`.
- Results produced after that prompt change, including later 20/20, 30/30,
  38/50, and 180-query Dynamic reruns.
- Reruns whose session audit showed reads of prior workflow memory, old
  generated scripts, or historical result files.
- Tokens, Claude session directories, raw temporary worktrees, and the large
  local TravelPlanner database.

The result PRs therefore describe the two original-prompt experiments, not a
later prompt revision or a merged history of all runs.
