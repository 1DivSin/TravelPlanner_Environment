# TravelPlanner PR #26 experiment

This directory records the isolated TravelPlanner experiment based on 1DivSin/psi-agent PR #26, commit 1b211f34cfff8738e6f7f42024ee715a392b90a7.

Contents: environment runner/adapter, frozen 180-case manifest, validation data, and merged results with responses, answers, workflows, predictions, and evaluator logs. API credentials are excluded.

The run used 15 isolated shards of 12 cases each with claude-opus-5. The recorded evaluator output reports 0% delivery because the old 180-case runner did not expose run_flow in the session; this is retained as an execution/runtime diagnostic.
