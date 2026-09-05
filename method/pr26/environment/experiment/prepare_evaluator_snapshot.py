#!/usr/bin/env python3
"""Build a 180-row evaluator snapshot from process-complete run answers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TRAVELPLANNER_ROOT = ROOT.parent
DEFAULT_RUN_ROOT = TRAVELPLANNER_ROOT / "runs" / "main-frozen30-20260901-r3"
FROZEN_IDS = (
    1, 11, 14, 17, 28, 33, 38, 41, 46, 48,
    70, 72, 77, 81, 83, 100, 110, 113, 116, 118,
    123, 124, 138, 144, 146, 151, 159, 161, 162, 163,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    merged = run_root / "merged" / "auto_workflow"
    output_root = args.output_root.resolve()
    arm_root = output_root / "auto_workflow"
    if output_root.exists():
        raise FileExistsError(f"output root already exists: {output_root}")
    arm_root.mkdir(parents=True)

    records_path = merged / "records.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))
    selected_ids = tuple(int(record["case_id"]) for record in records)
    expected_order = tuple(case_id for case_id in FROZEN_IDS if case_id in set(selected_ids))
    if not selected_ids or len(set(selected_ids)) != len(selected_ids) or selected_ids != expected_order:
        raise ValueError(f"case IDs must be a unique frozen-order subset: {selected_ids}")

    completed_ids = {
        int(record["case_id"])
        for record in records
        if record.get("status") == "completed_process"
    }
    failed_ids = set(selected_ids) - completed_ids
    if completed_ids | failed_ids != set(selected_ids):
        raise ValueError("run records do not partition the selected cases")

    rows: list[dict[str, Any]] = []
    sources: dict[str, dict[str, str]] = {}
    for case_id in range(1, 181):
        if case_id not in completed_ids:
            rows.append({"plan": []})
            continue
        answer_path = merged / "cases" / str(case_id) / "answer.json"
        answer = json.loads(answer_path.read_text(encoding="utf-8"))
        if not isinstance(answer, dict) or not isinstance(answer.get("plan"), list):
            raise ValueError(f"invalid completed answer: {answer_path}")
        rows.append(answer)
        sources[str(case_id)] = {
            "path": str(answer_path.relative_to(TRAVELPLANNER_ROOT)),
            "sha256": sha256_file(answer_path),
        }

    predictions = arm_root / "predictions.jsonl"
    predictions.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = {
        "arm": "auto_workflow",
        "source_run": str(run_root.relative_to(TRAVELPLANNER_ROOT)),
        "source_records_path": str(records_path.relative_to(TRAVELPLANNER_ROOT)),
        "source_records_sha256": sha256_file(records_path),
        "selected_case_ids": list(selected_ids),
        "completed_count": len(completed_ids),
        "completed_case_ids": sorted(completed_ids),
        "failed_process_case_ids": sorted(failed_ids),
        "prediction_rows": len(rows),
        "selection_policy": "all process-complete answers from the frozen-order run",
        "unfinished_policy": "empty_plan_counted_as_failure",
        "quality_or_verdict_based_selection": False,
        "sources": sources,
        "predictions_sha256": sha256_file(predictions),
    }
    (arm_root / "snapshot-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
