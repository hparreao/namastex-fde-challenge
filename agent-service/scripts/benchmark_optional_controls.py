from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from autoseguro.policy import CedarPolicyEngine, PolicyAction, PolicyController, PolicyEngine
from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository
from autoseguro.service import AgentService
from autoseguro.telemetry import InMemoryTelemetryBackend, SafeTelemetry


class CountingProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls = 0

    def generate_decision(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().generate_decision(**kwargs)


def repository() -> Repository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    result = Repository(engine)
    result.create_schema()
    return result


def quote_client() -> QuoteClient:
    return QuoteClient(
        "http://unused.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        sleeper=lambda _: None,
    )


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def run_profile(name: str, iterations: int, enabled: bool) -> tuple[dict[str, Any], list[str]]:
    provider = CountingProvider()
    telemetry_backend = InMemoryTelemetryBackend() if enabled else None
    telemetry = SafeTelemetry(telemetry_backend)
    policy_dir = Path(__file__).parents[1] / "policies"
    engine = (
        CedarPolicyEngine(
            policy_dir / "autoseguro.cedar",
            policy_dir / "autoseguro.cedarschema",
        )
        if enabled
        else PolicyEngine()
    )
    policy = PolicyController(
        engine,
        mode="shadow" if enabled else "off",
        enforce_actions={PolicyAction.CALL_QUOTE},
        telemetry=telemetry,
    )
    service = AgentService(
        repository(),
        provider,
        quote_client(),
        telemetry=telemetry,
        policy=policy,
    )
    durations: list[float] = []
    decisions: list[str] = []
    for _ in range(iterations):
        session_id = service.create_session().session.id
        started = time.perf_counter()
        result = service.handle_message(session_id, "Toyota Corolla 2022")
        durations.append((time.perf_counter() - started) * 1000)
        decisions.append(
            json.dumps(
                {
                    "state": result.session.state.value,
                    "collected": result.session.collected.model_dump(mode="json"),
                    "reply": result.assistant_message.content,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return (
        {
            "profile": name,
            "iterations": iterations,
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(percentile(durations, 0.95), 3),
            "llm_calls": provider.calls,
            "tokens": None,
            "tokens_available": False,
            "semantic_cache_hits": 0,
            "semantic_cache_hit_rate": 0.0,
            "telemetry_spans": len(telemetry_backend.records) if telemetry_backend else 0,
        },
        decisions,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument(
        "--output", type=Path, default=Path("../artifacts/baseline-vs-enabled.json")
    )
    parser.add_argument(
        "--semantic-report",
        type=Path,
        default=Path("../artifacts/semantic-cache-evaluation.json"),
    )
    args = parser.parse_args()
    baseline, baseline_decisions = run_profile("baseline", args.iterations, False)
    enabled, enabled_decisions = run_profile(
        "otel_in_memory_plus_cedar_shadow", args.iterations, True
    )
    equal = sum(
        left == right for left, right in zip(baseline_decisions, enabled_decisions, strict=True)
    )
    semantic = json.loads(args.semantic_report.read_text(encoding="utf-8"))
    report = {
        "scope": (
            "Local deterministic comparison. Redis, OTLP export, Langfuse and real LLM "
            "are excluded and are not claimed as benchmarked."
        ),
        "baseline": baseline,
        "enabled": enabled,
        "decision_equivalence": {
            "equal": equal,
            "total": args.iterations,
            "rate": round(equal / args.iterations, 4),
        },
        "unsafe_false_hit_rate": semantic["context_namespaced"]["unsafe_false_hit_rate"],
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
