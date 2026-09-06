from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from experiment import evaluate_selected as evaluator


EXACT_30 = "1,11,14,17,28,33,38,41,46,48,70,72,77,81,83,100,110,113,116,118,123,124,138,144,146,151,159,161,162,163"


def test_compact_attempts_prefers_latest_success_and_keeps_other_failure():
    attempts = [
        {"idx": 1, "query": "first", "plan": None, "error": "initial failure"},
        {"idx": 2, "query": "second", "plan": None, "error": "keep this failure"},
        {"idx": 1, "query": "first", "plan": [{"day": 1}], "error": None},
        {"idx": 1, "query": "first", "plan": None, "error": "later failure"},
    ]
    queries = [
        {"idx": 1, "query": "first"},
        {"idx": 2, "query": "second"},
        {"idx": 3, "query": "never attempted"},
    ]

    compacted = evaluator.compact_attempts(attempts, queries, {1, 2, 3})

    assert [row["idx"] for row in compacted] == [1, 2, 3]
    assert compacted[0]["plan"] == [{"day": 1}]
    assert compacted[0]["error"] is None
    assert compacted[1]["error"] == "keep this failure"
    assert compacted[2]["query"] == "never attempted"
    assert compacted[2]["error"] == "not_attempted"


def test_parse_indices_spec_is_an_exact_30_integer_set():
    selected = evaluator.parse_indices_spec(EXACT_30)

    assert len(selected) == 30
    assert selected == {
        1, 11, 14, 17, 28, 33, 38, 41, 46, 48,
        70, 72, 77, 81, 83, 100, 110, 113, 116, 118,
        123, 124, 138, 144, 146, 151, 159, 161, 162, 163,
    }


def test_load_jsonl_skips_blank_lines(tmp_path: Path):
    source = tmp_path / "records.jsonl"
    source.write_text('{"idx": 1}\n\n {"idx": 2}\n', encoding="utf-8")

    assert evaluator.load_jsonl(source) == [{"idx": 1}, {"idx": 2}]


def test_literal_uses_safe_literal_parsing(tmp_path: Path):
    marker = tmp_path / "must-not-exist"
    attack = f"__import__('pathlib').Path({str(marker)!r}).touch()"

    assert evaluator.literal("[1, {'safe': True}]") == [1, {"safe": True}]
    assert evaluator.literal({"already": "parsed"}) == {"already": "parsed"}
    with pytest.raises((SyntaxError, ValueError)):
        evaluator.literal(attack)
    assert not marker.exists()


def test_count_box_and_box_pass_ignore_untested_constraints():
    box = {
        "passed": [True, None],
        "failed": [False, "reason"],
        "untested": [None, "not applicable"],
    }

    assert evaluator.count_box(box) == (1, 2)
    assert not evaluator.box_pass(box)
    assert evaluator.box_pass({"only": [True, None]})
    assert not evaluator.box_pass(None)


def test_make_summary_uses_selected_count_for_all_macro_denominators():
    per_index = [
        {
            "idx": 1,
            "delivered": True,
            "commonsense_pass": True,
            "commonsense_passed": 2,
            "commonsense_tested": 2,
            "hard_pass": True,
            "hard_passed": 1,
            "hard_tested": 1,
            "final_pass": True,
            "cost_usd": 1.25,
        }
    ]

    summary = evaluator.make_summary(per_index, selected_count=30)

    for metric in (
        summary["delivery"],
        summary["commonsense"]["macro"],
        summary["hard"]["macro"],
        summary["final"],
    ):
        assert metric == {"passed": 1, "total": 30, "rate": pytest.approx(1 / 30)}
    assert summary["commonsense"]["micro"] == {"passed": 2, "total": 2, "rate": 1.0}
    assert summary["hard"]["micro"] == {"passed": 1, "total": 1, "rate": 1.0}
    assert summary["cost_usd"] == 1.25


