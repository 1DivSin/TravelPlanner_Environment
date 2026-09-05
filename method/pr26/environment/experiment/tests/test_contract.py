#!/usr/bin/env python3
"""Static and behavioral checks for the compact experiment boundary."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TRAVELPLANNER_ROOT = EXPERIMENT_ROOT.parent
sys.path.insert(0, str(EXPERIMENT_ROOT))

import runner  # noqa: E402


EXPECTED_PUBLIC_TOOL_FILES = {
    "compute_distance.py",
    "list_cities_in_state.py",
    "read.py",
    "run_flow.py",
    "search_accommodations.py",
    "search_attractions.py",
    "search_flights.py",
    "search_restaurants.py",
    "write.py",
}
FORBIDDEN_RUNTIME_TEXT = (
    "validate_" + "travel_plan",
    "TRAVELPLANNER_CASE_" + "STRUCTURED_REFERENCE",
    "TRAVELPLANNER_CASE_" + "CONSTRAINTS",
    "WORKFLOW_EXPLICIT_" + "ACTIVATION_NOTE",
    "WORKFLOW_TREATMENT_" + "V2",
    "WORKFLOW_TREATMENT_" + "V3",
    "WORKFLOW_TREATMENT_" + "V4",
    "WORKFLOW_TREATMENT_" + "V6",
)


def check_source_is_clean() -> None:
    assert runner.DEFAULT_SOURCE == TRAVELPLANNER_ROOT / "psi-agent"
    assert runner.DEFAULT_PSI == TRAVELPLANNER_ROOT.parent / "psi-agent" / ".venv" / "bin" / "psi-agent"
    assert runner.DEFAULT_PSI.is_file()
    assert runner.git_output(runner.DEFAULT_SOURCE, "rev-parse", "HEAD") == runner.EXPECTED_SOURCE_COMMIT
    assert runner.git_output(runner.DEFAULT_SOURCE, "status", "--porcelain") == ""
    assert shutil.which("git") is not None


def check_frozen_assets() -> None:
    cases = runner.load_cases()
    assert tuple(case.case_id for case in cases) == runner.EXPECTED_CASE_IDS
    assert runner.sha256_file(runner.PROMPT_TEMPLATE) == runner.EXPECTED_TEMPLATE_SHA256
    assert runner.partition_cases(cases, 10) == [cases[index : index + 3] for index in range(0, 30, 3)]
    prompts = [runner.render_prompt(case) for case in cases]
    assert len({runner.sha256_text(prompt) for prompt in prompts}) == 30
    assert all(prompt.startswith(runner.TREATMENT_PREFIX) for prompt in prompts)


def check_minimal_agent() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-agent-contract-") as temp:
        agent = Path(temp) / "agent"
        runner.prepare_agent(runner.DEFAULT_SOURCE, agent)
        public_tools = {
            path.name
            for path in (agent / "tools").glob("*.py")
            if not path.name.startswith("_")
        }
        assert public_tools == EXPECTED_PUBLIC_TOOL_FILES
        assert runner.sha256_file(agent / "skills" / "workflow" / "SKILL.md") == runner.EXPECTED_SKILL_SHA256
        assert runner.sha256_file(
            agent / "skills" / "workflow" / "grammar" / "FusionFlow.g4"
        ) == runner.EXPECTED_GRAMMAR_SHA256
        assert runner.sha256_file(
            agent / "skills" / "workflow" / "fusion_flow" / "workflow_runner.py"
        ) == runner.EXPECTED_RUNNER_SHA256
        for path in agent.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for forbidden in FORBIDDEN_RUNTIME_TEXT:
                assert forbidden not in text, f"{forbidden} leaked into {path}"


def check_task_queries() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-task-contract-") as temp:
        temp_root = Path(temp)
        reference = temp_root / "reference.json"
        city_index = temp_root / "cities.txt"
        reference.write_text(
            repr(
                [
                    {"Description": "Restaurants in Example City", "Content": "A, Example City"},
                    {"Description": "Attractions in Example City", "Content": "B, Example City"},
                    {"Description": "Accommodations in Example City", "Content": "C, Example City"},
                    {
                        "Description": "Flight from Alpha to Example City on 2026-01-02",
                        "Content": "Flight Number: X1",
                    },
                    {
                        "Description": "Self-driving from Alpha to Example City",
                        "Content": "self-driving, from Alpha to Example City, duration: 1 hour",
                    },
                ]
            ),
            encoding="utf-8",
        )
        city_index.write_text("Example City\tExample State\n", encoding="utf-8")
        os.environ["CLEAN_CASE_REFERENCE"] = str(reference)
        os.environ["CLEAN_CITY_STATE_INDEX"] = str(city_index)
        sys.path.insert(0, str(EXPERIMENT_ROOT / "task_tools"))
        try:
            restaurants = importlib.import_module("search_restaurants")
            attractions = importlib.import_module("search_attractions")
            accommodations = importlib.import_module("search_accommodations")
            flights = importlib.import_module("search_flights")
            distance = importlib.import_module("compute_distance")
            cities = importlib.import_module("list_cities_in_state")

            async def exercise() -> None:
                assert await restaurants.search_restaurants("Example City") == "A, Example City"
                assert await attractions.search_attractions("Example City") == "B, Example City"
                assert await accommodations.search_accommodations("Example City") == "C, Example City"
                assert await flights.search_flights("Alpha", "Example City", "2026-01-02") == "Flight Number: X1"
                assert "duration: 1 hour" in await distance.compute_distance("Alpha", "Example City")
                assert json.loads(await cities.list_cities_in_state("Example State")) == ["Example City"]

            asyncio.run(exercise())
        finally:
            sys.path.remove(str(EXPERIMENT_ROOT / "task_tools"))
            os.environ.pop("CLEAN_CASE_REFERENCE", None)
            os.environ.pop("CLEAN_CITY_STATE_INDEX", None)


def main() -> int:
    check_source_is_clean()
    check_frozen_assets()
    check_minimal_agent()
    check_task_queries()
    print(json.dumps({"status": "ok", "cases": 30, "public_tools": sorted(EXPECTED_PUBLIC_TOOL_FILES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
