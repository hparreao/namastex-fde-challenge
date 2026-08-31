from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(char for char in normalized if not unicodedata.combining(char)).split())


def namespace(item: dict[str, Any], side: str) -> str:
    collected = json.dumps(item[f"collected_{side}"], sort_keys=True, separators=(",", ":"))
    parts = [
        item[f"state_{side}"],
        hashlib.sha256(collected.encode()).hexdigest(),
        "fake",
        "deterministic-v1",
        "2026-08-28.1",
        "agent-decision.v1",
        "pt-BR",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def evaluate(dataset: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in dataset:
        similarity = SequenceMatcher(
            None,
            normalize(item["message_a"]),
            normalize(item["message_b"]),
        ).ratio()
        decisions_differ = item["expected_decision_a"] != item["expected_decision_b"]
        naive_hit = similarity >= threshold
        namespaced_hit = naive_hit and namespace(item, "a") == namespace(item, "b")
        results.append(
            {
                "dimension": item["dimension"],
                "similarity": round(similarity, 4),
                "decisions_differ": decisions_differ,
                "naive_hit": naive_hit,
                "namespaced_hit": namespaced_hit,
                "naive_unsafe": naive_hit and decisions_differ,
                "namespaced_unsafe": namespaced_hit and decisions_differ,
            }
        )

    naive_hits = sum(item["naive_hit"] for item in results)
    namespaced_hits = sum(item["namespaced_hit"] for item in results)
    naive_unsafe = sum(item["naive_unsafe"] for item in results)
    namespaced_unsafe = sum(item["namespaced_unsafe"] for item in results)
    return {
        "method": "lexical SequenceMatcher proxy; no embedding model was validated",
        "threshold": threshold,
        "pairs": len(results),
        "naive": {
            "hits": naive_hits,
            "unsafe_hits": naive_unsafe,
            "unsafe_false_hit_rate": round(naive_unsafe / naive_hits, 4) if naive_hits else 0,
        },
        "context_namespaced": {
            "hits": namespaced_hits,
            "unsafe_hits": namespaced_unsafe,
            "unsafe_false_hit_rate": (
                round(namespaced_unsafe / namespaced_hits, 4) if namespaced_hits else 0
            ),
        },
        "decision": (
            "reject_semantic_cache"
            if namespaced_unsafe > 0
            else "insufficient_evidence_for_implementation"
        ),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path, default=Path("evals/semantic-cache-adversarial.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("../artifacts/semantic-cache-evaluation.json")
    )
    parser.add_argument("--threshold", type=float, default=0.82)
    args = parser.parse_args()
    report = evaluate(json.loads(args.dataset.read_text(encoding="utf-8")), args.threshold)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: report[key] for key in ("pairs", "naive", "context_namespaced", "decision")},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
