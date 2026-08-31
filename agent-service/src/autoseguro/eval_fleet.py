from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .pii import find_pii


class EvaluationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    span: str
    status: str = "ok"
    attributes: dict[str, Any] = Field(default_factory=dict)


class SanitizedTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    final_status: str
    final_state: str
    events: list[EvaluationEvent] = Field(default_factory=list)
    quote: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None


class EvaluatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluator: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    findings: list[str] = Field(default_factory=list)
    evidence_event_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    duration_ms: float = Field(ge=0.0)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class FleetOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    passed: bool
    score: float
    evaluations: list[EvaluatorOutput]
    aggregation_version: str = "eval-fleet.v1"


class TraceEvaluator(Protocol):
    name: str

    def evaluate(self, trace: SanitizedTrace) -> EvaluatorOutput: ...


class _DeterministicEvaluator:
    name = "base"

    def _result(
        self,
        started: float,
        findings: list[str],
        evidence: list[str],
    ) -> EvaluatorOutput:
        passed = not findings
        return EvaluatorOutput(
            evaluator=self.name,
            passed=passed,
            score=1.0 if passed else 0.0,
            findings=findings,
            evidence_event_ids=sorted(set(evidence)),
            confidence=1.0,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


class QuoteIntegrityEvaluator(_DeterministicEvaluator):
    name = "quote_integrity"

    def evaluate(self, trace: SanitizedTrace) -> EvaluatorOutput:
        started = time.perf_counter()
        successful = [
            event
            for event in trace.events
            if event.span == "quote_attempt" and event.attributes.get("status") == "success"
        ]
        completions = [event for event in trace.events if event.span == "completion"]
        findings: list[str] = []
        evidence: list[str] = []
        if trace.quote is not None:
            if not successful:
                findings.append("quote_without_successful_quote_attempt")
            if trace.quote.get("source") != "quote-service":
                findings.append("quote_source_not_proven")
            evidence.extend(event.event_id for event in successful)
        if (trace.final_status == "completed" or completions) and trace.quote is None:
            findings.append("completion_without_quote")
            evidence.extend(event.event_id for event in completions)
        return self._result(started, findings, evidence)


class PrivacyEvaluator(_DeterministicEvaluator):
    name = "privacy"
    forbidden_keys = {"raw_prompt", "raw_response", "capability_token", "session_token"}

    def evaluate(self, trace: SanitizedTrace) -> EvaluatorOutput:
        started = time.perf_counter()
        serialized = json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        pii = sorted(find_pii(serialized))
        forbidden: list[str] = []
        evidence: list[str] = []
        for event in trace.events:
            keys = {str(key).lower() for key in event.attributes}
            if keys & self.forbidden_keys:
                forbidden.extend(sorted(keys & self.forbidden_keys))
                evidence.append(event.event_id)
        findings = [f"pii_detected:{category}" for category in pii]
        findings.extend(f"forbidden_trace_attribute:{key}" for key in sorted(set(forbidden)))
        return self._result(started, findings, evidence)


class HandoffEvaluator(_DeterministicEvaluator):
    name = "handoff"
    quote_failure_reasons = {
        "eligibility_rejected",
        "invalid_quote_payload",
        "policy_call_quote_denied",
        "quote_service_unavailable",
    }

    def evaluate(self, trace: SanitizedTrace) -> EvaluatorOutput:
        started = time.perf_counter()
        findings: list[str] = []
        evidence = [event.event_id for event in trace.events if event.span == "handoff"]
        if trace.final_status == "handoff" and not trace.handoff:
            findings.append("handoff_status_without_reason")
        if trace.final_status == "completed" and trace.handoff:
            findings.append("completion_and_handoff_conflict")
        reason = str((trace.handoff or {}).get("reason", ""))
        if reason in self.quote_failure_reasons and trace.quote is not None:
            findings.append("failed_quote_handoff_contains_quote")
        return self._result(started, findings, evidence)


class ResilienceEvaluator(_DeterministicEvaluator):
    name = "resilience"
    retryable = {"retryable_error", "timeout", "transport_error"}

    def evaluate(self, trace: SanitizedTrace) -> EvaluatorOutput:
        started = time.perf_counter()
        attempts = [event for event in trace.events if event.span == "quote_attempt"]
        findings: list[str] = []
        evidence = [event.event_id for event in attempts]
        grouped: dict[str, list[EvaluationEvent]] = {}
        for event in attempts:
            quote_id = str(event.attributes.get("quote_id", "missing"))
            grouped.setdefault(quote_id, []).append(event)
        for quote_id, group in grouped.items():
            numbers = [int(event.attributes.get("attempt", -1)) for event in group]
            if len(numbers) != len(set(numbers)):
                findings.append(f"duplicate_quote_attempt:{quote_id}")
            expected = list(range(min(numbers, default=1), max(numbers, default=0) + 1))
            if numbers and sorted(numbers) != expected:
                findings.append(f"non_contiguous_attempts:{quote_id}")
            statuses = [str(event.attributes.get("status", "")) for event in group]
            exhausted = len(group) >= 3 and statuses[-1] in self.retryable
            if exhausted and trace.final_status != "handoff":
                findings.append(f"exhausted_retries_without_handoff:{quote_id}")
        return self._result(started, findings, evidence)


DEFAULT_EVALUATORS: tuple[TraceEvaluator, ...] = (
    QuoteIntegrityEvaluator(),
    PrivacyEvaluator(),
    HandoffEvaluator(),
    ResilienceEvaluator(),
)


def evaluate_trace(
    trace: SanitizedTrace,
    evaluators: tuple[TraceEvaluator, ...] = DEFAULT_EVALUATORS,
    *,
    max_workers: int = 4,
) -> FleetOutput:
    """Run side-effect-free evaluators concurrently and aggregate in stable name order."""
    with ThreadPoolExecutor(max_workers=min(max_workers, len(evaluators))) as executor:
        outputs = list(executor.map(lambda evaluator: evaluator.evaluate(trace), evaluators))
    outputs.sort(key=lambda output: output.evaluator)
    return FleetOutput(
        trace_id=trace.trace_id,
        passed=all(output.passed for output in outputs),
        score=round(sum(output.score for output in outputs) / len(outputs), 6),
        evaluations=outputs,
    )
