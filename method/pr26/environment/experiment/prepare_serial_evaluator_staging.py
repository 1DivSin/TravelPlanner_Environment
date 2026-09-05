#!/usr/bin/env python3
"""Prepare post-inference 180-row evaluator snapshots for the 2026-09-03 run.

This helper only reads completed experiment artifacts and frozen manifests.  It
does not invoke a model, the official evaluator, or any credential file.  The
three output roots are deliberately separate because the official evaluator
wrapper accepts only one ``no_workflow`` directory per invocation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


TRAVELPLANNER_ROOT = Path(__file__).resolve().parents[1]
QUEUE_ROOT = TRAVELPLANNER_ROOT / "parallel_experiments" / "20260903"
INPUT_ROOT = QUEUE_ROOT / "inputs"
EVALUATIONS_ROOT = TRAVELPLANNER_ROOT / "evaluations"

FULL_MANIFEST = INPUT_ROOT / "manifest-180.jsonl"
FROZEN_MANIFEST = INPUT_ROOT / "manifest.jsonl"

ODW_ROOT = QUEUE_ROOT / "open_dynamic_workflow"
CC_ROOT = QUEUE_ROOT / "pure_claude_code_cost"
HAITUN_ROOT = QUEUE_ROOT / "pure_haitun_cost" / "output" / "no_workflow"

OFFICIAL_COMMIT = "e52c87f4ac348a3410c46dc3553c519db5ec5e23"
EXPECTED_FULL_MANIFEST_SHA256 = "eb577f6a1b12ebdd7540ed7e0947ec2e6967992558cd8906e1a5eb5c81450df3"
EXPECTED_FROZEN_MANIFEST_SHA256 = "a55b1ba56722c4fae7f020b88a95bc18b171a278088163c4db22b2f804690045"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(value)
    return rows


def require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(TRAVELPLANNER_ROOT))


def canonical_manifest() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    full_path = require_file(FULL_MANIFEST)
    frozen_path = require_file(FROZEN_MANIFEST)
    full_hash = sha256_file(full_path)
    frozen_hash = sha256_file(frozen_path)
    if full_hash != EXPECTED_FULL_MANIFEST_SHA256:
        raise ValueError(f"full manifest hash mismatch: {full_hash}")
    if frozen_hash != EXPECTED_FROZEN_MANIFEST_SHA256:
        raise ValueError(f"frozen manifest hash mismatch: {frozen_hash}")

    full = read_jsonl(full_path)
    frozen = read_jsonl(frozen_path)
    if len(full) != 180 or [str(row.get("case_id")) for row in full] != [str(i) for i in range(1, 181)]:
        raise ValueError("full manifest must contain case IDs 1..180 in order")
    frozen_ids = [str(row.get("case_id")) for row in frozen]
    if len(frozen) != 30 or len(set(frozen_ids)) != 30:
        raise ValueError("frozen manifest must contain 30 unique cases")
    full_by_id = {str(row["case_id"]): row for row in full}
    for row in frozen:
        case_id = str(row.get("case_id"))
        if case_id not in full_by_id or row.get("question") != full_by_id[case_id].get("question"):
            raise ValueError(f"frozen/full manifest query mismatch for case {case_id}")
    return full, frozen


def plan_from_row(row: dict[str, Any], source: Path, *, allow_empty: bool = True) -> list[Any]:
    plan = row.get("plan")
    if not isinstance(plan, list):
        if allow_empty and plan is None:
            return []
        raise ValueError(f"{source}: row lacks a plan list")
    return plan


def make_rows(plans_by_id: dict[int, list[Any]]) -> list[dict[str, Any]]:
    rows = [{"plan": plans_by_id.get(case_id, [])} for case_id in range(1, 181)]
    if len(rows) != 180 or any(not isinstance(row["plan"], list) for row in rows):
        raise AssertionError("internal snapshot row construction failure")
    return rows


def prediction_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")


def source_descriptor(path: Path) -> dict[str, Any]:
    return {"path": rel(path), "sha256": sha256_file(require_file(path))}


def build_odw(full: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    predictions_path = require_file(ODW_ROOT / "predictions.jsonl")
    records_path = require_file(ODW_ROOT / "records.jsonl")
    predictions = read_jsonl(predictions_path)
    records = read_jsonl(records_path)
    if len(predictions) != 180 or len(records) != 180:
        raise ValueError("ODW artifacts must contain 180 rows")
    expected_ids = [str(i) for i in range(1, 181)]
    record_ids = [str(row.get("case_id")) for row in records]
    if record_ids != expected_ids:
        raise ValueError("ODW records are not in case ID order 1..180")
    plans: dict[int, list[Any]] = {}
    status_counts: dict[str, int] = {}
    for position, (record, prediction) in enumerate(zip(records, predictions, strict=True), 1):
        plan = plan_from_row(prediction, predictions_path)
        record_plan = plan_from_row(record, records_path)
        if record_plan != plan:
            raise ValueError(f"ODW prediction/record plan mismatch at case {position}")
        plans[position] = plan
        status = str(record.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    rows = make_rows(plans)
    metadata = {
        "arm": "open_dynamic_workflow",
        "source_predictions": source_descriptor(predictions_path),
        "source_records": source_descriptor(records_path),
        "source_case_count": 180,
        "source_case_ids": list(range(1, 181)),
        "source_status_counts": status_counts,
        "source_nonempty_plan_count": sum(bool(plan) for plan in plans.values()),
        "selection_policy": "all 180 ODW rows, including worker failures as empty plans",
    }
    return rows, metadata


def build_cc(full: list[dict[str, Any]], frozen: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_path = require_file(CC_ROOT / "results.jsonl")
    source_rows = read_jsonl(source_path)
    frozen_ids = [int(row["case_id"]) for row in frozen]
    if len(source_rows) != len(frozen_ids):
        raise ValueError(f"CC result count mismatch: {len(source_rows)} != {len(frozen_ids)}")
    seen: set[int] = set()
    plans: dict[int, list[Any]] = {}
    status_counts: dict[str, int] = {}
    for row, expected_id in zip(source_rows, frozen_ids, strict=True):
        case_id = int(row.get("case_id"))
        if case_id != expected_id or case_id in seen:
            raise ValueError(f"CC result case order/duplicate mismatch at {expected_id}")
        seen.add(case_id)
        if row.get("query") != full[case_id - 1].get("question"):
            raise ValueError(f"CC query mismatch for case {case_id}")
        status = str(row.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        plan = plan_from_row(row, source_path)
        if status == "success":
            if not plan:
                raise ValueError(f"CC success has an empty plan for case {case_id}")
            plans[case_id] = plan
        else:
            # The registered process-error case is represented by an empty
            # plan; no quality-based substitution is performed.
            plans[case_id] = []
    if len(seen) != 30 or status_counts.get("success") != 29:
        raise ValueError(f"expected 29 successful CC rows, got {status_counts}")
    rows = make_rows(plans)
    metadata = {
        "arm": "pure_claude_code_cost",
        "source_results": source_descriptor(source_path),
        "source_case_count": 30,
        "source_case_ids": frozen_ids,
        "source_status_counts": status_counts,
        "source_nonempty_plan_count": sum(bool(plan) for plan in plans.values()),
        "selection_policy": "all 29 successful frozen-30 answers; process-error case 159 as empty plan",
    }
    return rows, metadata


def build_haitun(full: list[dict[str, Any]], frozen: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    predictions_path = require_file(HAITUN_ROOT / "predictions.jsonl")
    records_path = require_file(HAITUN_ROOT / "records.json")
    predictions = read_jsonl(predictions_path)
    records_value = json.loads(records_path.read_text(encoding="utf-8"))
    if not isinstance(records_value, list) or any(not isinstance(row, dict) for row in records_value):
        raise ValueError(f"{records_path}: expected a JSON list of objects")
    records = records_value
    frozen_ids = [int(row["case_id"]) for row in frozen]
    if len(predictions) != 30 or len(records) != 30:
        raise ValueError("Haitun artifacts must contain 30 rows")
    record_ids = [int(row.get("case_id")) for row in records]
    if record_ids != frozen_ids:
        raise ValueError("Haitun records do not match frozen manifest order")
    if any(str(row.get("status")) != "completed" for row in records):
        raise ValueError("Haitun source contains a non-completed record")
    plans: dict[int, list[Any]] = {}
    for prediction, case_id in zip(predictions, frozen_ids, strict=True):
        plan = plan_from_row(prediction, predictions_path, allow_empty=False)
        if not plan:
            raise ValueError(f"Haitun completed case {case_id} has an empty plan")
        plans[case_id] = plan
        if prediction.get("case_id") is not None and int(prediction["case_id"]) != case_id:
            raise ValueError(f"Haitun prediction case mismatch for case {case_id}")
        if full[case_id - 1].get("question") != frozen[record_ids.index(case_id)].get("question"):
            raise ValueError(f"Haitun query alignment mismatch for case {case_id}")
    rows = make_rows(plans)
    metadata = {
        "arm": "pure_haitun_cost",
        "source_predictions": source_descriptor(predictions_path),
        "source_records": source_descriptor(records_path),
        "source_case_count": 30,
        "source_case_ids": frozen_ids,
        "source_status_counts": {"completed": 30},
        "source_nonempty_plan_count": sum(bool(plan) for plan in plans.values()),
        "selection_policy": "all 30 completed frozen-30 answers; remaining validation rows as empty plans",
    }
    return rows, metadata


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_snapshot(destination: Path, arm_dirname: str, rows: list[dict[str, Any]], metadata: dict[str, Any], full: list[dict[str, Any]], frozen: list[dict[str, Any]]) -> dict[str, Any]:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing staging root: {destination}")
    arm_root = destination / arm_dirname
    arm_root.mkdir(parents=True)
    predictions = arm_root / "predictions.jsonl"
    payload = prediction_bytes(rows)
    predictions.write_bytes(payload)
    empty_ids = [case_id for case_id, row in enumerate(rows, 1) if not row["plan"]]
    manifest = {
        "schema_version": 1,
        "arm": metadata["arm"],
        "evaluator_split": "validation",
        "prediction_rows": len(rows),
        "prediction_sha256": sha256_file(predictions),
        "full_manifest": {"path": rel(FULL_MANIFEST), "sha256": sha256_file(FULL_MANIFEST), "case_count": len(full)},
        "frozen_manifest": {"path": rel(FROZEN_MANIFEST), "sha256": sha256_file(FROZEN_MANIFEST), "case_count": len(frozen)},
        "empty_plan_case_ids": empty_ids,
        "nonempty_plan_count": len(rows) - len(empty_ids),
        "unfinished_policy": "empty_plan_counted_as_failure",
        "quality_or_verdict_based_selection": False,
        **metadata,
    }
    write_json(arm_root / "snapshot-manifest.json", manifest)
    provenance = {
        "schema_version": 1,
        "status": "prepared_for_post_inference_evaluation",
        "arm": metadata["arm"],
        "evaluator_visibility": "post-inference only",
        "evaluator_used_during_inference": False,
        "official_evaluator": {
            "repository": "https://github.com/OSU-NLP-Group/TravelPlanner",
            "commit": OFFICIAL_COMMIT,
            "split": "validation",
        },
        "prediction": {
            "path": rel(predictions),
            "rows": len(rows),
            "sha256": sha256_file(predictions),
        },
        "snapshot_manifest": {
            "path": rel(arm_root / "snapshot-manifest.json"),
            "sha256": sha256_file(arm_root / "snapshot-manifest.json"),
        },
        "canonical_case_order": "full manifest case_id 1..180",
        "empty_plan_case_ids": empty_ids,
        "credential_values_recorded": False,
        "credentials_read_by_this_helper": False,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    write_json(destination / "provenance.json", provenance)
    return manifest


def main() -> int:
    full, frozen = canonical_manifest()
    # Build and validate all source mappings before creating any destination,
    # so a malformed source cannot leave a misleading partial staging set.
    odw_rows, odw_meta = build_odw(full)
    cc_rows, cc_meta = build_cc(full, frozen)
    haitun_rows, haitun_meta = build_haitun(full, frozen)
    destinations = (
        (EVALUATIONS_ROOT / "serial-comparison-20260903-odw", "auto_workflow", odw_rows, odw_meta),
        (EVALUATIONS_ROOT / "serial-comparison-20260903-cc30", "no_workflow", cc_rows, cc_meta),
        (EVALUATIONS_ROOT / "serial-comparison-20260903-haitun30", "no_workflow", haitun_rows, haitun_meta),
    )
    for destination, _arm_dirname, _rows, _metadata in destinations:
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite existing staging root: {destination}")
    manifests = [
        write_snapshot(destination, arm_dirname, rows, metadata, full, frozen)
        for destination, arm_dirname, rows, metadata in destinations
    ]
    print(json.dumps({"staging_roots": [rel(item[0]) for item in destinations], "manifests": manifests}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