def test_write_checkpoint_reuses_shared_evaluator(monkeypatch, tmp_path: Path):
    attempts = tmp_path / "attempts.jsonl"
    queries = tmp_path / "queries.jsonl"
    scores = tmp_path / "scores.json"
    failure = tmp_path / "scores-failed.json"
    attempts.write_text('{"idx": 1, "plan": [{"day": 1}]}\n', encoding="utf-8")
    queries.write_text('{"idx": 1, "query": "q1"}\n', encoding="utf-8")
    monkeypatch.setattr(
        evaluator,
        "evaluate",
        lambda predictions, selected, root: {"summary": {"selected": len(selected)}},
    )

    assert evaluator.write_checkpoint(attempts, queries, {1}, tmp_path, scores, failure)
    assert json.loads(scores.read_text(encoding="utf-8"))["summary"] == {"selected": 1}
    assert not failure.exists()


def test_evaluate_scores_only_selected_and_gates_hard_constraints(monkeypatch, tmp_path: Path):
    dataset = [
        {"name": "q1", "date": "['2026-09-01']", "local_constraint": "{'budget': 10}", "level": "easy", "days": 1},
        {"name": "q2", "date": "['2026-09-02']", "local_constraint": "{}", "level": "medium", "days": 2},
        {"name": "q3", "date": "['2026-09-03']", "local_constraint": "{}", "level": "hard", "days": 3},
    ]
    commonsense_calls: list[str] = []
    hard_calls: list[str] = []

    def commonsense(query, plan):
        assert isinstance(query["date"], list)
        assert isinstance(query["local_constraint"], dict)
        assert plan
        commonsense_calls.append(query["name"])
        sandbox_ok = query["name"] == "q1"
        return {
            "is_not_absent": [True, None],
            "is_valid_information_in_sandbox": [sandbox_ok, None if sandbox_ok else "invalid"],
            "other": [True, None],
        }

    def hard(query, plan):
        hard_calls.append(query["name"])
        return {"budget": [True, None]}

    monkeypatch.setattr(evaluator, "_load_official", lambda: (dataset, commonsense, hard))
    root = tmp_path / "TravelPlanner"
    (root / "evaluation").mkdir(parents=True)
    predictions = [
        {"idx": 1, "plan": [{"day": 1}], "cost_usd": 0.1, "model": "m"},
        {"idx": 2, "plan": [{"day": 1}], "cost_usd": 0.2, "model": "m"},
        {"idx": 3, "plan": None, "error": "not_attempted", "cost_usd": 0.3},
    ]

    scores = evaluator.evaluate(predictions, {1, 2, 3}, root)

    assert commonsense_calls == ["q1", "q2"]
    assert hard_calls == ["q1"]
    assert [row["idx"] for row in scores["per_index"]] == [1, 2, 3]
    assert scores["per_index"][2]["delivered"] is False
    assert scores["per_index"][1]["hard_box"] is None
    assert scores["summary"]["delivery"] == {"passed": 2, "total": 3, "rate": pytest.approx(2 / 3)}
    assert scores["summary"]["commonsense"]["micro"] == {"passed": 5, "total": 6, "rate": pytest.approx(5 / 6)}
    assert scores["summary"]["commonsense"]["macro"] == {"passed": 1, "total": 3, "rate": pytest.approx(1 / 3)}
    assert scores["summary"]["hard"]["micro"] == {"passed": 1, "total": 1, "rate": 1.0}
    assert scores["summary"]["hard"]["macro"] == {"passed": 1, "total": 3, "rate": pytest.approx(1 / 3)}
    assert scores["summary"]["final"] == {"passed": 1, "total": 3, "rate": pytest.approx(1 / 3)}
    assert scores["summary"]["cost_usd"] == pytest.approx(0.6)


def test_evaluate_rejects_prediction_indices_that_are_not_exact(tmp_path: Path):
    with pytest.raises(ValueError, match="exactly"):
        evaluator.evaluate([{"idx": 1, "plan": []}], {1, 2}, tmp_path)


def test_evaluate_preserves_preexisting_import_paths(monkeypatch, tmp_path: Path):
    root = tmp_path / "TravelPlanner"
    (root / "evaluation").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(root.resolve()))
    before = list(sys.path)
    monkeypatch.setattr(
        evaluator,
        "_load_official",
        lambda: ([{"date": "[]", "local_constraint": "{}"}], None, None),
    )

    evaluator.evaluate([{"idx": 1, "plan": None}], {1}, root)

    assert sys.path == before


