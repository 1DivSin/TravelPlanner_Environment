#!/public/home/sychen/cxy/workflow/psi-agent/.venv/bin/python3
"""Launch psi-agent with the frozen managed-runtime config-read adaptation."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ADAPTED_RUN_MODULE = WORKSPACE_ROOT / "psi-agent" / "src" / "psi_agent" / "_run.py"
EXPECTED_ADAPTED_RUN_SHA256 = "0444ba90b8998b4ff5aad4e03118ae90abf00ddd7c02c64c6555cffccac4b9d8"


def install_adapted_run_module() -> None:
    digest = hashlib.sha256(ADAPTED_RUN_MODULE.read_bytes()).hexdigest()
    if digest != EXPECTED_ADAPTED_RUN_SHA256:
        raise RuntimeError("managed-runtime adapter source hash mismatch")

    spec = importlib.util.spec_from_file_location("psi_agent._run", ADAPTED_RUN_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load managed-runtime adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


def main() -> None:
    install_adapted_run_module()
    from psi_agent.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
