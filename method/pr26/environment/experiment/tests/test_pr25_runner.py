#!/usr/bin/env python3
"""Contract checks for the PR #25 frozen candidate configuration."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))

import pr25_runner  # noqa: E402
import runner  # noqa: E402


def test_key_file_contributes_only_the_api_key(tmp_path: Path) -> None:
    credential = tmp_path / "arm.env"
    credential.write_text(
        "PSI_AI_PROVIDER=wrong-provider\n"
        "PSI_AI_MODEL=wrong-model\n"
        "PSI_AI_API_KEY=test-only-key\n"
        "PSI_AI_BASE_URL=https://wrong.invalid\n",
        encoding="utf-8",
    )

    loaded = pr25_runner.load_key_only(credential)

    assert loaded == {
        "PSI_AI_PROVIDER": pr25_runner.FROZEN_PROVIDER,
        "PSI_AI_MODEL": pr25_runner.FROZEN_MODEL,
        "PSI_AI_API_KEY": "test-only-key",
        "PSI_AI_BASE_URL": pr25_runner.FROZEN_BASE_URL,
    }
    assert pr25_runner._credential_fingerprint == hashlib.sha256(b"test-only-key").hexdigest()


def test_candidate_snapshot_includes_the_required_authoring_reference() -> None:
    with tempfile.TemporaryDirectory(prefix="pr25-frozen-contract-") as temp:
        temp_root = Path(temp)
        agent = temp_root / "agent"
        workspace = temp_root / "workspace"
        pr25_runner.configure()
        runner.prepare_agent(pr25_runner.DEFAULT_SOURCE, agent)
        runner.prepare_workspace(agent, workspace)

        guide = workspace / "skills" / "workflow" / "references" / "workflow-authoring-guide.md"
        assert runner.sha256_file(guide) == pr25_runner.EXPECTED_GUIDE_SHA256


def test_candidate_is_pinned_to_pr25_head() -> None:
    assert runner.git_output(pr25_runner.DEFAULT_SOURCE, "rev-parse", "HEAD") == pr25_runner.EXPECTED_SOURCE_COMMIT
    assert runner.git_output(pr25_runner.DEFAULT_SOURCE, "status", "--porcelain") == ""
    assert pr25_runner.EXPECTED_BASE_COMMIT == runner.git_output(
        pr25_runner.DEFAULT_SOURCE,
        "merge-base",
        "origin/main",
        "HEAD",
    )


def test_runtime_adapter_starts_with_a_domain_neutral_empty_config(tmp_path: Path) -> None:
    config = tmp_path / "empty.yml"
    config.write_text("[]\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(pr25_runner.DEFAULT_SOURCE / "src")
    completed = subprocess.run(
        [sys.executable, str(pr25_runner.DEFAULT_PSI), "run", str(config)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
