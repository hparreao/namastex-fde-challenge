from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from autoseguro.eval_fleet import SanitizedTrace, evaluate_trace


def _metrics(rows: list[dict[str, Any]], evaluator_names: list[str]) -> dict[str, Any]:
    per_evaluator: dict[str, Any] = {}
    total_tp = total_tn = total_fp = total_fn = 0
    for name in evaluator_names:
        tp = tn = fp = fn = 0
        for row in rows:
            expected = name in row["expected_failed_evaluators"]
            predicted = name in row["predicted_failed_evaluators"]
            if expected and predicted:
                tp += 1
            elif expected:
                fn += 1
            elif predicted:
                fp += 1
            else:
                tn += 1
        total_tp += tp
        total_tn += tn
        total_fp += fp
        total_fn += fn
        per_evaluator[name] = {
            "agreement_rate": (tp + tn) / len(rows),
            "false_positives": fp,
            "false_negatives": fn,
        }
    denominator = total_tp + total_tn + total_fp + total_fn
    return {
        "agreement_rate": (total_tp + total_tn) / denominator,
        "false_positives": total_fp,
        "false_negatives": total_fn,
        "per_evaluator": per_evaluator,
    }


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evals/eval-fleet-labeled.json")
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("../artifacts/eval-fleet-report.json")
    cases = json.loads(source.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        output = evaluate_trace(SanitizedTrace.model_validate(case["trace"]))
        latencies.append((time.perf_counter() - started) * 1000)
        failed = sorted(item.evaluator for item in output.evaluations if not item.passed)
        rows.append(
            {
                "case_id": case["case_id"],
                "expected_failed_evaluators": sorted(case["expected_failed_evaluators"]),
                "predicted_failed_evaluators": failed,
                "equivalent": failed == sorted(case["expected_failed_evaluators"]),
                "fleet_output": output.model_dump(mode="json"),
            }
        )
    names = ["quote_integrity", "privacy", "handoff", "resilience"]
    fleet_metrics = _metrics(rows, names)
    baseline_rows = [
        {
            **row,
            "predicted_failed_evaluators": [
                name
                for name in row["predicted_failed_evaluators"]
                if name in {"quote_integrity", "privacy"}
            ],
        }
        for row in rows
    ]
    baseline_metrics = _metrics(baseline_rows, names)
    report = {
        "evaluation_version": "eval-fleet.v1",
        "mode": "offline_deterministic_parallel",
        "labeled_cases": len(rows),
        "baseline": {**baseline_metrics, "evaluators": ["quote_integrity", "privacy"]},
        "fleet": {
            **fleet_metrics,
            "evaluators": names,
            "p50_latency_ms": statistics.median(latencies),
            "p95_latency_ms": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)],
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        },
        "additional_defects_found": sum(
            1 for row in rows if set(row["expected_failed_evaluators"]) & {"handoff", "resilience"}
        ),
        "cases": rows,
        "limitations": [
            "Synthetic labeled traces test rule calibration, not LLM-judge quality.",
            "Agents SDK evaluator agents were not run because OPENAI_API_KEY was absent.",
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("labeled_cases", "baseline", "fleet", "additional_defects_found")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
