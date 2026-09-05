"""Compact and score exactly the selected TravelPlanner validation queries."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


Box = dict[str, Any]
Checker = Callable[[dict[str, Any], list[dict[str, Any]]], Box]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_indices_spec(spec: str) -> set[int]:
    """Parse comma-separated integer indices into a set."""
    return {int(chunk.strip()) for chunk in spec.split(",") if chunk.strip()}


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def compact_attempts(
    attempts: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    selected: set[int],
) -> list[dict[str, Any]]:
    """Choose one deterministic record for every selected index."""
    grouped: dict[int, list[dict[str, Any]]] = {idx: [] for idx in selected}
    for attempt in attempts:
        idx = int(attempt["idx"])
        if idx in selected:
            grouped[idx].append(attempt)

    query_by_idx = {int(query["idx"]): query for query in queries}
    compacted: list[dict[str, Any]] = []
    for idx in sorted(selected):
        candidates = grouped[idx]
        successes = [row for row in candidates if row.get("plan") and not row.get("error")]
        if successes:
            compacted.append(dict(successes[-1]))
        elif candidates:
            compacted.append(dict(candidates[-1]))
        else:
            row = dict(query_by_idx.get(idx, {}))
            row.update(idx=idx, plan=None, error="not_attempted")
            compacted.append(row)
    return compacted


def literal(value: Any) -> Any:
    """Parse serialized Python literals without executing code."""
    return ast.literal_eval(value) if isinstance(value, str) else value


def _box_value(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value


def count_box(box: Box | None) -> tuple[int, int]:
    """Return (passed, tested) for an official constraint result box."""
    tested = [_box_value(value) for value in (box or {}).values()]
    tested = [value for value in tested if value is not None]
    return sum(bool(value) for value in tested), len(tested)


def box_pass(box: Box | None) -> bool:
    passed, tested = count_box(box)
    return tested > 0 and passed == tested


def _metric(passed: int, total: int) -> dict[str, int | float | None]:
    return {"passed": passed, "total": total, "rate": passed / total if total else None}


def make_summary(per_index: list[dict[str, Any]], selected_count: int) -> dict[str, Any]:
    """Aggregate macro rates over selected queries and micro rates over tested boxes."""
    commonsense_passed = sum(int(row.get("commonsense_passed", 0)) for row in per_index)
    commonsense_tested = sum(int(row.get("commonsense_tested", 0)) for row in per_index)
    hard_passed = sum(int(row.get("hard_passed", 0)) for row in per_index)
    hard_tested = sum(int(row.get("hard_tested", 0)) for row in per_index)
    return {
        "selected_count": selected_count,
        "delivery": _metric(sum(bool(row.get("delivered")) for row in per_index), selected_count),
        "commonsense": {
            "micro": _metric(commonsense_passed, commonsense_tested),
            "macro": _metric(sum(bool(row.get("commonsense_pass")) for row in per_index), selected_count),
        },
        "hard": {
            "micro": _metric(hard_passed, hard_tested),
            "macro": _metric(sum(bool(row.get("hard_pass")) for row in per_index), selected_count),
        },
        "final": _metric(sum(bool(row.get("final_pass")) for row in per_index), selected_count),
        "cost_usd": sum(float(row.get("cost_usd") or 0) for row in per_index),
    }


def _load_official() -> tuple[Any, Checker, Checker]:
    """Load the validation split and official TravelPlanner constraint checkers lazily."""
    from datasets import load_dataset
    from commonsense_constraint import evaluation as commonsense_checker
    from hard_constraint import evaluation as hard_checker

    dataset = load_dataset("osunlp/TravelPlanner", "validation")["validation"]
    return dataset, commonsense_checker, hard_checker


def _constraint_passed(box: Box, key: str) -> bool:
    return bool(_box_value(box.get(key)))


def evaluate(
    predictions: list[dict[str, Any]],
    selected: set[int],
    travelplanner_root: str | Path,
) -> dict[str, Any]:
    """Score selected predictions with the official validation evaluator."""
    prediction_indices = [int(row["idx"]) for row in predictions]
    if len(prediction_indices) != len(selected) or set(prediction_indices) != selected:
        raise ValueError("prediction indices must match selected indices exactly once")

    root = Path(travelplanner_root).resolve()
    evaluation_dir = root / "evaluation"
    original_cwd = Path.cwd()
    inserted_paths = [str(root), str(evaluation_dir)]
    added_paths: list[str] = []
    for path in reversed(inserted_paths):
        if path not in sys.path:
            sys.path.insert(0, path)
            added_paths.append(path)

    try:
        os.chdir(evaluation_dir)
        dataset, commonsense_checker, hard_checker = _load_official()
        query_by_idx: dict[int, dict[str, Any]] = {}
        for idx, raw_query in enumerate(dataset, start=1):
            if idx not in selected:
                continue
            query = dict(raw_query)
            for field in ("date", "local_constraint"):
                if field in query:
                    query[field] = literal(query[field])
            query_by_idx[idx] = query

        if set(query_by_idx) != selected:
            raise ValueError("validation indices must contain every selected index exactly once")

        prediction_by_idx = {int(row["idx"]): row for row in predictions}
        per_index: list[dict[str, Any]] = []
        for idx in sorted(selected):
            prediction = prediction_by_idx[idx]
            query = query_by_idx[idx]
            plan = prediction.get("plan")
            delivered = bool(plan) and not prediction.get("error")
            row: dict[str, Any] = {
                "idx": idx,
                "level": query.get("level"),
                "days": query.get("days"),
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
                "model": prediction.get("model"),
                "error": prediction.get("error") or (None if delivered else "missing_plan"),
            }
            if not delivered:
                per_index.append(row)
                continue

            commonsense_box = commonsense_checker(query, plan)
            commonsense_passed, commonsense_tested = count_box(commonsense_box)
            commonsense_ok = box_pass(commonsense_box)
            row.update(
                commonsense_box=commonsense_box,
                commonsense_pass=commonsense_ok,
                commonsense_passed=commonsense_passed,
                commonsense_tested=commonsense_tested,
            )

            prerequisites_ok = all(
                _constraint_passed(commonsense_box, key)
                for key in ("is_not_absent", "is_valid_information_in_sandbox")
            )
            if prerequisites_ok:
                hard_box = hard_checker(query, plan)
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
        for path in added_paths:
            if path in sys.path:
                sys.path.remove(path)

    return {"per_index": per_index, "summary": make_summary(per_index, len(selected))}


def write_checkpoint(
    attempts_path: Path,
    queries_path: Path,
    selected: set[int],
    travelplanner_root: str | Path,
    scores_path: Path,
    failure_path: Path,
) -> bool:
    """Score one CC checkpoint and record an honest failure when unavailable."""
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    scores_path.unlink(missing_ok=True)
    failure_path.unlink(missing_ok=True)
    scores_path.with_name(f"scores-{len(selected)}-not-reached.log").unlink(missing_ok=True)
    try:
        scores = evaluate(
            compact_attempts(load_jsonl(attempts_path), load_jsonl(queries_path), selected),
            selected,
            travelplanner_root,
        )
        scores["evaluated_at"] = utc_now()
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


def _format_metric(metric: dict[str, Any]) -> str:
    rate = metric["rate"]
    suffix = "N/A" if rate is None else f"{rate:.2%}"
    return f'{metric["passed"]}/{metric["total"]} ({suffix})'


def _error_status(value: Any) -> str:
    if not value:
        return "-"
    return value if value in {"missing_plan", "not_attempted"} else "error"


def render_report(gateway: dict[str, Any], scores: dict[str, Any]) -> str:
    """Render a safe Markdown report containing only aggregate and per-query results."""
    summary = scores["summary"]
    lines = [
        "# TravelPlanner selected-query evaluation",
        "",
        f'- Gateway base: `{gateway.get("base_url", "unknown")}`',
        f'- Model: `{gateway.get("main_model", "unknown")}`',
        f'- Selected: {summary["selected_count"]}',
        f'- Delivered: {_format_metric(summary["delivery"])}',
        f'- Commonsense micro: {_format_metric(summary["commonsense"]["micro"])}',
        f'- Commonsense macro: {_format_metric(summary["commonsense"]["macro"])}',
        f'- Hard micro: {_format_metric(summary["hard"]["micro"])}',
        f'- Hard macro: {_format_metric(summary["hard"]["macro"])}',
        f'- Final: {_format_metric(summary["final"])}',
        f'- Cost: ${summary["cost_usd"]:.4f}',
        "",
        "| idx | delivered | commonsense | hard | final | cost | error |",
        "| ---: | :---: | :---: | :---: | :---: | ---: | --- |",
    ]
    for row in scores["per_index"]:
        lines.append(
            "| {idx} | {delivered} | {commonsense} | {hard} | {final} | ${cost:.4f} | {error} |".format(
                idx=row["idx"],
                delivered="yes" if row.get("delivered") else "no",
                commonsense="pass" if row.get("commonsense_pass") else "fail",
                hard="pass" if row.get("hard_pass") else "fail",
                final="pass" if row.get("final_pass") else "fail",
                cost=float(row.get("cost_usd") or 0),
                error=_error_status(row.get("error")),
            )
        )

    return "\n".join(lines) + "\n"


def publish_outputs(outputs: dict[Path, str]) -> None:
    """Stage every output beside its target before replacing the final files."""
    temporary = {target: target.with_name(target.name + ".tmp") for target in outputs}
    try:
        for target, temp in temporary.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(outputs[target], encoding="utf-8")
        for target, temp in temporary.items():
            os.replace(temp, target)
    finally:
        for temp in temporary.values():
            temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--indices", required=True)
    parser.add_argument("--travelplanner-root", type=Path, required=True)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    selected = parse_indices_spec(args.indices)
    if len(selected) != 30:
        parser.error("--indices must contain exactly 30 unique integers")

    predictions = compact_attempts(load_jsonl(args.attempts), load_jsonl(args.queries), selected)
    scores = evaluate(predictions, selected, args.travelplanner_root)
    gateway = json.loads(args.gateway.read_text(encoding="utf-8"))
    report = render_report(gateway, scores)
    publish_outputs(
        {
            args.predictions: "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions),
            args.scores: json.dumps(scores, ensure_ascii=False, indent=2) + "\n",
            args.report: report,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
