#!/usr/bin/env python3
"""Run the pinned official TravelPlanner evaluator on completed experiment arms."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
OFFICIAL_ROOT = ROOT / "vendor" / "travelplanner-official"
EVALUATION_ROOT = OFFICIAL_ROOT / "evaluation"
STUB_ROOT = OFFICIAL_ROOT / "_stubs"
EXPECTED_OFFICIAL_COMMIT = "e52c87f4ac348a3410c46dc3553c519db5ec5e23"
DEFAULT_ARMS = {
    "auto-workflow": ROOT / ".runs-validation-parallel-merged-final" / "auto_workflow" / "predictions.jsonl",
    "no-workflow": ROOT / ".runs-validation" / "no_workflow" / "predictions.jsonl",
}
RUN_ROOT_ARM_DIRECTORIES = {
    "auto-workflow": "auto_workflow",
    "travelplanner-skill": "travelplanner_skill",
    "no-workflow": "no_workflow",
}
SCORE_PATTERN = re.compile(r"^(.+ Rate): ([0-9.eE+-]+)%$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_predictions(path: Path, expected_rows: int) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = path.read_text(encoding="utf-8").splitlines()
    if len(rows) != expected_rows:
        raise ValueError(f"{path}: expected {expected_rows} rows, found {len(rows)}")
    for number, line in enumerate(rows, 1):
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("plan"), list):
            raise ValueError(f"{path}:{number}: expected a JSON object with a plan list")


def _prepare_evaluator_input(path: Path) -> tuple[Path, list[dict[str, object]]]:
    """Serialize supported structured resource values into official text form."""
    output = path.parent / "official-evaluator-input.jsonl"
    transformations: list[dict[str, object]] = []
    normalized_rows: list[str] = []
    for row_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = json.loads(line)
        for day_number, day in enumerate(value["plan"], 1):
            if not isinstance(day, dict):
                raise ValueError(f"{path}:{row_number}: day {day_number} must be an object")
            accommodation = day.get("accommodation")
            if isinstance(accommodation, dict):
                name = accommodation.get("NAME")
                city = accommodation.get("city")
                if not isinstance(name, str) or not isinstance(city, str):
                    raise ValueError(
                        f"{path}:{row_number}: day {day_number} accommodation object lacks string NAME/city"
                    )
                day["accommodation"] = f"{name}, {city}"
                transformations.append(
                    {
                        "row": row_number,
                        "day": day_number,
                        "field": "accommodation",
                        "operation": "resource_object_to_name_city",
                    }
                )
            elif accommodation is not None and not isinstance(accommodation, str):
                raise ValueError(
                    f"{path}:{row_number}: day {day_number} accommodation must be a string"
                )
        normalized_rows.append(json.dumps(value, ensure_ascii=False))
    output.write_text("\n".join(normalized_rows) + "\n", encoding="utf-8")
    return output, transformations


def _official_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=OFFICIAL_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _parse_scores(stdout: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for line in stdout.splitlines():
        match = SCORE_PATTERN.match(line.strip())
        if match:
            scores[match.group(1)] = float(match.group(2)) / 100.0
    return scores


def _evaluate(arm: str, predictions: Path, split: str) -> dict[str, object]:
    output_root = predictions.parent
    stdout_path = output_root / "official-evaluator.stdout.txt"
    stderr_path = output_root / "official-evaluator.stderr.txt"
    result_path = output_root / "official-evaluator.json"
    evaluator_input, transformations = _prepare_evaluator_input(predictions)

    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(STUB_ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    command = [
        sys.executable,
        "eval.py",
        "--set_type",
        split,
        "--evaluation_file_path",
        str(evaluator_input.resolve()),
    ]
    completed = subprocess.run(
        command,
        cwd=EVALUATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    scores = _parse_scores(completed.stdout)
    record: dict[str, object] = {
        "arm": arm,
        "split": split,
        "prediction_rows": len(predictions.read_text(encoding="utf-8").splitlines()),
        "prediction_sha256": _sha256(predictions),
        "evaluator_input_path": str(evaluator_input.relative_to(REPO_ROOT)),
        "evaluator_input_sha256": _sha256(evaluator_input),
        "input_transformations": transformations,
        "official_repository": "https://github.com/OSU-NLP-Group/TravelPlanner",
        "official_commit": _official_commit(),
        "official_evaluator_unmodified": True,
        "query_source": str((ROOT / "travelplanner" / f"{split}.csv").resolve()),
        "scores": scores,
        "returncode": completed.returncode,
        "stdout_path": str(stdout_path.relative_to(REPO_ROOT)),
        "stderr_path": str(stderr_path.relative_to(REPO_ROOT)),
    }
    result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"official evaluator failed for {arm}; see {stderr_path}")
    if len(scores) != 6:
        raise RuntimeError(f"official evaluator returned {len(scores)} metrics for {arm}, expected 6")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("train", "validation"), default="validation")
    parser.add_argument(
        "--arm",
        choices=(*RUN_ROOT_ARM_DIRECTORIES, "both", "workflow-vs-skill"),
        default="both",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        help="evaluate <run-root>/<arm>/predictions.jsonl instead of the archived default runs",
    )
    parser.add_argument("--serial", action="store_true", help="evaluate arms sequentially")
    args = parser.parse_args()

    commit = _official_commit()
    if commit != EXPECTED_OFFICIAL_COMMIT:
        raise RuntimeError(f"official evaluator commit changed: {commit}")
    expected_rows = 180 if args.split == "validation" else 45
    available = (
        {
            arm: args.run_root / directory / "predictions.jsonl"
            for arm, directory in RUN_ROOT_ARM_DIRECTORIES.items()
        }
        if args.run_root is not None
        else DEFAULT_ARMS
    )
    if args.arm == "both":
        selected_names = ("auto-workflow", "no-workflow")
    elif args.arm == "workflow-vs-skill":
        selected_names = ("auto-workflow", "travelplanner-skill")
    else:
        selected_names = (args.arm,)
    selected = {arm: available[arm] for arm in selected_names}
    for path in selected.values():
        _validate_predictions(path, expected_rows)

    if len(selected) == 2 and not args.serial:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                arm: executor.submit(_evaluate, arm, path, args.split)
                for arm, path in selected.items()
            }
            results = {arm: future.result() for arm, future in futures.items()}
    else:
        results = {arm: _evaluate(arm, path, args.split) for arm, path in selected.items()}
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
