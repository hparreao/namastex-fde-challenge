from __future__ import annotations

import json
from pathlib import Path

from autoseguro.eval_fleet import SanitizedTrace, evaluate_trace


def test_eval_fleet_matches_all_labeled_cases() -> None:
    cases = json.loads(Path("evals/eval-fleet-labeled.json").read_text(encoding="utf-8"))
    for case in cases:
        result = evaluate_trace(SanitizedTrace.model_validate(case["trace"]))
        failed = sorted(item.evaluator for item in result.evaluations if not item.passed)
        assert failed == sorted(case["expected_failed_evaluators"]), case["case_id"]
        assert [item.evaluator for item in result.evaluations] == sorted(
            item.evaluator for item in result.evaluations
        )
        for evaluation in result.evaluations:
            assert 0 <= evaluation.score <= 1
            assert evaluation.input_tokens == 0
            assert evaluation.output_tokens == 0
            assert evaluation.estimated_cost_usd == 0


def test_eval_fleet_aggregation_is_deterministic() -> None:
    trace = SanitizedTrace(
        trace_id="trace_deterministic",
        final_status="handoff",
        final_state="handoff",
        handoff={"reason": "human_requested"},
    )
    first = evaluate_trace(trace)
    second = evaluate_trace(trace)
    assert first.passed is True
    assert second.passed is True
    assert first.score == second.score
    assert [item.evaluator for item in first.evaluations] == [
        item.evaluator for item in second.evaluations
    ]
