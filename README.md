# TravelPlanner Environment

Reproducible harnesses and evidence for the two Claude Code TravelPlanner
experiments captured in the linked sessions.

The work is intentionally split into four reviewable changes:

1. CC direct-MCP environment (`experiment/cc_pure/`)
2. CC Dynamic Workflow environment (`experiment/` plus `experiment/dynamic/`)
3. CC direct-MCP results (`results/cc/`)
4. CC Dynamic Workflow results (`results/cc-dynamic/`)

The current tree contains the first environment layer. Later layers are kept
on separate branches/PRs so an environment change cannot silently change a
reported result.

See [the CC environment](experiment/cc_pure/README.md) for setup and exact
runtime constraints. The provenance boundary, including the prompt-change
cutoff, is recorded in [docs/provenance.md](docs/provenance.md).
