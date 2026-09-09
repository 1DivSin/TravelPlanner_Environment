# Clean main TravelPlanner experiment bundle

This directory archives the 180-case validation assembled from the clean main
workflow runs. The bundle is intentionally review-oriented: it includes the
frozen inputs, prompt configuration, provenance for every batch, the exact
180-row evaluator input, and the official evaluator output. API keys, session
logs, caches, and duplicate runtime workspaces are excluded.

## Source and inference configuration

- psi-agent source commit: `851f041a74b85465b898d56236159765a17eceb4`
- prompt variant: `clean-v1-no-adapter-constraints`
- evaluator feedback during inference: disabled
- relation-contract injection: excluded
- benchmark-specific treatment prompt: excluded
- inference tool policy: generic workflow tools plus registered TravelPlanner task tools
- model API endpoint is recorded in `provenance/model-config.json`; credentials are not included

## Batches

The final evaluator input combines these authorized result roots in case-id
order. Quota rerun answers take precedence over missing answers in the main
run; no quality-based substitution is performed.

1. `provenance/main-1-50/` — original 1–50 run
2. `provenance/main-51-180/` — main 51–180 run, concurrency 10, maximum 3 process attempts
3. `provenance/quota-retry6/` — 11 answers recovered before the paused rerun
4. `provenance/quota-retry8/` — remaining quota cases, concurrency 2, maximum 3 attempts

## Evaluator

`evaluator/predictions.jsonl` contains exactly 180 validation rows. Missing
answers are represented by `{"plan": []}`. The file was evaluated with the
official `travelplanner-official/evaluation/eval.py` implementation and
`--set_type validation`.

Recorded result: 175 nonempty plans; 5 empty plans (cases 43, 56, 105, 152,
170); Final Pass Rate 72.2222%.

The per-batch and final provenance files are metadata only. They contain no
credentials.
