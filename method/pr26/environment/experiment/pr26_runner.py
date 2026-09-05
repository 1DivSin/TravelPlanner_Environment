#!/usr/bin/env python3
"""Run the user-selected validation subset against psi-agent PR #26."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import runner


TRAVELPLANNER_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = TRAVELPLANNER_ROOT / "worktrees" / "generic-global-verifier"
DEFAULT_PSI = Path(__file__).resolve().parent / "psi_agent_runtime_adapter.py"
EXPECTED_SOURCE_COMMIT = "1b211f34cfff8738e6f7f42024ee715a392b90a7"
EXPECTED_BASE_COMMIT = "65a89066e00d677a694e4f2fe13fcaab1eb7f2aa"
EXPECTED_SKILL_SHA256 = "3d4dcb20836d2075141a2eb0576fdc71798fcd1c0b35b6a8935cc758c80a7a9b"
EXPECTED_GRAMMAR_SHA256 = "72a7765b626a1447d0557bf8fcef1aa5e38167249c4af57daec93e76242c4328"
EXPECTED_RUNNER_SHA256 = "3d0593e89f26d29435c7457e4f17e87000fed99f8c9b85260f9516843ced182f"
EXPECTED_README_SHA256 = "a71f7e1a217b92830c8829d1841f374bd246fddc5994bdd53d2557b7e3c4caa3"
EXPECTED_GUIDE_SHA256 = "0f070f42b8cb3e3f3b2945dcbecbdb36fe4b1f2e5161c9924bbbd26a3935a792"
EXPECTED_SYSTEM_SHA256 = "b4b0b736c1cce95a3fe465063599ecdfb80c2d65f2186285bd2bf83c8e73f20a"
FROZEN_PROVIDER = "anthropic"
FROZEN_MODEL = "claude-opus-5"
FROZEN_BASE_URL = "https://api2.penguinsaichat.dpdns.org"
METHOD_HYPOTHESIS = (
    "Requiring an independently authored verifier to derive visible relation predicates and "
    "audit the complete candidate will catch cross-item inconsistencies that item-wise checks "
    "miss, without importing hidden evaluator rules."
)

_base_prepare_agent = runner.prepare_agent
_base_prepare_workspace = runner.prepare_workspace
_base_provenance = runner.provenance
_credential_fingerprint: str | None = None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_key_only(path: Path, visited: set[Path] | None = None) -> dict[str, str]:
    """Read only the method-arm API key; ignore every other env-file setting."""

    del visited
    values: list[str] = []
    for raw in path.resolve().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, value = line.split("=", 1)
        if key.strip() == "PSI_AI_API_KEY":
            values.append(_unquote(value))
    if len(values) != 1 or not values[0]:
        raise ValueError("credential file must contain exactly one non-empty PSI_AI_API_KEY")

    global _credential_fingerprint
    _credential_fingerprint = hashlib.sha256(values[0].encode("utf-8")).hexdigest()
    return {
        "PSI_AI_PROVIDER": FROZEN_PROVIDER,
        "PSI_AI_MODEL": FROZEN_MODEL,
        "PSI_AI_API_KEY": values[0],
        "PSI_AI_BASE_URL": FROZEN_BASE_URL,
    }


def prepare_agent(source: Path, destination: Path) -> None:
    _base_prepare_agent(source, destination)
    expected = {
        destination / "skills" / "workflow" / "README.md": EXPECTED_README_SHA256,
        destination / "skills" / "workflow" / "references" / "workflow-authoring-guide.md": EXPECTED_GUIDE_SHA256,
        destination / "systems" / "system.py": EXPECTED_SYSTEM_SHA256,
    }
    for path, digest in expected.items():
        if runner.sha256_file(path) != digest:
            raise ValueError(f"PR #26 snapshot hash mismatch: {path.relative_to(destination)}")


def prepare_workspace(agent: Path, workspace: Path) -> None:
    _base_prepare_workspace(agent, workspace)
    source = agent / "skills" / "workflow" / "references"
    target = workspace / "skills" / "workflow" / "references"
    shutil.copytree(source, target)


def provenance(**kwargs: Any) -> dict[str, Any]:
    value = _base_provenance(**kwargs)
    source = kwargs["source"]
    commits = runner.git_output(
        source,
        "log",
        "--reverse",
        "--format=%H%x09%s",
        f"{EXPECTED_BASE_COMMIT}..HEAD",
    ).splitlines()
    value.update(
        {
            "method_hypothesis": METHOD_HYPOTHESIS,
            "method_owner": "Workflow Skill adversarial-verifier authoring guidance",
            "included_method_prs": [
                {"pr": 21, "commit": "4548a1f1", "scope": "domain-neutral adversarial verifier authoring"},
                {"pr": 22, "commit": "8c7f51e6", "scope": "programmatic Artifact schema execution"},
                {"pr": 24, "commit": "6d22e72b", "scope": "revert benchmark-related authoring defaults"},
                {"pr": 25, "commit": "65a89066", "scope": "dynamic Workflow authoring guidance"},
            ],
            "candidate_pr": 26,
            "candidate_commits": [
                {"commit": row.split("\t", 1)[0], "subject": row.split("\t", 1)[1]}
                for row in commits
            ],
            "arm": "pr26-generic-global-verifier",
            "candidate_config_sha256": runner.sha256_file(Path(__file__)),
            "credential_arm": "psi-agent-method",
            "credential_fingerprint_sha256": _credential_fingerprint,
            "credential_file_non_key_settings_used": False,
            "workspace_authoring_reference_sha256": EXPECTED_GUIDE_SHA256,
            "runtime_compatibility_adaptation": {
                "file": "src/psi_agent/_run.py",
                "scope": "replace AnyIO async config-file read with pathlib synchronous read before startup",
                "reason": "avoid Python 3.14/AnyIO thread-pool startup stall",
                "method_or_prompt_change": False,
                "adapted_file_sha256": "0444ba90b8998b4ff5aad4e03118ae90abf00ddd7c02c64c6555cffccac4b9d8",
                "launcher_sha256": runner.sha256_file(
                    Path(__file__).resolve().parent / "psi_agent_runtime_adapter.py"
                ),
            },
        }
    )
    return value


def configure() -> None:
    runner.DEFAULT_SOURCE = DEFAULT_SOURCE
    runner.DEFAULT_PSI = DEFAULT_PSI
    runner.EXPECTED_SOURCE_COMMIT = EXPECTED_SOURCE_COMMIT
    runner.EXPECTED_BASE_COMMIT = EXPECTED_BASE_COMMIT
    runner.EXPECTED_SKILL_SHA256 = EXPECTED_SKILL_SHA256
    runner.EXPECTED_GRAMMAR_SHA256 = EXPECTED_GRAMMAR_SHA256
    runner.EXPECTED_RUNNER_SHA256 = EXPECTED_RUNNER_SHA256
    runner.load_env_file = load_key_only
    runner.prepare_agent = prepare_agent
    runner.prepare_workspace = prepare_workspace
    runner.provenance = provenance


def main() -> int:
    configure()
    return runner.main()


if __name__ == "__main__":
    raise SystemExit(main())