def test_render_report_has_safe_gateway_model_cost_and_per_query_table():
    scores = {
        "summary": {
            "selected_count": 30,
            "delivery": {"passed": 1, "total": 30, "rate": 1 / 30},
            "commonsense": {
                "micro": {"passed": 2, "total": 2, "rate": 1.0},
                "macro": {"passed": 1, "total": 30, "rate": 1 / 30},
            },
            "hard": {
                "micro": {"passed": 1, "total": 1, "rate": 1.0},
                "macro": {"passed": 1, "total": 30, "rate": 1 / 30},
            },
            "final": {"passed": 1, "total": 30, "rate": 1 / 30},
            "cost_usd": 1.25,
        },
        "per_index": [
            {
                "idx": 1,
                "delivered": True,
                "commonsense_pass": True,
                "hard_pass": True,
                "final_pass": True,
                "cost_usd": 1.25,
                "error": "api_key=report-secret thinking trace",
            }
        ],
    }
    gateway = {
        "base_url": "https://gateway.example",
        "main_model": "claude-opus-test",
        "api_key": "must-not-be-reported",
    }

    text = evaluator.render_report(gateway, scores)
    assert "https://gateway.example" in text
    assert "claude-opus-test" in text
    assert "1/30" in text
    assert "$1.2500" in text
    assert "| 1 |" in text
    assert "must-not-be-reported" not in text
    assert "report-secret" not in text
    assert "thinking" not in text.lower()


def test_publish_outputs_stages_every_file_before_replacing(monkeypatch, tmp_path: Path):
    outputs = {
        tmp_path / "predictions.jsonl": "predictions\n",
        tmp_path / "scores.json": "scores\n",
        tmp_path / "report.md": "report\n",
    }
    real_replace = evaluator.os.replace
    replaced: list[Path] = []

    def observed_replace(source, target):
        if not replaced:
            assert all(path.with_name(path.name + ".tmp").exists() for path in outputs)
        real_replace(source, target)
        replaced.append(Path(target))

    monkeypatch.setattr(evaluator.os, "replace", observed_replace)

    evaluator.publish_outputs(outputs)

    assert replaced == list(outputs)
    assert {path: path.read_text(encoding="utf-8") for path in outputs} == outputs
    assert not list(tmp_path.glob("*.tmp"))


def test_publish_outputs_cleans_remaining_temps_when_replace_fails(monkeypatch, tmp_path: Path):
    outputs = {
        tmp_path / "predictions.jsonl": "predictions\n",
        tmp_path / "scores.json": "scores\n",
        tmp_path / "report.md": "report\n",
    }
    real_replace = evaluator.os.replace
    calls = 0

    def fail_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("replace failed")
        real_replace(source, target)

    monkeypatch.setattr(evaluator.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="replace failed"):
        evaluator.publish_outputs(outputs)

    assert not list(tmp_path.glob("*.tmp"))


def test_cli_compacts_exactly_30_before_writing_scores(monkeypatch, tmp_path: Path):
    selected = evaluator.parse_indices_spec(EXACT_30)
    attempts = tmp_path / "attempts.jsonl"
    queries = tmp_path / "queries.jsonl"
    gateway = tmp_path / "gateway.json"
    predictions = tmp_path / "predictions.jsonl"
    scores_path = tmp_path / "scores.json"
    report = tmp_path / "report.md"
    attempts.write_text(json.dumps({"idx": 1, "query": "q1", "plan": [{"day": 1}]}) + "\n", encoding="utf-8")
    queries.write_text(
        "".join(json.dumps({"idx": idx, "query": f"q{idx}"}) + "\n" for idx in sorted(selected)),
        encoding="utf-8",
    )
    gateway.write_text(json.dumps({"base_url": "https://gateway.example", "main_model": "model"}), encoding="utf-8")

    def fake_evaluate(compacted, received_selected, travelplanner_root):
        assert len(compacted) == 30
        assert {row["idx"] for row in compacted} == selected
        assert received_selected == selected
        assert travelplanner_root == tmp_path / "TravelPlanner"
        per_index = [
            {
                "idx": row["idx"],
                "delivered": bool(row.get("plan")),
                "commonsense_pass": False,
                "commonsense_passed": 0,
                "commonsense_tested": 0,
                "hard_pass": False,
                "hard_passed": 0,
                "hard_tested": 0,
                "final_pass": False,
                "cost_usd": row.get("cost_usd") or 0,
                "error": row.get("error"),
            }
            for row in compacted
        ]
        return {"per_index": per_index, "summary": evaluator.make_summary(per_index, 30)}

    monkeypatch.setattr(evaluator, "evaluate", fake_evaluate)

    assert evaluator.main(
        [
            "--attempts", str(attempts),
            "--queries", str(queries),
            "--indices", EXACT_30,
            "--travelplanner-root", str(tmp_path / "TravelPlanner"),
            "--gateway", str(gateway),
            "--predictions", str(predictions),
            "--scores", str(scores_path),
            "--report", str(report),
        ]
    ) == 0
    assert len(evaluator.load_jsonl(predictions)) == 30
    assert json.loads(scores_path.read_text(encoding="utf-8"))["summary"]["selected_count"] == 30
    assert report.exists()


