# Dynamic full 180 evidence

This directory contains the complete locally preserved evidence for the 180-query Dynamic evaluation.

- `dynamic180-predictions.jsonl`: one delivered Dynamic prediction per validation index.
- `dynamic180-rescored.json`: offline evaluator output for all 180 predictions.
- `dynamic-source-manifest.json`: source record mapping for each index.
- `full180-comparison.json`: comparison metadata used in the PR4/PR5 review.
- `analysis.md`: detailed offline analysis of the 62.78% result and matched-query comparison.

The 180-query run produced 180 delivered plans and scored 113/180 (62.78%). The 67 failures are evaluator failures on delivered plans; they are not missing-plan or timeout rows.

The original raw Claude Code stream transcripts and tool-call traces are not included here because the source run directories are outside this checkout and were not available as readable files during packaging. The prediction records retain the final plans, query text, usage metadata, and source mapping. Add raw streams under `raw/` only after verifying they contain no credentials or private gateway tokens.
