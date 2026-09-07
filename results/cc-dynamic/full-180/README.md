# Dynamic full 180 evidence

This directory contains the complete locally preserved evidence for the 180-query Dynamic evaluation.

- `dynamic180-predictions.jsonl`: one delivered Dynamic prediction per validation index.
- `dynamic180-rescored.json`: offline evaluator output for all 180 predictions.
- `dynamic-source-manifest.json`: source record mapping for each index.
- `full180-comparison.json`: comparison metadata used in the PR4/PR5 review.
- `analysis.md`: detailed offline analysis of the 62.78% result and matched-query comparison.
- `raw-dynamic-runs.zip`: compressed raw Dynamic run directories, including Claude Code JSONL session streams, generated Workflow artifacts, subagent journals, tool outputs, logs, and retry records.

The 180-query run produced 180 delivered plans and scored 113/180 (62.78%). The 67 failures are evaluator failures on delivered plans; they are not missing-plan or timeout rows.

Credential and authentication configuration files, shell snapshots, and local settings were removed from the raw archive. The archive is evidence material, not a runnable credential bundle.