def test_cli_rejects_any_selection_other_than_30(tmp_path: Path, capsys):
    with pytest.raises(SystemExit):
        evaluator.main(
            [
                "--attempts", str(tmp_path / "attempts.jsonl"),
                "--queries", str(tmp_path / "queries.jsonl"),
                "--indices", ",".join(str(i) for i in range(1, 30)),
                "--travelplanner-root", str(tmp_path / "TravelPlanner"),
                "--gateway", str(tmp_path / "gateway.json"),
                "--predictions", str(tmp_path / "predictions.jsonl"),
                "--scores", str(tmp_path / "scores.json"),
                "--report", str(tmp_path / "report.md"),
            ]
        )

    assert "exactly 30" in capsys.readouterr().err


def test_cli_does_not_publish_predictions_when_evaluation_fails(monkeypatch, tmp_path: Path):
    selected = evaluator.parse_indices_spec(EXACT_30)
    attempts = tmp_path / "attempts.jsonl"
    queries = tmp_path / "queries.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    attempts.write_text("", encoding="utf-8")
    queries.write_text(
        "".join(json.dumps({"idx": idx, "query": f"q{idx}"}) + "\n" for idx in sorted(selected)),
        encoding="utf-8",
    )

    def fail_evaluation(*_args):
        raise RuntimeError("evaluation failed")

    monkeypatch.setattr(evaluator, "evaluate", fail_evaluation)
    with pytest.raises(RuntimeError, match="evaluation failed"):
        evaluator.main(
            [
                "--attempts", str(attempts),
                "--queries", str(queries),
                "--indices", EXACT_30,
                "--travelplanner-root", str(tmp_path / "TravelPlanner"),
                "--gateway", str(tmp_path / "gateway.json"),
                "--predictions", str(predictions),
                "--scores", str(tmp_path / "scores.json"),
                "--report", str(tmp_path / "report.md"),
            ]
        )

    assert not predictions.exists()


def test_cli_does_not_publish_any_output_when_gateway_is_invalid(monkeypatch, tmp_path: Path):
    selected = evaluator.parse_indices_spec(EXACT_30)
    attempts = tmp_path / "attempts.jsonl"
    queries = tmp_path / "queries.jsonl"
    gateway = tmp_path / "gateway.json"
    outputs = [tmp_path / "predictions.jsonl", tmp_path / "scores.json", tmp_path / "report.md"]
    attempts.write_text("", encoding="utf-8")
    queries.write_text(
        "".join(json.dumps({"idx": idx, "query": f"q{idx}"}) + "\n" for idx in sorted(selected)),
        encoding="utf-8",
    )
    gateway.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(evaluator, "evaluate", lambda *_args: {"per_index": [], "summary": {}})

    with pytest.raises(json.JSONDecodeError):
        evaluator.main(
            [
                "--attempts", str(attempts),
                "--queries", str(queries),
                "--indices", EXACT_30,
                "--travelplanner-root", str(tmp_path / "TravelPlanner"),
                "--gateway", str(gateway),
                "--predictions", str(outputs[0]),
                "--scores", str(outputs[1]),
                "--report", str(outputs[2]),
            ]
        )

    assert not any(path.exists() for path in outputs)


def test_scale_official_scores_caps_selected_passes() -> None:
    summary = evaluator.scale_official_scores({"Final Pass Rate": 1.0}, selected_count=1)

    assert summary["final"] == {"passed": 1, "total": 1, "rate": 1.0}
