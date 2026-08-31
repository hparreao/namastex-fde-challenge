from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .telemetry import SafeTelemetry

logger = logging.getLogger(__name__)


class PolicyAction(StrEnum):
    CALL_LLM = "CallLLM"
    CALL_QUOTE = "CallQuote"
    PERSIST_AUDIT = "PersistAudit"
    COMPLETE_SESSION = "CompleteSession"
    HANDOFF_SESSION = "HandoffSession"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    engine_available: bool
    reasons: tuple[str, ...] = ()
    error_category: str | None = None


class PolicyEngine:
    name = "unavailable"

    def evaluate(
        self, action: PolicyAction, session_id: str, context: dict[str, Any]
    ) -> PolicyDecision:
        del action, session_id, context
        return PolicyDecision(
            allowed=False,
            engine_available=False,
            error_category="engine_unavailable",
        )


class CedarPolicyEngine(PolicyEngine):
    name = "cedarpy"

    def __init__(self, policy_path: Path, schema_path: Path) -> None:
        from cedarpy import PolicySet, Schema, validate_policies

        policy_text = policy_path.read_text(encoding="utf-8")
        schema_text = schema_path.read_text(encoding="utf-8")
        validation = validate_policies(policy_text, schema_text)
        if not validation.validation_passed:
            errors = "; ".join(str(error) for error in validation.errors)
            raise ValueError(f"Cedar policy validation failed: {errors}")
        self.policy_set = PolicySet.from_str(policy_text)
        self.schema = Schema.from_str(schema_text)

    def evaluate(
        self, action: PolicyAction, session_id: str, context: dict[str, Any]
    ) -> PolicyDecision:
        try:
            from cedarpy import Decision, is_authorized

            entity = {"type": "AutoSeguro::Session", "id": session_id}
            request = {
                "principal": entity,
                "action": {"type": "AutoSeguro::Action", "id": action.value},
                "resource": entity,
                "context": context,
            }
            entities = [{"uid": entity, "attrs": {}, "parents": []}]
            result = is_authorized(
                request,
                self.policy_set,
                entities,
                schema=self.schema,
            )
            return PolicyDecision(
                allowed=result.decision is Decision.Allow,
                engine_available=True,
                reasons=tuple(result.diagnostics.reasons),
                error_category=("evaluation_error" if result.diagnostics.errors else None),
            )
        except Exception as exc:
            logger.warning("cedar_evaluation_failed", extra={"error_type": type(exc).__name__})
            return PolicyDecision(
                allowed=False,
                engine_available=False,
                error_category=type(exc).__name__,
            )


class PolicyController:
    def __init__(
        self,
        engine: PolicyEngine,
        *,
        mode: str,
        enforce_actions: set[PolicyAction],
        telemetry: SafeTelemetry,
    ) -> None:
        if mode not in {"off", "shadow", "enforce"}:
            raise ValueError("POLICY_MODE deve ser 'off', 'shadow' ou 'enforce'")
        self.engine = engine
        self.mode = mode
        self.enforce_actions = enforce_actions
        self.telemetry = telemetry

    def check(
        self, action: PolicyAction, session_id: str, context: dict[str, Any]
    ) -> PolicyDecision:
        if self.mode == "off":
            return PolicyDecision(allowed=True, engine_available=False, reasons=("disabled",))
        decision = self.engine.evaluate(action, session_id, context)
        with self.telemetry.span(
            "cedar_policy_decision",
            {
                "session_id": session_id,
                "policy_action": action.value,
                "policy_mode": self.mode,
                "policy_engine": self.engine.name,
                "allowed": decision.allowed,
                "engine_available": decision.engine_available,
                "error_category": decision.error_category,
            },
        ):
            pass
        return decision

    def blocks(self, action: PolicyAction, decision: PolicyDecision) -> bool:
        return self.mode == "enforce" and action in self.enforce_actions and not decision.allowed


def policy_from_config(
    *,
    mode: str,
    policy_path: str,
    schema_path: str,
    enforce_actions: str,
    telemetry: SafeTelemetry,
) -> PolicyController:
    engine: PolicyEngine
    try:
        engine = CedarPolicyEngine(Path(policy_path), Path(schema_path))
    except Exception as exc:
        logger.warning("cedar_initialization_failed", extra={"error_type": type(exc).__name__})
        engine = PolicyEngine()
    parsed_actions: set[PolicyAction] = set()
    for item in enforce_actions.split(","):
        if item.strip():
            parsed_actions.add(PolicyAction(item.strip()))
    return PolicyController(
        engine,
        mode=mode,
        enforce_actions=parsed_actions,
        telemetry=telemetry,
    )
