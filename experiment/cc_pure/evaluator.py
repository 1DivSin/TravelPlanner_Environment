"""Run the official TravelPlanner validation checkers for a selected prefix."""

from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def literal(value: Any) -> Any:
    return ast.literal_eval(value) if isinstance(value, str) else value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def compact_attempts(
    attempts: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    selected: set[int],
) -> list[dict[str, Any]]:
    """Keep the last successful attempt, or the last failure, per index."""
    grouped: dict[int, list[dict[str, Any]]] = {idx: [] for idx in selected}
    for row in attempts:
        idx = int(row.get("idx", -1))
        if idx in grouped:
            grouped[idx].append(row)

    query_by_idx = {int(row["idx"]): {"idx": row["idx"], "query": row.get("query", "")} for row in queries}
    compacted: list[dict[str, Any]] = []
    for idx in sorted(selected):
        candidates = grouped[idx]
        successes = [row for row in candidates if row.get("plan") and not row.get("error")]
        if successes:
            compacted.append(dict(successes[-1]))
        elif candidates:
            compacted.append(dict(candidates[-1]))
        else:
            row = dict(query_by_idx.get(idx, {"idx": idx, "query": ""}))
            row.update(plan=None, error="not_attempted")
            compacted.append(row)
    return compacted


def _box_value(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value


def count_box(box: dict[str, Any] | None) -> tuple[int, int]:
    values = [_box_value(value) for value in (box or {}).values()]
    values = [value for value in values if value is not None]
    return sum(bool(value) for value in values), len(values)


def box_pass(box: dict[str, Any] | None) -> bool:
    passed, tested = count_box(box)
    return tested > 0 and passed == tested


def metric(passed: int, total: int) -> dict[str, int | float | None]:
    return {"passed": passed, "total": total, "rate": passed / total if total else None}


def _load_official(travelplanner_root: Path):
    from datasets import load_dataset

    evaluation_dir = travelplanner_root / "evaluation"
    inserted = [str(travelplanner_root), str(evaluation_dir)]
    added: list[str] = []
    for path in reversed(inserted):
        if path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    try:
        from commonsense_constraint import evaluation as commonsense_checker
        from hard_constraint import evaluation as hard_checker

        dataset = load_dataset("osunlp/TravelPlanner", "validation")["validation"]
        return dataset, commonsense_checker, hard_checker, added
    except Exception:
        for path in added:
            if path in sys.path:
                sys.path.remove(path)
        raise


def evaluate_predictions(
    predictions: list[dict[str, Any]],
    selected: set[int],
    travelplanner_root: Path,
) -> dict[str, Any]:
    indices = [int(row["idx"]) for row in predictions]
    if len(indices) != len(selected) or set(indices) != selected:
        raise ValueError("prediction indices must match selected indices exactly once")

    dataset, commonsense_checker, hard_checker, added = _load_official(travelplanner_root)
    evaluation_dir = travelplanner_root / "evaluation"
    original_cwd = Path.cwd()
    query_by_idx: dict[int, dict[str, Any]] = {}
    per_index: list[dict[str, Any]] = []
    try:
        os.chdir(evaluation_dir)
        for idx, raw_query in enumerate(dataset, start=1):
            if idx in selected:
                query = dict(raw_query)
                query["date"] = literal(query.get("date"))
                query["local_constraint"] = literal(query.get("local_constraint"))
                query_by_idx[idx] = query

        if set(query_by_idx) != selected:
            raise ValueError("validation indices must contain every selected index exactly once")

        by_idx = {int(row["idx"]): row for row in predictions}
        for idx in sorted(selected):
            prediction = by_idx[idx]
            plan = prediction.get("plan")
            delivered = bool(plan) and not prediction.get("error")
            row: dict[str, Any] = {
                "idx": idx,
                "level": query_by_idx[idx].get("level"),
                "days": query_by_idx[idx].get("days"),
                "delivered": delivered,
                "commonsense_box": None,
                "commonsense_pass": False,
                "commonsense_passed": 0,
                "commonsense_tested": 0,
                "hard_box": None,
                "hard_pass": False,
                "hard_passed": 0,
                "hard_tested": 0,
                "final_pass": False,
                "cost_usd": prediction.get("cost_usd") or 0,
                "input_tokens": prediction.get("input_tokens") or 0,
                "output_tokens": prediction.get("output_tokens") or 0,
                "model": prediction.get("model"),
                "error": prediction.get("error") or (None if delivered else "missing_plan"),
            }
            if delivered:
                commonsense_box = commonsense_checker(query_by_idx[idx], plan)
                commonsense_passed, commonsense_tested = count_box(commonsense_box)
                commonsense_ok = box_pass(commonsense_box)
                row.update(
                    commonsense_box=commonsense_box,
                    commonsense_pass=commonsense_ok,
                    commonsense_passed=commonsense_passed,
                    commonsense_tested=commonsense_tested,
                )
                prerequisites_ok = all(
                    bool(_box_value(commonsense_box.get(key)))
                    for key in ("is_not_absent", "is_valid_information_in_sandbox")
                )
                if prerequisites_ok:
                    hard_box = hard_checker(query_by_idx[idx], plan)
                    hard_passed, hard_tested = count_box(hard_box)
                    hard_ok = box_pass(hard_box)
                    row.update(
                        hard_box=hard_box,
                        hard_pass=hard_ok,
                        hard_passed=hard_passed,
                        hard_tested=hard_tested,
                        final_pass=commonsense_ok and hard_ok,
                    )
            per_index.append(row)
    finally:
        os.chdir(original_cwd)
        for path in added:
            if path in sys.path:
                sys.path.remove(path)

    commonsense_passed = sum(int(row["commonsense_passed"]) for row in per_index)
    commonsense_tested = sum(int(row["commonsense_tested"]) for row in per_index)
    hard_passed = sum(int(row["hard_passed"]) for row in per_index)
    hard_tested = sum(int(row["hard_tested"]) for row in per_index)
    return {
        "evaluated_at": utc_now(),
        "selected_indices": sorted(selected),
        "per_index": per_index,
        "summary": {
            "selected_count": len(selected),
            "delivery": metric(sum(bool(row["delivered"]) for row in per_index), len(selected)),
            "commonsense": {
                "micro": metric(commonsense_passed, commonsense_tested),
                "macro": metric(sum(bool(row["commonsense_pass"]) for row in per_index), len(selected)),
            },
            "hard": {
                "micro": metric(hard_passed, hard_tested),
                "macro": metric(sum(bool(row["hard_pass"]) for row in per_index), len(selected)),
            },
            "final": metric(sum(bool(row["final_pass"]) for row in per_index), len(selected)),
            "cost_usd": sum(float(row.get("cost_usd") or 0) for row in per_index),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in per_index),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in per_index),
        },
    }


def write_checkpoint(
    attempts_path: Path,
    queries_path: Path,
    selected: set[int],
    travelplanner_root: Path,
    scores_path: Path,
    failure_path: Path,
) -> bool:
    """Score a checkpoint; return False and persist a reason when unavailable."""
    # A resumed run may have a stale result from an earlier attempt; keep one
    # authoritative status for this checkpoint.
    scores_path.unlink(missing_ok=True)
    failure_path.unlink(missing_ok=True)
    scores_path.with_name(f"scores-{len(selected)}-not-reached.log").unlink(missing_ok=True)
    try:
        scores = evaluate_predictions(
            compact_attempts(load_jsonl(attempts_path), load_jsonl(queries_path), selected),
            selected,
            travelplanner_root,
        )
        scores_path.write_text(json.dumps(scores, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        failure_path.write_text(
            json.dumps(
                {
                    "status": "evaluation_failed",
                    "checkpoint": len(selected),
                    "failed_at": utc_now(),
                    "reason": f"{type(exc).__name__}: {exc}",
                    "fallback_used": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return False
